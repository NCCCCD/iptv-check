#!/bin/sh
set -e

CONFIG_DIR="/root/.config/iptv-check"
CONFIG_FILE="$CONFIG_DIR/config.json"

# 首次运行：从环境变量生成配置
if [ ! -f "$CONFIG_FILE" ] && [ -n "$M3U_URL" ]; then
    mkdir -p "$CONFIG_DIR"
    cat > "$CONFIG_FILE" <<EOF
{
  "m3u_url": "$M3U_URL",
  "repo_url": "${REPO_URL:-}"
}
EOF
    echo "[entrypoint] 已从环境变量生成 config.json"
fi

# 如果提供了 GITHUB_TOKEN，写入配置（不覆盖已有 token）
if [ -n "$GITHUB_TOKEN" ]; then
    mkdir -p "$CONFIG_DIR"
    if [ -f "$CONFIG_FILE" ]; then
        python3 -c "
import json
with open('$CONFIG_FILE') as f:
    cfg = json.load(f)
cfg.setdefault('github_token', '$GITHUB_TOKEN')
with open('$CONFIG_FILE', 'w') as f:
    json.dump(cfg, f, indent=2)
" 2>/dev/null
    else
        echo "{\"github_token\": \"$GITHUB_TOKEN\"}" > "$CONFIG_FILE"
    fi
    echo "[entrypoint] GITHUB_TOKEN 已写入配置"
fi

# 单次运行模式
if [ "$1" = "run" ]; then
    echo "[entrypoint] 单次运行模式"
    cd /app
    exec python3 iptv-check.py
fi

# 交互模式（手动调试）
if [ "$1" = "shell" ]; then
    echo "[entrypoint] 交互模式"
    exec /bin/sh
fi

# 定时运行模式（默认）
LOGFILE="/var/log/iptv-check.log"
touch "$LOGFILE"
echo "[entrypoint] 定时模式，调度: $CRON_SCHEDULE"

# 启动时立即执行一次（重启 / 更新后自动跑一轮），不阻塞定时任务
echo "[entrypoint] 启动立即执行..."
cd /app && python3 iptv-check.py >> "$LOGFILE" 2>&1 || true

# cron 输出写入文件，tail 实时推送到 stdout（Docker 日志）
echo "$CRON_SCHEDULE cd /app && python3 iptv-check.py >> $LOGFILE 2>&1" | crontab -
crond

# 前台 tail 日志文件，所有输出实时显示在 dockge / docker logs 中
exec tail -f -n +1 "$LOGFILE"
