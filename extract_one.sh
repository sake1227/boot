#!/usr/bin/env bash
# extract_one.sh —— 单个 ROM：优先「远程按需提取」，失败再回退整包下载
#
# 核心：5ec1cff/payload-dumper 支持直接把 URL 交给它，只按 Range 取需要的分片，
# 不需要下载整包（8G -> 几十 MB），这是主路径。
# 非 payload 包（tgz / 直接放分区 img 的 zip / tar.md5）才走下载回退。
#
# 入参（环境变量）
#   ROM_URL / RESOLVE_CMD   直链 或 现场解析命令
#   ROM_NAME                产物名（版本号，无扩展名）
#   REFERER                 下载/提取需要的 Referer（魅族 CDN 强制）
#   BRANDCN CAT DEVNAME     网盘目录：品牌/分类/机型
#   PARTITIONS              默认 boot,init_boot,vbmeta
#   UNZIP_PASSWORD          设置后加密打包
#   PASSFILE                一起打进压缩包的说明文件
#   RCLONE_DEST             默认 123:zanfang
#   NO_REMOTE=1             强制走整包下载（调试用）
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
BRANDCN="${BRANDCN:-其他}"
CAT="${CAT:-其他}"
DEVNAME="${DEVNAME:-$MODEL}"
VERSION="${VERSION:-}"
REFERER="${REFERER:-}"
PARTITIONS="${PARTITIONS:-boot,init_boot,vbmeta}"
DEST="${RCLONE_DEST:-123:zanfang}"
PASS="${UNZIP_PASSWORD:-}"
PASSFILE="${PASSFILE:-}"
NO_REMOTE="${NO_REMOTE:-0}"
TARGETS="${TARGETS:-}"             # 多目标：品牌|分类|机型;品牌|分类|机型（通用机型用）
OPTIONAL="${OPTIONAL:-0}"          # 解析不到就静默跳过（欧加分支探测）
WORK="$(pwd)/work"
REPO_ROOT="$(pwd)"

UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"

log() { printf '\n==> %s\n' "$*"; }

mkdir -p "$WORK/raw" "$WORK/out" "$WORK/ext" "$WORK/dump"
TARGET="$DEST/$BRANDCN/$CAT/$DEVNAME"

# --------------------------------------------------------------------------- #
# A. 动态链接现场解析（欧加 / 魅族）
# --------------------------------------------------------------------------- #
if [ -n "$RESOLVE_CMD" ]; then
  log "解析直链（动态链接）"
  OUT=""
  for attempt in 1 2 3; do
    OUT=$( cd "$REPO_ROOT" && bash -c "$RESOLVE_CMD" 2>&1 ) || true
    URLS=$(printf '%s' "$OUT" |
      grep -oE 'https?://[^ "'"'"'<>]+\.(zip|ozip)(\?[^ "'"'"'<>]*)?' || true)
    ROM_URL=$(printf '%s\n' "$URLS" | grep -E 'Expires=|sign=' | tail -1 || true)
    if [ -z "${ROM_URL:-}" ]; then
      ROM_URL=$(printf '%s\n' "$URLS" | tail -1 || true)
    fi
    if [ -n "${ROM_URL:-}" ]; then break; fi
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
    if [ "$OPTIONAL" = "1" ]; then
      log "该分支没有可用包（optional），跳过"
      exit 0
    fi
    exit 9
  fi
  log "已解析: $(basename "${ROM_URL%%\?*}")"

  VLINE=$(printf '%s' "$OUT" \
    | grep -iE 'Version:' | grep -viE 'Ota[[:space:]]*Version' | tail -1 || true)
  if [ -n "${VLINE:-}" ]; then
    NEWNAME=$(printf '%s' "$VLINE" | sed -E 's/^[^:]*:[[:space:]]*//; s/[[:space:]]+$//')
    NEWNAME=$(printf '%s' "$NEWNAME" | sed -E 's#[/\\:*?"<>|]+#_#g; s/[[:space:]]+/_/g')
    if [ -n "$NEWNAME" ]; then
      ROM_NAME="$NEWNAME"
      log "版本号作为文件名: ${ROM_NAME}.zip"
    fi
  fi
fi

# --------------------------------------------------------------------------- #
# B. 多目标查重：通用机型要传到多个目录，只传缺的那些
# --------------------------------------------------------------------------- #
if [ -z "$TARGETS" ]; then
  TARGETS="$BRANDCN|$CAT|$DEVNAME"
fi
UPLOAD_TARGETS=()
IFS=';' read -ra _tg <<< "$TARGETS"
for _one in "${_tg[@]}"; do
  [ -n "$_one" ] || continue
  IFS='|' read -r _b _c _d <<< "$_one"
  [ -n "${_d:-}" ] || continue
  _path="$DEST/$_b/$_c/$_d"
  if rclone lsf "$_path/" --files-only 2>/dev/null | grep -qxF "${ROM_NAME}.zip"; then
    log "已有 $_path/${ROM_NAME}.zip，跳过这个目录"
  else
    UPLOAD_TARGETS+=("$_path")
  fi
done
if [ "${#UPLOAD_TARGETS[@]}" -eq 0 ]; then
  log "所有目标目录都已有 ${ROM_NAME}.zip，跳过"
  exit 0
fi
log "将上传到 ${#UPLOAD_TARGETS[@]} 个目录：${UPLOAD_TARGETS[*]}"

# --------------------------------------------------------------------------- #
# C. 体积预检：小于 1G 的多半是增量/组件包，直接跳过
# --------------------------------------------------------------------------- #
probe_total() {
  curl -s -o /dev/null -D - -r 0-0 -A "$UA" ${REFERER:+-e "$REFERER"} "$1" 2>/dev/null \
    | tr -d '\r' | awk 'tolower($1)=="content-range:"{split($2,a,"/");print a[2];exit}'
}
TOTAL=$(probe_total "$ROM_URL" || true)
if [ -n "${TOTAL:-}" ] && [ "$TOTAL" -lt 1073741824 ]; then
  HUMAN=$(numfmt --to=iec "$TOTAL")
  log "包体只有 ${HUMAN}，判定为增量/组件包，跳过"
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "- SKIP \`${ROM_NAME}\`：仅 ${HUMAN}，疑似增量包" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 0
fi
[ -n "${TOTAL:-}" ] && log "远端包体 $(numfmt --to=iec "$TOTAL")"

# --------------------------------------------------------------------------- #
# 共用：收集 / 打包上传
# --------------------------------------------------------------------------- #
FOUND=0
collect_imgs() {
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
  while IFS= read -r -d '' lz; do
    base=$(basename "$lz" .lz4)
    if lz4 -d -f "$lz" "$WORK/out/$base" 2>/dev/null; then
      FOUND=$((FOUND + 1)); log "  + $base (lz4)"
    fi
  done < <(find "$WORK/ext" -name '*.img.lz4' -print0 2>/dev/null || true)
  return 0
}

package_and_upload() {
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
  for _path in "${UPLOAD_TARGETS[@]}"; do
    log "上传 $_path/"
    rclone copy "$ZIP_NAME" "$_path/" --retries 3 --low-level-retries 5 \
      --transfers 4 --checkers 16 --buffer-size 64M --stats-one-line
  done
  log "完成 ${ROM_NAME}.zip -> ${#UPLOAD_TARGETS[@]} 个目录"
}

# --------------------------------------------------------------------------- #
# D. 主路径：远程按需提取（不下载整包）
# --------------------------------------------------------------------------- #
REMOTE_OK=0
if [ "$NO_REMOTE" != "1" ]; then
  command -v payload_dumper >/dev/null 2>&1 || {
    echo "payload_dumper 没装（检查 workflow 里 pip install 那步）" >&2; exit 6; }
  log "远程提取：URL 直接交给 payload_dumper（只取需要的分片）"
  cd "$WORK/dump"
  if payload_dumper --partitions "$PARTITIONS" "$ROM_URL"; then
    REMOTE_OK=1
  else
    log "整体远程提取失败，逐个分区再试（包里没有的自动跳过）"
    IFS=',' read -ra _plist <<< "$PARTITIONS"
    for _p in "${_plist[@]}"; do
      _p=$(printf '%s' "$_p" | tr -d ' ')
      [ -n "$_p" ] || continue
      if payload_dumper --partitions "$_p" "$ROM_URL"; then
        REMOTE_OK=1; log "  ✓ $_p"
      else
        log "  - $_p 取不到，跳过"
      fi
    done
  fi
  if [ "$REMOTE_OK" = "1" ]; then
    cd "$WORK"
    collect_imgs
    if [ "$FOUND" -gt 0 ]; then
      log "远程提取成功，省掉了整包下载"
      package_and_upload
      exit 0
    fi
    log "远程提取没产出目标分区，回退整包下载"
  else
    log "远程提取不适用（可能不是 payload 包），回退整包下载"
  fi
  cd "$WORK"
fi

# --------------------------------------------------------------------------- #
# E. 回退：整包下载 + 本地提取
# --------------------------------------------------------------------------- #
log "回退：下载 $ROM_URL"
dl_args=()
if [ -n "$REFERER" ]; then
  dl_args+=(--referer="$REFERER")
  log "带上 Referer: $REFERER"
fi
aria2c -x16 -s16 -k1M --retry-wait=3 --max-tries=5 --console-log-level=warn \
  --file-allocation=none --auto-file-renaming=false --user-agent="$UA" \
  "${dl_args[@]}" \
  -d "$WORK/raw" -o "rom.bin" "$ROM_URL"

ROM="$WORK/raw/rom.bin"
SIZE=$(stat -c%s "$ROM")
log "下载完成 $(numfmt --to=iec "$SIZE")"
if [ "$SIZE" -lt 52428800 ]; then
  echo "包体小于 50M，多半下到了错误页/HTML，前 400 字节：" >&2
  head -c 400 "$ROM" >&2 || true
  exit 5
fi

detect_kind() {
  local hex
  hex=$(head -c 8 "$ROM" | od -An -tx1 | tr -d ' \n')
  case "$hex" in
    504b0304*|504b0506*) echo zip;  return ;;
    1f8b*)               echo gzip; return ;;
  esac
  if [ "$(dd if="$ROM" bs=1 skip=257 count=5 2>/dev/null)" = "ustar" ]; then
    echo tar; return
  fi
  echo unknown
}
KIND=$(detect_kind)
log "包类型: $KIND"

PB=""
case "$KIND" in
  zip)
    ENTRIES=$(unzip -Z1 "$ROM" 2>/dev/null || true)
    PB_ENTRY=$(printf '%s\n' "$ENTRIES" | grep -iE '(^|/)payload\.bin$' | head -1 || true)

    if [ -n "${PB_ENTRY:-}" ]; then
      log "有 payload.bin：本地解出后按分区解"
      unzip -o -q -j "$ROM" "$PB_ENTRY" -d "$WORK/ext"
      PB=$(find "$WORK/ext" -name payload.bin -print -quit)
      [ -n "$PB" ] || { echo "没解出 payload.bin" >&2; exit 6; }
      rm -f "$ROM" || true
      log "已删除原始包，释放 $(numfmt --to=iec "$SIZE")"
      if ! payload_dumper --partitions "$PARTITIONS" "$PB"; then
        log "整体解包失败，逐个分区尝试"
        IFS=',' read -ra _plist <<< "$PARTITIONS"
        for _p in "${_plist[@]}"; do
          _p=$(printf '%s' "$_p" | tr -d ' ')
          [ -n "$_p" ] || continue
          payload_dumper --partitions "$_p" "$PB" && log "  ✓ $_p" || log "  - $_p 跳过"
        done
      fi
    else
      log "分区 img 直接放在 zip 里：只挑需要的解"
      WANT=$(printf '%s\n' "$ENTRIES" \
        | grep -iE '(^|/)(boot|init_boot|vbmeta|vendor_boot)\.img(\.lz4)?$' || true)
      if [ -z "${WANT:-}" ]; then
        echo "zip 里既没有 payload.bin，也没有 boot/init_boot/vbmeta。前 50 个条目：" >&2
        printf '%s\n' "$ENTRIES" | head -50 >&2
        exit 6
      fi
      while IFS= read -r _e; do
        [ -n "$_e" ] || continue
        unzip -o -q "$ROM" "$_e" -d "$WORK/ext" && log "  + $_e"
      done <<< "$WANT"
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

cd "$WORK"
collect_imgs
if [ "$FOUND" -eq 0 ]; then
  echo "没提取到任何 boot 分区。" >&2
  if [ -n "$PB" ]; then
    echo "payload.bin 解完之后没有目标分区，实际解出来的 img：" >&2
    find "$WORK" -name '*.img' -not -path "$WORK/out/*" | head -30 >&2
  fi
  echo "work 下前 30 个文件：" >&2
  find "$WORK" -type f | head -30 >&2
  exit 8
fi

package_and_upload
