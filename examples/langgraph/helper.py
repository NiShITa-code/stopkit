"""The helper the demo agent starts in the background, as coding agents start development servers. It reports
to the outside service every 2 s until something ends it."""
import json
import time
import urllib.request

while True:
    try:
        urllib.request.urlopen(urllib.request.Request(
            'http://127.0.0.1:8899/action', data=json.dumps({'source': 'helper', 'text': 'still running'}).encode(),
            headers={'Content-Type': 'application/json'}), timeout=2).read()
    except OSError:
        pass
    time.sleep(2)
