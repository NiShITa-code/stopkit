#!/bin/bash
d=$(mktemp -d)
chown root:crontab "$d"; chmod 1730 "$d"
echo line1 > "$d/n8ntest"; chown n8ntest:crontab "$d/n8ntest"; chmod 600 "$d/n8ntest"
echo "id=$(id -u) caps=$(grep CapEff /proc/self/status)"
ls -ld "$d"; ls -l "$d/n8ntest"
python3 -c "open('$d/n8ntest','w').write('rewritten\n'); print('direct open-w OK')" || echo "direct open-w FAIL"
ip netns add sttest 2>/dev/null
ip netns exec sttest python3 -c "open('$d/n8ntest','w').write('ns\n'); print('netns open-w OK')" || echo "netns open-w FAIL"
ip netns del sttest 2>/dev/null
rm -rf "$d"
