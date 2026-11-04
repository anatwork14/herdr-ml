import sys, datetime
for line in sys.stdin:
    print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", line.rstrip("\n"), flush=True)
