#!/bin/sh
# campaign cb1013, worker w4, launch 1; written by campaign_b.py
printf '%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /root/stopcheck-campaign/cb1013/w4-launch-1.start.tmp && mv /root/stopcheck-campaign/cb1013/w4-launch-1.start.tmp /root/stopcheck-campaign/cb1013/w4-launch-1.start
cd /root/stopcheck || { printf '97 %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /root/stopcheck-campaign/cb1013/w4-launch-1.exit; exit 97; }
env STOPCHECK_HOST_PROFILE=linux-vm /root/pilot-venv/bin/python -B n8n/run_expB.py --freeze n8n/FREEZE-expB.json --conditions B1 B4b B9a --seed 20261012 --batch-id cb1013-w4 > /root/stopcheck-campaign/cb1013/w4-launch-1.log 2>&1
status=$?
printf '%s %s\n' "$status" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /root/stopcheck-campaign/cb1013/w4-launch-1.exit.tmp && mv /root/stopcheck-campaign/cb1013/w4-launch-1.exit.tmp /root/stopcheck-campaign/cb1013/w4-launch-1.exit
exit "$status"
