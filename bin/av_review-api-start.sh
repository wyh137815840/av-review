#!/bin/bash
set -euo pipefail
cd /home/yuanhao/av-review
mkdir -p logs run
# already running?
if pgrep -af "[p]ython3 .*av_review api" >/dev/null; then
  echo "api already running"
  exit 0
fi
exec /usr/bin/python3 /home/yuanhao/av-review/bin/av_review api --host 0.0.0.0 --port 18765 >> /home/yuanhao/av-review/logs/api.log 2>&1
