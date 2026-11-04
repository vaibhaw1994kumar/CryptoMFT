#!/bin/bash
# samples the resident memory of every replay / test process every 30 s; the peak per process is the maximum of its column
echo "ts,pid,rss_mb,tag" > /Users/vaibhav/Documents/Newproject/out/queue_v2/logs/rss_samples.csv
while true; do
  ps -axo pid,rss,command | grep -E "qv2_run.py|pytest tests|oct_bybit|qv2_cal_replay" | grep -v grep | while read pid rss rest; do
    tag=$(ps eww -p $pid 2>/dev/null | tr ' ' '\n' | grep -E "^EXEC_TAG=" | head -1 | cut -d= -f2)
    echo "$(date +%T),$pid,$((rss/1024)),${tag:-other}" >> /Users/vaibhav/Documents/Newproject/out/queue_v2/logs/rss_samples.csv
  done
  sleep 30
done
