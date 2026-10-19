#!/usr/bin/env bash
# extract_one.sh —— 单个 ROM 包：下载 → 提取 boot/init_boot/vbmeta → 加密打包 → 上传 123
#
# 入参（环境变量）
#   ROM_URL        必填，刷机包直链
#   ROM_NAME       必填，产物名（workflow 里已 sanitize、无扩展名）
#   BRAND MODEL VERSION  可选，用于网盘目录和元数据
#   PARTITIONS     可选，默认 boot,init_boot,vbmeta
#   UNZIP_PASSWORD 可选，设置后加密打包
#   PASSFILE       可选，要一起打进压缩包的说明文件（如 获取解压密码.txt）
#   RCLONE_DEST    可选，默认 123:zhanfang
#
# 支持：小米卡刷包(zip+payload.bin)、线刷包(tgz/tar)、三星 tar.md5、
#       Google factory/OTA zip、lolinet zip、img.lz4
set -euo pipefail

: "${ROM_URL:=}"
: "${ROM_NAME:=}"
: "${RESOLVE_CMD:=}"
if [ -z "$ROM_URL" ] && [ -z "$RESOLVE_CMD" ]; then
  echo "ROM_URL 或 RESOLVE_CMD 至少要给一个" >&2
  exit 2
fi
BRAND="${BRAND:-unknown}"
MODEL="${MODEL:-unknown}"
VERSION="${VERSION:-}"
PARTITIONS="${PARTITIONS:-boot,init_boot,vbmeta}"
DEST="${RCLONE_DEST:-123:zhanfang}"
PASS="${UNZIP_PASSWORD:-}"
PASSFILE="${PASSFILE:-}"
WORK="$(pwd)/work"
REPO_ROOT="$(pwd)"   # 解析命令里的相对路径（tools/oplus/...）以仓库根为基准

log() { printf '\n==> %s\n' "$*"; }

mkdir -p "$WORK/raw" "$WORK/out" "$WORK/ext"
cd "$WORK"
TARGET="$DEST/$BRAND/$MODEL"

# --------------------------------------------------------------------------- #
# A. 动态链接现场解析（OPPO / 一加 / realme）
#    这类链接 10~30 分钟过期，必须在下载的同一个 job 里「解析完立刻下」，
#    所以解析放在这里而不是 discover 阶段。
# --------------------------------------------------------------------------- #
if [ -n "$RESOLVE_CMD" ]; then
  log "解析直链（动态链接）"
  OUT=""
  for attempt in 1 2 3; do
    # 注意：此时 cwd 已经是 work/，解析命令必须在仓库根目录执行
    OUT=$( cd "$REPO_ROOT" && bash -c "$RESOLVE_CMD" 2>&1 ) || true
    URLS=$(printf '%s' "$OUT" |
      grep -oE 'https?://[^ "'"'"'<>]+\.(zip|ozip)(\?[^ "'"'"'<>]*)?' || true)
    # tomboy_pro 一次会打印 A/C/F/H/J 多个分支的结果，靠后的更新；
    # 且优先取带签名参数的（不带 sign/Expires 的 original link 通常 403）
    ROM_URL=$(printf '%s\n' "$URLS" | grep -E 'Expires=|sign=' | tail -1 || true)
    if [ -z "${ROM_URL:-}" ]; then
      ROM_URL=$(printf '%s\n' "$URLS" | tail -1 || true)
    fi
    if [ -n "${ROM_URL:-}" ]; then break; fi
    # 解析器本身不存在时重试没有意义，直接跳出并说明原因
    if printf '%s' "$OUT" | grep -q "can.t open file"; then
      log "解析器不存在（tools/oplus 没准备好），不再重试"
      break
    fi
    log "第 $attempt 次没解析出链接，5 秒后重试"
    sleep 5
  done
  if [ -z "${ROM_URL:-}" ]; then
    echo "解析失败。命令输出末尾 40 行：" >&2
    printf '%s\n' "$OUT" | tail -40 >&2
    echo "" >&2
    echo "排查：解析命令是 —— $RESOLVE_CMD" >&2
    echo "      它依赖 tools/oplus/tomboy_pro.py（由 batch.yml 里「准备欧加解析工具」步骤 clone）" >&2
    exit 9
  fi
  # 产物名用解析出来的真实文件名（里面有版本号），比占位名有用
  ROM_NAME=$(basename "${ROM_URL%%\?*}")
  ROM_NAME=$(printf '%s' "$ROM_NAME" | sed -E 's/\.(zip|ozip)$//I; s/[^A-Za-z0-9._-]+/_/g')
  log "已解析: $ROM_NAME"
fi

# --------------------------------------------------------------------------- #
# B. 同目录查重：动态源每天都要重查，已传过的包不该再下一次
# --------------------------------------------------------------------------- #
if rclone lsf "$TARGET/" --files-only 2>/dev/null | grep -qxF "${ROM_NAME}.zip"; then
  log "已存在 ${ROM_NAME}.zip，跳过"
  exit 0
fi

# --------------------------------------------------------------------------- #
# C. 体积预检：OPPO 有些分支只放 80~200M 的增量/组件包，下了也提不出 boot
# --------------------------------------------------------------------------- #
probe_total() {
  curl -s -o /dev/null -D - -r 0-0 "$1" 2>/dev/null | tr -d '\r' \
    | awk 'tolower($1)=="content-range:"{split($2,a,"/");print a[2];exit}'
}
TOTAL=$(probe_total "$ROM_URL" || true)
if [ -n "${TOTAL:-}" ] && [ "$TOTAL" -lt 1073741824 ]; then
  HUMAN=$(numfmt --to=iec "$TOTAL")
  log "包体只有 ${HUMAN}，判定为增量/组件包，跳过（完整包一般 4G 以上）"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "- SKIP \`${ROM_NAME}\`：仅 ${HUMAN}，疑似增量包" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi
[ -n "${TOTAL:-}" ] && log "远端包体 $(numfmt --to=iec "$TOTAL")"

# --------------------------------------------------------------------------- #
# 0. 磁盘检查 —— 8G 的包在 14G 的 runner 上必爆，先看清楚
# --------------------------------------------------------------------------- #
AVAIL_KB=$(df -Pk "$WORK" | awk 'NR==2{print $4}')
AVAIL_GB=$((AVAIL_KB / 1024 / 1024))
log "可用磁盘 ${AVAIL_GB}G"
if [ "$AVAIL_GB" -lt 10 ]; then
  echo "磁盘不足 10G，放弃（检查 workflow 里的「腾磁盘」步骤）" >&2
  exit 4
fi

# --------------------------------------------------------------------------- #
# 1. 下载
# --------------------------------------------------------------------------- #
log "下载 $ROM_URL"
aria2c -x16 -s16 -k1M --retry-wait=3 --max-tries=5 --console-log-level=warn \
  --file-allocation=none --auto-file-renaming=false \
  -d "$WORK/raw" -o "rom.bin" "$ROM_URL"

ROM="$WORK/raw/rom.bin"
SIZE=$(stat -c%s "$ROM")
log "下载完成 $(numfmt --to=iec "$SIZE")"
if [ "$SIZE" -lt 52428800 ]; then
  echo "包体小于 50M，多半下到了错误页/HTML，前 400 字节：" >&2
  head -c 400 "$ROM" >&2 || true
  exit 5
fi

# --------------------------------------------------------------------------- #
# 2. 判断包类型（只看文件头，不扫描整个包 —— 7G 的包用 tar -tf 会慢到超时）
# --------------------------------------------------------------------------- #
detect_kind() {
  local hex
  hex=$(head -c 8 "$ROM" | od -An -tx1 | tr -d ' \n')
  case "$hex" in
    504b0304*|504b0506*) echo zip;  return ;;
    1f8b*)               echo gzip; return ;;
  esac
  # tar 的 magic 在偏移 257 处
  if [ "$(dd if="$ROM" bs=1 skip=257 count=5 2>/dev/null)" = "ustar" ]; then
    echo tar; return
  fi
  # 三星 .tar.md5 也是 tar，只是尾部多一段 md5 文本
  echo unknown
}

KIND=$(detect_kind)
log "包类型: $KIND"

# --------------------------------------------------------------------------- #
# 3. 提取 —— 各策略把 img 丢到 work 下任意位置，第 4 步统一收
# --------------------------------------------------------------------------- #
case "$KIND" in
  zip)
    if unzip -l "$ROM" | grep -qiE '(^|/)payload\.bin$'; then
      log "A/B 包：先取 payload.bin 再按分区解"
      unzip -q -o "$ROM" 'payload.bin' -d "$WORK/ext" 2>/dev/null \
        || unzip -q -o "$ROM" '*/payload.bin' -d "$WORK/ext"
      PB=$(find "$WORK/ext" -name payload.bin -print -quit)
      [ -n "$PB" ] || { echo "zip 里没解出 payload.bin" >&2; exit 6; }
      # 只解需要的分区：否则 8G 的 super 直接撑爆磁盘
      if ! payload_dumper --partitions "$PARTITIONS" "$PB"; then
        log "含 init_boot 的解不了（老机型没有该分区），退回 boot,vbmeta"
        payload_dumper --partitions boot,vbmeta "$PB"
      fi
    else
      log "普通 zip：直接展开找 img"
      unzip -q -o "$ROM" -d "$WORK/ext" || true
    fi
    ;;
  gzip|tar)
    log "tar/tgz：展开后找 img"
    tar -xf "$ROM" -C "$WORK/ext" 2>/dev/null \
      || tar -xf "$ROM" -C "$WORK/ext" --wildcards '*.img*' 2>/dev/null \
      || true
    ;;
  *)
    echo "无法识别的包类型（ozip 等加密格式请先解密）" >&2
    exit 7
    ;;
esac

# --------------------------------------------------------------------------- #
# 4. 收集 img —— payload_dumper 的输出目录随版本变化，靠 find 兜底
# --------------------------------------------------------------------------- #
log "收集分区镜像"
FOUND=0
for name in boot.img init_boot.img vbmeta.img vendor_boot.img; do
  f=$(find "$WORK" -name "$name" -not -path "$WORK/out/*" -print -quit 2>/dev/null || true)
  if [ -n "$f" ]; then
    cp -f "$f" "$WORK/out/$name"
    FOUND=$((FOUND + 1))
    log "  + $name ($(numfmt --to=iec "$(stat -c%s "$f")"))"
  fi
done

# .lz4 压缩的 img 顺手解开（部分三星/联想包）
while IFS= read -r -d '' lz; do
  base=$(basename "$lz" .lz4)
  if lz4 -d -f "$lz" "$WORK/out/$base" 2>/dev/null; then
    FOUND=$((FOUND + 1))
    log "  + $base (lz4)"
  fi
done < <(find "$WORK/ext" -name '*.img.lz4' -print0 2>/dev/null || true)

if [ "$FOUND" -eq 0 ]; then
  echo "没提取到任何 boot 分区。work 下前 30 个文件：" >&2
  find "$WORK" -type f | head -30 >&2
  exit 8
fi

# --------------------------------------------------------------------------- #
# 5. 加密打包
# --------------------------------------------------------------------------- #
cd "$WORK/out"
ZIP_NAME="${ROM_NAME}.zip"
log "打包 $ZIP_NAME"
if [ -n "$PASS" ]; then
  7z a -tzip -p"$PASS" "$ZIP_NAME" ./*.img >/dev/null
else
  7z a -tzip "$ZIP_NAME" ./*.img >/dev/null
fi
if [ -n "$PASSFILE" ] && [ -f "$PASSFILE" ]; then
  7z a -tzip "$ZIP_NAME" "$PASSFILE" >/dev/null
fi
ls -lh "$ZIP_NAME"

# --------------------------------------------------------------------------- #
# 6. 上传 123 + 元数据（站点可以直接拿 json 当索引）
# --------------------------------------------------------------------------- #
TARGET="$DEST/$BRAND/$MODEL"
log "上传 $TARGET/"
rclone copy "$ZIP_NAME" "$TARGET/" --retries 3 --low-level-retries 5 --stats-one-line

python3 - "$ZIP_NAME" "$ROM_URL" "$WORK/out" "$BRAND" "$MODEL" "$VERSION" "$ROM_NAME" \
  > "${ROM_NAME}.json" <<'PY'
import json, os, sys, time
zip_name, src, outdir, brand, model, version, name = sys.argv[1:8]
print(json.dumps({
    "brand": brand,
    "model": model,
    "version": version,
    "name": name,
    "zip": zip_name,
    "zip_bytes": os.path.getsize(os.path.join(outdir, zip_name)),
    "images": sorted(f for f in os.listdir(outdir) if f.endswith(".img")),
    "source_url": src,
    "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, ensure_ascii=False, indent=1))
PY

rclone copy "${ROM_NAME}.json" "$TARGET/" --retries 3 --stats-one-line

log "完成 $ROM_NAME"
