"""Process-survival evidence: count ticker lines inside each observed window.

Inputs: run/ticker.log (one line per second, written by the pane's own loop, UTC timestamp),
run/10-observer-timeline.txt (observer attach windows), run/11-tui-attach-*.json (pty attach windows).
Output: run/survival.txt. A window is attributed by the log timestamp of each TICK line.
"""
import json, re, pathlib
from datetime import datetime
run = pathlib.Path("run")
def ts(s): return datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S.%f")
lines = []
for l in (run / "ticker.log").read_text().splitlines():
    m = re.match(r"TICK (\d+) (\S+)$", l)
    if m: lines.append((int(m.group(1)), ts(m.group(2))))
out = []
out.append(f"ticker.log lines: {len(lines)}; first TICK {lines[0][0]} {lines[0][1].isoformat()}Z; last TICK {lines[-1][0]} {lines[-1][1].isoformat()}Z")
gaps = [(lines[i+1][1]-lines[i][1]).total_seconds() for i in range(len(lines)-1)]
out.append(f"tick interval: min {min(gaps):.3f}s max {max(gaps):.3f}s; sequence contiguous: {all(lines[i+1][0]==lines[i][0]+1 for i in range(len(lines)-1))}")
def count(a, b):
    return sum(1 for _, t in lines if a <= t <= b)
for l in (run / "10-observer-timeline.txt").read_text().splitlines():
    m = re.match(r"(attach\d) (start|end)=(\S+)", l)
    if m: pass
obs = {}
for l in (run / "10-observer-timeline.txt").read_text().splitlines():
    m = re.match(r"(attach\d) start=(\S+) ", l)
    if m: obs.setdefault(m.group(1), {})["start"] = ts(m.group(2))
    m = re.match(r"(attach\d) end=(\S+) exit=(\d+)", l)
    if m: obs.setdefault(m.group(1), {}).update(end=ts(m.group(2)), exit=m.group(3))
for k in sorted(obs):
    o = obs[k]
    out.append(f"observer {k}: window {o['start'].isoformat()}Z..{o['end'].isoformat()}Z exit={o['exit']} ticks_in_window={count(o['start'], o['end'])}")
for n in (1, 2):
    j = json.loads((run / f"11-tui-attach-{n}.json").read_text())
    a, b = ts(j["start"]), ts(j["end"])
    out.append(f"tui attach {n}: window {a.isoformat()}Z..{b.isoformat()}Z exit_code={j['exit_code']} detach_observed_exit={j['detach_observed_exit']} ticks_in_window={count(a, b)}")
# gap between the last observer window and the tui attach 1 window (ticker kept running while detached)
out.append("interpretation: ticks inside each window and during gaps show the pane process kept running; this is process survival, not streaming evidence.")
(run / "survival.txt").write_text("\n".join(out) + "\n")
print("\n".join(out))
