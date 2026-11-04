import json, base64, sys
rows = []
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    ts, _, payload = line.partition(" ")
    try:
        d = json.loads(payload)
    except Exception:
        continue
    raw = d.get("bytes") or d.get("data") or ""
    try:
        txt = base64.b64decode(raw).decode("utf-8", "replace") if raw else ""
    except Exception:
        txt = ""
    rows.append((ts, d.get("type"), d.get("seq"), d.get("full"), len(txt), "DETACHED-WINDOW-DONE" in txt, txt.count("DETACHED-WINDOW")))
for r in rows:
    print("\t".join(str(x) for x in r))
print("frames", len(rows))
