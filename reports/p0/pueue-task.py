import json, os, sys, time
path = sys.argv[1]
project = sys.argv[2]
def event(action):
    with open(path, 'a', encoding='utf-8') as out:
        out.write(json.dumps({'project': project, 'event': action, 'monotonic_ns': time.monotonic_ns(), 'pid': os.getpid()}) + '\n')
event('start')
time.sleep(0.7)
event('end')
