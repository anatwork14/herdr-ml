import json, base64, re, sys, pathlib
run = pathlib.Path(sys.argv[1])
for n in (1, 2, 3):
    raw = (run / f"10-observer-{n}.out").read_text(errors="replace")
    dec = json.JSONDecoder(); i = 0; objs = []
    while i < len(raw):
        while i < len(raw) and raw[i].isspace(): i += 1
        if i >= len(raw): break
        o, j = dec.raw_decode(raw, i); objs.append(o); i = j
    print(f"== attach{n}: {len(objs)} JSON objects")
    ticks_all = []
    for k, o in enumerate(objs):
        keys = sorted(o.keys())
        meta = {kk: o[kk] for kk in o if kk not in ("bytes", "data", "payload")}
        b = o.get("bytes") or o.get("data") or ""
        text = base64.b64decode(b).decode("utf-8", "replace") if b else ""
        t = re.findall(r"TICK (\d+)", text)
        ticks_all += t
        print(f"  obj{k}: keys={keys} meta={json.dumps(meta)[:220]} decoded_bytes={len(text.encode())} ticks={t[:1]}..{t[-1:] if t else []} count={len(t)}")
    print(f"  total TICK occurrences decoded: {len(ticks_all)}; unique: {len(set(ticks_all))}")
