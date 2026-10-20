#!/usr/bin/env bash
# farm.sh —— 在上海/香港服务器上跑整套「发现 → 远程提取 → 上传 123」
#
# 为什么放服务器：Actions 在美国，上传 123 走国际链路很慢；
# 服务器到 ROM CDN 和 123 都是国内链路，单个包 100MB 通常 10~30 秒传完。
#
# 用法：
#   ./server/farm.sh incremental   # 每天增量：每机型 1 个版本
#   ./server/farm.sh reconcile     # 对账：扫全部版本，只补网盘缺的
#   ./server/farm.sh backfill 100  # 一次性补 100 个
#
# 依赖：python3 / aria2 / p7zip / unzip / rclone / curl / lz4 / payload_dumper
set -euo pipefail

MODE="${1:-incremental}"
MAX_TOTAL="${2:-12}"
cd "$(dirname "$0")/.."

DST="${RCLONE_DEST:-123:zanfang}"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------------- #
# 0. 依赖自检
# --------------------------------------------------------------------------- #
for c in python3 rclone aria2c 7z unzip curl lz4 payload_dumper; do
  command -v "$c" >/dev/null 2>&1 || { echo "缺少 $c，先跑 server/install.sh" >&2; exit 1; }
done
rclone lsd "${DST%%:*}" >/dev/null 2>&1 || { echo "rclone 远程 ${DST%%:*} 不可用" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# 1. 列网盘已有内容（已上传的就不再提取）
# --------------------------------------------------------------------------- #
mkdir -p out state logs
log "列网盘清单 $DST"
rclone lsf "$DST" -R --files-only --format pst > out/remote.txt 2>/dev/null || true
echo "网盘现有文件数: $(wc -l < out/remote.txt)"

# --------------------------------------------------------------------------- #
# 2. 发现
# --------------------------------------------------------------------------- #
log "发现（模式 $MODE，上限 $MAX_TOTAL）"
ARGS=(--devices devices.json --state state/seen.json
      --out out/candidates.json --matrix-out out/matrix.json
      --existing out/remote.txt --max-total "$MAX_TOTAL" --print)
if [ "$MODE" = "reconcile" ] || [ "$MODE" = "backfill" ]; then
  ARGS+=(--all-versions --max-new-per-device 50)
else
  ARGS+=(--new-only --max-new-per-device 1)
fi
if [ -n "${ONLY:-}" ];  then ARGS+=(--only "$ONLY");      fi
if [ -n "${BRAND:-}" ]; then ARGS+=(--brandcn "$BRAND");  fi

python3 discover.py "${ARGS[@]}"

# --------------------------------------------------------------------------- #
# 3. 逐个提取上传（matrix.json -> TSV）
# --------------------------------------------------------------------------- #
python3 - out/matrix.json <<'PY' > out/jobs.tsv
import json, sys
items = json.load(open(sys.argv[1], encoding="utf-8"))["include"]
for i in items:
    print("\t".join([
        i.get("brandcn") or "", i.get("cat") or "", i.get("devname") or "",
        i.get("model") or "", i.get("version") or "", i.get("name") or "",
        i.get("url") or "", i.get("resolve") or "", i.get("referer") or "",
    ]))
PY

TOTAL=$(wc -l < out/jobs.tsv)
log "待处理 $TOTAL 个"
[ "$TOTAL" -gt 0 ] || { echo "没有需要补的包，收工"; exit 0; }

PARALLEL="${PARALLEL:-3}"
FAILED=0
: > out/failed.txt

run_one() {
  local brandcn="$1" cat="$2" devname="$3" model="$4" version="$5" name="$6" url="$7" resolve="$8" referer="$9"
  local logf="logs/${name//[^A-Za-z0-9._-]/_}.log"
  BRANDCN="$brandcn" CAT="$cat" DEVNAME="$devname" MODEL="$model" VERSION="$version" \
  ROM_NAME="$name" ROM_URL="$url" RESOLVE_CMD="$resolve" REFERER="$referer" \
  RCLONE_DEST="$DST" PASSFILE="${PASSFILE:-}" UNZIP_PASSWORD="${UNZIP_PASSWORD:-}" \
    ./extract_one.sh >"$logf" 2>&1 && echo "OK   $brandcn/$cat/$devname/$name" \
    || { echo "FAIL $brandcn/$cat/$devname/$name  (见 $logf)"; echo "$name" >> out/failed.txt; }
}
export -f run_one
export DST PASSFILE UNZIP_PASSWORD

# shellcheck disable=SC2016
xargs -a out/jobs.tsv -d '\n' -P "$PARALLEL" -I{} bash -c '
  IFS=$'"'"'\t'"'"' read -r brandcn cat devname model version name url resolve referer <<< "{}"
  run_one "$brandcn" "$cat" "$devname" "$model" "$version" "$name" "$url" "$resolve" "$referer"
'

log "完成：失败 $(wc -l < out/failed.txt) 个"
[ -s out/failed.txt ] && cat out/failed.txt
exit 0
