#!/usr/bin/env python3
"""
plato-watch: PLATO emergence monitor

Watches PLATO rooms for topology changes and detects emergence events
using H1 cohomology / beta-1 as an early warning system.

β₁ = E - V + C
ε = β₁ / (V - 2) - 1

Usage:
  python3 plato-watch.py watch --room forge --interval 30
  python3 plato-watch.py scan
  python3 plato-watch.py daemon --log /tmp/plato-emergence.log
"""

import argparse
import json
import math
import os
import signal
import sys
import time
import urllib.error
import urllib.request

# ── config ──────────────────────────────────────────────────────────────────

PLATO_HOST = "http://localhost:8847"
POLL_INTERVAL = 30  # seconds
JACCARD_THRESHOLD = 0.15
ALERT_APPROACHING = 0.7
ALERT_EMERGENCE = 0.9
STATE_FILE = ".plato-watch-state.json"
TERMINATE = False

# ── helpers ─────────────────────────────────────────────────────────────────


def plato_get(path):
    """Fetch a JSON resource from PLATO."""
    url = f"{PLATO_HOST}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} from {url}", file=sys.stderr)
        return None
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        print(f"  Connection error: {e}", file=sys.stderr)
        return None


def list_rooms():
    """Return list of room names from PLATO."""
    data = plato_get("/rooms")
    if data is None:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Dict of room_name -> metadata
        for key in ("rooms", "room", "data", "names"):
            val = data.get(key)
            if isinstance(val, list):
                return val
            if isinstance(val, dict):
                return list(val.keys())
        # If the dict keys ARE the room names (e.g. {"forge": {...}, "oc1": {...}})
        # Check first key looks like a room name
        keys = list(data.keys())
        if keys and not any(k in data for k in ("room", "name", "id", "key")):
            # Assume top-level keys are room names if values are dicts with tile_count
            if all(isinstance(v, dict) for v in data.values()):
                return keys
    print(f"  Unexpected /rooms response: {type(data).__name__}", file=sys.stderr)
    return []



def fetch_tiles(room):
    """Fetch all tiles in a room. Returns list of tile dicts."""
    data = plato_get(f"/room/{room}")
    if data is None:
        return []
    if isinstance(data, dict):
        for key in ("tiles", "data", "messages", "items"):
            if key in data and isinstance(data[key], list):
                return data[key]
    if isinstance(data, list):
        return data
    print(f"  Unexpected /room/{room} response: {type(data).__name__}", file=sys.stderr)
    return []


def extract_text(tile):
    """Extract text content from a tile, whatever shape it is."""
    if isinstance(tile, str):
        return tile
    if isinstance(tile, dict):
        for key in ("content", "text", "message", "body", "title", "summary"):
            val = tile.get(key)
            if val and isinstance(val, str):
                return val
        # fallback: stringify all values
        return " ".join(str(v) for v in tile.values() if isinstance(v, (str, int, float)))
    return str(tile)


def tokenize(text):
    """Simple word tokenizer (lowercase, split on non-alpha)."""
    out = []
    buf = []
    for ch in text.lower():
        if ch.isalpha() or ch == "'":
            buf.append(ch)
        else:
            if buf:
                w = "".join(buf)
                if len(w) > 1:
                    out.append(w)
                buf = []
    if buf:
        w = "".join(buf)
        if len(w) > 1:
            out.append(w)
    return out


def jaccard_similarity(tokens_a, tokens_b):
    """Jaccard similarity between two token sets."""
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    if union == 0:
        return 0.0
    return inter / union


def compute_emergence(tiles, threshold=None):
    """
    Given a list of tile texts, compute emergence metrics.

    Returns dict with V, E, C, beta1, v_minus_2, epsilon, status, error.
    """
    if threshold is None:
        threshold = JACCARD_THRESHOLD

    if not tiles:
        return {"V": 0, "E": 0, "C": 0, "beta1": 0, "v_minus_2": 0,
                "epsilon": 0, "status": "empty", "error": None}

    texts = [extract_text(t) for t in tiles]
    tokens = [tokenize(t) for t in texts]

    V = len(tiles)
    E = 0
    for i in range(V):
        for j in range(i + 1, V):
            if jaccard_similarity(tokens[i], tokens[j]) > threshold:
                E += 1

    # Connected components (union-find)
    parent = list(range(V))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(V):
        for j in range(i + 1, V):
            if jaccard_similarity(tokens[i], tokens[j]) > threshold:
                union(i, j)

    roots = set(find(i) for i in range(V))
    C = len(roots)

    # Euler characteristic: χ = V - E (for a graph)
    # Betti-0 (C) = connected components
    # Betti-1: β₁ = E - V + C
    beta1 = E - V + C
    v_minus_2 = V - 2
    if v_minus_2 > 0:
        # ε > 0 means emergence. ε = 0 at β₁ = V - 2.
        # ε > 1 means E > 2(V-2) + C
        epsilon = beta1 / v_minus_2 - 1
    else:
        epsilon = -1.0  # too small to assess

    # Status
    if epsilon >= ALERT_EMERGENCE:
        status = "EMERGENT"
    elif epsilon >= ALERT_APPROACHING:
        status = "approaching"
    else:
        status = "stable"

    return {
        "V": V,
        "E": E,
        "C": C,
        "beta1": beta1,
        "v_minus_2": v_minus_2,
        "epsilon": round(epsilon, 4),
        "status": status,
        "error": None,
    }


def load_state():
    """Load emergence tracking state from file."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"history": {}, "alerts": []}


def save_state(state):
    """Persist tracking state."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def record_observation(state, room, metrics):
    """Record an emergence observation for a room."""
    if room not in state["history"]:
        state["history"][room] = []
    state["history"][room].append({
        "time": time.time(),
        "epsilon": metrics["epsilon"],
        "V": metrics["V"],
        "E": metrics["E"],
        "beta1": metrics["beta1"],
        "status": metrics["status"],
    })
    # Keep last 1000 observations per room
    if len(state["history"][room]) > 1000:
        state["history"][room] = state["history"][room][-1000:]
    return state


def format_watch_line(room, metrics, alert=False):
    """Format a timestamped watch-mode output line."""
    t = time.strftime("%H:%M:%S")
    m = metrics
    icon = "🚨 EMERGENCE DETECTED" if alert else ("⚠️ approaching" if m["status"] == "approaching" else "✅ stable")
    return f"[{t}] {room} | V={m['V']} E={m['E']} C={m['C']} β₁={m['beta1']} threshold={m['v_minus_2']} ε={m['epsilon']:.2f} {icon}"


def check_alert(room, metrics, state):
    """Check if current metrics cross alert thresholds. Returns alert info or None."""
    if metrics["V"] < 3:
        return None
    if metrics["epsilon"] < ALERT_APPROACHING:
        return None

    # Check if we already alerted for this level
    room_history = state["history"].get(room, [])
    if room_history:
        last = room_history[-1]
        last_status = last["status"]
        # Don't re-alert for same level
        if metrics["status"] == last_status:
            return None
        # Don't re-alert for approaching if last was already emergent
        if metrics["status"] == "approaching" and last_status == "EMERGENT":
            return None

    level = "EMERGENCE" if metrics["epsilon"] >= ALERT_EMERGENCE else "approaching"
    return {
        "room": room,
        "level": level,
        "metrics": metrics,
        "time": time.time(),
    }


# ── modes ──────────────────────────────────────────────────────────────────


def cmd_watch(args):
    """Watch a specific room continuously."""
    room = args.room
    interval = args.interval or POLL_INTERVAL
    alert_threshold = args.alert_threshold
    state = load_state()

    global TERMINATE
    def handle_signal(sig, frame):
        global TERMINATE
        TERMINATE = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"Watching room '{room}' every {interval}s (Ctrl+C to stop)")
    print(f"  Jaccard threshold: {alert_threshold or JACCARD_THRESHOLD}")
    print()

    while not TERMINATE:
        tiles = fetch_tiles(room)
        if tiles:
            metrics = compute_emergence(tiles, threshold=alert_threshold)
            alert_info = check_alert(room, metrics, state)
            is_alert = alert_info is not None
            state = record_observation(state, room, metrics)

            line = format_watch_line(room, metrics, alert=is_alert)
            print(line)

            if is_alert:
                level_icon = "🚨" if alert_info["level"] == "EMERGENCE" else "⚠️"
                print(f"  {level_icon} ALERT: {room} | ε={metrics['epsilon']:.2f} | {alert_info['level']}")
                state["alerts"].append(alert_info)
        else:
            print(f"[{time.strftime('%H:%M:%S')}] {room} | no tiles or connection error")

        save_state(state)

        if not TERMINATE:
            # Sleep in small chunks so we respond to signals promptly
            for _ in range(interval):
                if TERMINATE:
                    break
                time.sleep(1)

    print("\nStopped.")


def cmd_scan(args):
    """Scan all rooms and rank by emergence."""
    rooms = list_rooms()
    if not rooms:
        print("No rooms found or PLATO unreachable.")
        return

    results = []

    print("=== Emergence Scan: all rooms ===\n")

    for room in sorted(rooms):
        tiles = fetch_tiles(room)
        if not tiles:
            continue
        metrics = compute_emergence(tiles)
        results.append((room, metrics))

    # Sort by epsilon descending (most emergent first)
    results.sort(key=lambda r: r[1]["epsilon"], reverse=True)

    # Header
    print(f"{'room':<22} {'V':<5} {'E':<5} {'β₁':<5} {'V-2':<5} {'ε':<6} status")
    print("-" * 70)

    for rank, (room, m) in enumerate(results, 1):
        icon = "🚨 EMERGENT" if m["epsilon"] >= ALERT_EMERGENCE else ("⚠️ approaching" if m["epsilon"] >= ALERT_APPROACHING else "✅ stable")
        print(f"{room:<22} {m['V']:<5} {m['E']:<5} {m['beta1']:<5} {m['v_minus_2']:<5} {m['epsilon']:<6.2f} {icon}")

    print()
    print(f"Scanned {len(results)} rooms with >= 3 tiles.")
    print(f"Emergent rooms: {sum(1 for _, m in results if m['epsilon'] >= ALERT_EMERGENCE)}")
    print(f"Approaching:    {sum(1 for _, m in results if ALERT_APPROACHING <= m['epsilon'] < ALERT_EMERGENCE)}")
    print(f"Stable:         {sum(1 for _, m in results if m['epsilon'] < ALERT_APPROACHING)}")


def cmd_daemon(args):
    """Run as background daemon, logging to file."""
    log_path = args.log or "/tmp/plato-emergence.log"
    interval = args.interval or POLL_INTERVAL
    alert_threshold = args.alert_threshold
    room = args.room  # optional: specific room

    state = load_state()

    # Redirect stdout/stderr to log
    log_fh = open(log_path, "a", buffering=1)
    os.dup2(log_fh.fileno(), sys.stdout.fileno())
    os.dup2(log_fh.fileno(), sys.stderr.fileno())

    global TERMINATE
    def handle_signal(sig, frame):
        global TERMINATE
        TERMINATE = True

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    target = f"room '{room}'" if room else "all rooms"
    print(f"[daemon] plato-watch started. Watching {target} every {interval}s.")
    print(f"[daemon] Log: {log_path}")
    print(f"[daemon] State: {STATE_FILE}")

    while not TERMINATE:
        if room:
            # Watch specific room
            tiles = fetch_tiles(room)
            if tiles:
                metrics = compute_emergence(tiles, threshold=alert_threshold)
                alert_info = check_alert(room, metrics, state)
                is_alert = alert_info is not None
                state = record_observation(state, room, metrics)

                line = format_watch_line(room, metrics, alert=is_alert)
                print(line)

                if is_alert:
                    print(f"  ALERT: {room} | ε={metrics['epsilon']:.2f} | {alert_info['level']}")
                    state["alerts"].append(alert_info)
            else:
                print(f"[daemon] {room}: no tiles or connection error")
        else:
            # Scan all rooms
            rooms = list_rooms()
            tick = time.strftime("%H:%M:%S")
            print(f"[daemon {tick}] Scanning {len(rooms)} rooms...")
            for r in sorted(rooms):
                tiles = fetch_tiles(r)
                if not tiles:
                    continue
                metrics = compute_emergence(tiles, threshold=alert_threshold)
                alert_info = check_alert(r, metrics, state)
                state = record_observation(state, r, metrics)

                if metrics["epsilon"] >= ALERT_APPROACHING:
                    print(f"  {r}: ε={metrics['epsilon']:.2f} [{metrics['status']}]")
                    if alert_info:
                        state["alerts"].append(alert_info)

        save_state(state)

        if not TERMINATE:
            for _ in range(interval):
                if TERMINATE:
                    break
                time.sleep(1)

    print("[daemon] plato-watch stopped.")


# ── entry ──────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="plato-watch: PLATO emergence monitor",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # watch
    p_watch = sub.add_parser("watch", help="Watch a specific room continuously")
    p_watch.add_argument("--room", required=True, help="Room name to watch")
    p_watch.add_argument("--interval", type=int, default=None, help=f"Poll interval (default {POLL_INTERVAL}s)")
    p_watch.add_argument("--alert-threshold", type=float, default=None, help=f"Jaccard similarity threshold (default {JACCARD_THRESHOLD})")

    # scan
    p_scan = sub.add_parser("scan", help="Scan all rooms and rank by emergence")
    p_scan.add_argument("--alert-threshold", type=float, default=None, help=f"Jaccard similarity threshold (default {JACCARD_THRESHOLD})")

    # daemon
    p_daemon = sub.add_parser("daemon", help="Run as background daemon")
    p_daemon.add_argument("--log", default="/tmp/plato-emergence.log", help="Log file path")
    p_daemon.add_argument("--room", default=None, help="Specific room to watch (omit to scan all)")
    p_daemon.add_argument("--interval", type=int, default=None, help=f"Poll interval (default {POLL_INTERVAL}s)")
    p_daemon.add_argument("--alert-threshold", type=float, default=None, help=f"Jaccard similarity threshold (default {JACCARD_THRESHOLD})")

    args = parser.parse_args()

    if args.command == "watch":
        cmd_watch(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "daemon":
        cmd_daemon(args)


if __name__ == "__main__":
    main()
