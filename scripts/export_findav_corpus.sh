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

pick_col() {
  local table="$1"
  shift
  local cols name
  cols="$(sql "SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='${table}'")"
  for name in "$@"; do
    if printf '%s\n' "$cols" | tr -d '\r' | grep -qx "$name"; then
      printf '%s' "$name"
      return 0
    fi
  done
  return 1
}

ed2k_code="$(pick_col av_ed2k code av_code title_code)" || {
  echo "ERROR: av_ed2k 没有番号列。" >&2
  exit 1
}
ed2k_uri="$(pick_col av_ed2k ed2k ed2k_url ed2k_link uri url link)" || {
  echo "ERROR: av_ed2k 没有链接列。" >&2
  exit 1
}
magnet_code="$(pick_col av_magnet code av_code title_code)" || {
  echo "ERROR: av_magnet 没有番号列。" >&2
  exit 1
}
magnet_uri="$(pick_col av_magnet magnet magnet_url magnet_link uri url link)" || {
  echo "ERROR: av_magnet 没有链接列。" >&2
  exit 1
}

for ident in "$ed2k_code" "$ed2k_uri" "$magnet_code" "$magnet_uri"; do
  if [[ ! "$ident" =~ ^[A-Za-z0-9_]+$ ]]; then
    echo "ERROR: 列名无法安全引用。" >&2
    exit 1
  fi
done

umask 077
mkdir -p "$(dirname "$dest")"
tmp="${dest}.tmp"
: > "$tmp"
chmod 600 "$tmp"

sql "SELECT code, IFNULL(user_verdict,'pending'), IFNULL(erotic_level,'') FROM av_title" \
  | awk -F '\t' 'NF >= 1 && $1 != "" { print "T\t" $1 "\t" $2 "\t" $3 }' >> "$tmp"

sql "SELECT \`${ed2k_code}\`, \`${ed2k_uri}\` FROM av_ed2k" \
  | awk -F '\t' 'NF >= 2 && $1 != "" && $2 != "" { print "E\t" $1 "\t" $2 }' >> "$tmp"

sql "SELECT \`${magnet_code}\`, \`${magnet_uri}\` FROM av_magnet" \
  | awk -F '\t' 'NF >= 2 && $1 != "" && $2 != "" { print "M\t" $1 "\t" $2 }' >> "$tmp"

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
