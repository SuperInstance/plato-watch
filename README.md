# plato-watch 🔮

PLATO emergence monitor — watches PLATO rooms for topology changes and detects
emergence events using H1 cohomology (β₁) as an early warning system.

## Theory

The fleet math says:

```
β₁ = E - V + C    (Betti-1 = edges - vertices + components)
ε = β₁ / (V-2) - 1    (emergence severity)
```

- **ε > 0** = emergence — something new is happening in the room
- **ε > 0.7** = approaching — keep watching
- **ε > 0.9** = 🚨 **EMERGENCE DETECTED** — topology is restructuring

## Usage

### Watch a room

```bash
python3 plato-watch.py watch --room forge --interval 30
```

Poll every 30 seconds and print emergence metrics:

```
[11:02:31] forge | V=12 E=28 C=1 β₁=17 threshold=10 ε=0.70 ⚠️ approaching
[11:03:01] forge | V=13 E=31 C=1 β₁=19 threshold=11 ε=0.73 ⚠️ approaching
[11:03:31] forge | V=14 E=36 C=1 β₁=23 threshold=12 ε=0.92 🚨 EMERGENCE DETECTED
```

### Scan all rooms

```bash
python3 plato-watch.py scan
```

Rank all rooms by emergence severity ε, top-5 emergent first:

```
room                 | V    E    β₁   V-2  ε     status
forge                | 14   36   23   12   0.92  🚨 EMERGENT
disc-golf-math       | 4    4    1    2    -0.50 ✅ stable
...
```

### Daemon (background)

```bash
nohup python3 plato-watch.py daemon --log /tmp/plato-emergence.log &
```

Runs continuously, writes to log file. Optionally watch a specific room:

```bash
python3 plato-watch.py daemon --room forge --log /tmp/plato-emergence.log
```

## Edge Detection

Uses **Jaccard similarity** on word tokens (no external deps). Default threshold
is 0.15 — two tiles are connected if their word overlap exceeds 15%.

Adjust with `--alert-threshold` on any command.

## State

Watch state is tracked in `.plato-watch-state.json` — keeps metric history and
alerts. Alerts are deduplicated so you only get notified on status transitions
(stable → approaching → emergent).

## Dependencies

Zero. Python 3 stdlib only.
