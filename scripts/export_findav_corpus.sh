#!/bin/bash
# Read-only findav export for av_auto library-scan.
# Writes a mode-600 TSV. Stdout is counts only.
# Never prints ed2k, magnet, cookies, or passwords.
set -euo pipefail
set +x

dest="${1:-}"
if [[ -z "$dest" || "$dest" != /* || "$dest" != *.tsv ]]; then
  echo "ERROR: 语料输出必须是绝对路径 .tsv（不打印链接）。" >&2
  exit 2
fi

envfile="${AV_AUTO_FINDAV_ENV:-/mnt/user/appdata/findav/connection.env}"
if [[ ! -f "$envfile" ]]; then
  echo "ERROR: 找不到 findav 连接文件。" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$envfile"
set +a

if [[ -z "${FINDDAV_USER:-}" || -z "${FINDDAV_PASSWORD:-}" || -z "${FINDDAV_DB:-}" ]]; then
  echo "ERROR: findav 连接文件缺少必要字段。" >&2
  exit 1
fi

export MYSQL_PWD="$FINDDAV_PASSWORD"
unset FINDDAV_PASSWORD
export FINDDAV_USER FINDDAV_DB

container="${FINDAV_DB_CONTAINER:-findav-db}"

sql() {
  local query="$1"
  docker exec -e MYSQL_PWD -e FINDDAV_USER -e FINDDAV_DB "$container" \
    sh -c 'exec mariadb -u"$FINDDAV_USER" -D"$FINDDAV_DB" --default-character-set=utf8mb4 -N -B -e "$1"' \
    sh "$query"
}

# av_ed2k / av_magnet store hashes, not download URIs.
# Same formula as library_scan.ed2k_uri / magnet_uri.
# A page reference column is not a download link and is not selected.
umask 077
mkdir -p "$(dirname "$dest")"
tmp="${dest}.tmp"
: > "$tmp"
chmod 600 "$tmp"

sql "SELECT code, IFNULL(user_verdict,'pending'), IFNULL(erotic_level,'') FROM av_title" \
  | awk -F '\t' 'NF >= 1 && $1 != "" { print "T\t" $1 "\t" $2 "\t" $3 }' >> "$tmp"

sql "SELECT TRIM(code), REPLACE(REPLACE(IFNULL(file_name, ''), CHAR(10), ''), CHAR(13), ''), IFNULL(size_bytes, 0), TRIM(IFNULL(file_hash, '')) FROM av_ed2k" \
  | awk -F '\t' '
      $1 != "" && $4 != "" && $3 ~ /^[0-9]+$/ && ($3 + 0) > 0 {
        hash = $4
        name = $2
        if (name == "") name = hash ".mp4"
        gsub(/\|/, "_", name)
        gsub(/\r/, "", name)
        gsub(/\n/, "", name)
        printf "%d\t%s\ted2k://|file|%s|%d|%s|/\n", ($3 + 0), $1, name, ($3 + 0), tolower(hash)
      }
    ' \
  | LC_ALL=C sort -t "$(printf '\t')" -k1,1nr -k2,2 -k3,3 \
  | awk -F '\t' '{ printf "E\t%s\t%s\n", $2, $3 }' >> "$tmp"

sql "SELECT TRIM(code), IFNULL(infohash, '') FROM av_magnet" \
  | awk -F '\t' '
      {
        code = $1
        hash = $2
        gsub(/^[ \t]+|[ \t]+$/, "", hash)
        if (code != "" && hash != "") printf "%s\t%s\n", code, tolower(hash)
      }
    ' \
  | LC_ALL=C sort -t "$(printf '\t')" -k1,1 -k2,2 \
  | awk -F '\t' '{ printf "M\t%s\tmagnet:?xt=urn:btih:%s\n", $1, $2 }' >> "$tmp"

titles="$(awk -F '\t' '$1 == "T" { n++ } END { print n + 0 }' "$tmp")"
ed2k_rows="$(awk -F '\t' '$1 == "E" { n++ } END { print n + 0 }' "$tmp")"
magnet_rows="$(awk -F '\t' '$1 == "M" { n++ } END { print n + 0 }' "$tmp")"

if [[ "$(id -u)" == "0" ]]; then
  if uid="$(docker exec moviepilot id -u 2>/dev/null)" && gid="$(docker exec moviepilot id -g 2>/dev/null)"; then
    chown "$uid:$gid" "$tmp"
  fi
fi
chmod 600 "$tmp"
mv -f "$tmp" "$dest"
chmod 600 "$dest"
echo "titles=${titles} ed2k_rows=${ed2k_rows} magnet_rows=${magnet_rows}"
