#!/bin/bash
# Process jobs.jsonl: probe or frame. Never echo source URLs.
# Distinct from the in-flight rest scan (/tmp/rest-vision-work).
set -uo pipefail
JOBS="${1:?jobs.jsonl}"
OUTROOT="/tmp/av-review-work"
PROBE_TO="${PROBE_TIMEOUT:-90}"
FRAME_TO="${FRAME_TIMEOUT:-180}"
mkdir -p "$OUTROOT/probes" "$OUTROOT/frames" "$OUTROOT/logs"
docker exec embyserver mkdir -p /tmp/av-review-frames

n=0
ok=0
fail=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  n=$((n+1))
  action=$(echo "$line" | jq -r .action)
  code=$(echo "$line" | jq -r .code)
  url=$(echo "$line" | jq -r .url)
  label=$(echo "$line" | jq -r .label)
  if [[ "$action" == "probe" ]]; then
    dest="$OUTROOT/probes/${code}.json"
    if timeout "$PROBE_TO" docker exec embyserver ffprobe -v error -show_entries format=duration,format_name -show_entries stream=codec_name,codec_type,width,height -of json "$url" > "$dest" 2> "$OUTROOT/logs/${code}_probe.err"; then
      ok=$((ok+1))
      echo "PROBE_OK $code"
    else
      fail=$((fail+1))
      echo "PROBE_FAIL $code exit:$?"
    fi
    continue
  fi
  if [[ "$action" == "frame" ]]; then
    ss=$(echo "$line" | jq -r .ss)
    fname="${code}_${label}.jpg"
    cpath="/tmp/av-review-frames/${fname}"
    hostpath="$OUTROOT/frames/${fname}"
    if timeout "$FRAME_TO" docker exec embyserver ffmpeg -hide_banner -loglevel error -y -ss "$ss" -i "$url" -frames:v 1 -q:v 2 -vf "scale=w='min(800,iw)':h=-2" "$cpath" 2> "$OUTROOT/logs/${code}_${label}.err"; then
      if docker cp "embyserver:${cpath}" "$hostpath" 2>> "$OUTROOT/logs/${code}_${label}.err"; then
        sz=$(stat -c%s "$hostpath" 2>/dev/null || echo 0)
        ok=$((ok+1))
        echo "FRAME_OK $code $label ss=$ss bytes=$sz"
      else
        fail=$((fail+1))
        echo "FRAME_CP_FAIL $code $label"
      fi
    else
      fail=$((fail+1))
      echo "FRAME_FAIL $code $label ss=$ss"
    fi
    continue
  fi
  echo "UNKNOWN $action $code"
  fail=$((fail+1))
done < "$JOBS"
echo "SUMMARY n=$n ok=$ok fail=$fail"
