#!/usr/bin/env bash
# install.sh —— 在 Ubuntu/Debian 服务器上装齐依赖（上海/香港机器都适用）
set -euo pipefail

sudo apt-get update -qq
sudo apt-get install -y -qq aria2 p7zip-full unzip curl lz4 python3-pip git

# 支持「URL 直接提取、只拉需要的分片」的 payload dumper
python3 -m pip install --upgrade "git+https://github.com/5ec1cff/payload-dumper" \
  || python3 -m pip install --upgrade --break-system-packages "git+https://github.com/5ec1cff/payload-dumper"

# rclone
command -v rclone >/dev/null 2>&1 || curl -fsSL https://rclone.org/install.sh | sudo bash

echo
echo "检查："
for c in python3 rclone aria2c 7z unzip curl lz4 payload_dumper; do
  printf '  %-16s %s\n' "$c" "$(command -v "$c" || echo '缺失')"
done

cat <<'EOF'

下一步：
1) 配 123 的 rclone 远程（远程名必须是 123，目录默认 zanfang）
   rclone config
2) 把仓库放到服务器（git clone 或 scp 整个目录）
3) 先小范围试：
   BRAND=小米 ONLY=小米15 ./server/farm.sh reconcile 2
4) 挂定时任务（crontab -e）：
   20 2 * * *   cd /opt/boot && BRAND= ./server/farm.sh incremental 12 >> logs/cron.log 2>&1
   40 4 */3 * * cd /opt/boot && BRAND= ./server/farm.sh reconcile 200 >> logs/cron.log 2>&1
EOF
