#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO_DIR="/Users/xiaofengdai/Documents/claude/trading-system"
LOG_FILE="$REPO_DIR/auto-pull.log"
LOCAL_PORT=8091

cd "$REPO_DIR"

if ! git fetch origin main >> "$LOG_FILE" 2>&1; then
    "$HOME/bin/kids-alert.sh" trading-fetch "trading git fetch 失败（认证或网络断链）" "$(tail -3 \"$LOG_FILE\")"
    exit 1
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到新提交，开始更新 ($LOCAL -> $REMOTE)..." >> "$LOG_FILE"

if ! git pull --ff-only origin main >> "$LOG_FILE" 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ git pull 失败（可能有未提交改动或分叉），跳过本次同步" >> "$LOG_FILE"
    osascript -e 'display notification "trading git pull 失败，请检查工作树" with title "自动同步告警" sound name "Basso"' 2>/dev/null || true
    "$HOME/bin/kids-alert.sh" trading-pull "trading git pull 失败（工作树脏或分叉）" "$(tail -3 \"$LOG_FILE\")"
    exit 1
fi

CHANGED=$(git diff --name-only "$LOCAL" "$REMOTE" 2>/dev/null || true)

# 仅文档变更无需重建
if ! echo "$CHANGED" | grep -qvE '\.(md|txt)$'; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 仅文档变更，跳过重建" >> "$LOG_FILE"
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 重建镜像..." >> "$LOG_FILE"
if ! docker compose build >> "$LOG_FILE" 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ docker compose build 失败，保持旧容器运行" >> "$LOG_FILE"
    osascript -e 'display notification "trading 构建失败，旧版本仍在运行，请检查 auto-pull.log" with title "自动同步告警" sound name "Basso"' 2>/dev/null || true
    exit 1
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 重启容器..." >> "$LOG_FILE"
docker compose up -d >> "$LOG_FILE" 2>&1

# 等待健康检查通过（最多 60 秒）
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 等待容器健康..." >> "$LOG_FILE"
for i in $(seq 1 30); do
    sleep 2
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "http://127.0.0.1:${LOCAL_PORT}/health" 2>/dev/null)
    if [ "$code" = "200" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ 更新完成，服务健康 (用时 $((i*2))s)" >> "$LOG_FILE"
        exit 0
    fi
done

# 60 秒后仍不健康 → 告警
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ 容器启动超时，服务可能不可用，请检查 docker logs" >> "$LOG_FILE"
osascript -e 'display notification "trading 更新后容器启动超时，请检查 docker logs" with title "自动同步告警" sound name "Basso"' 2>/dev/null || true
exit 1
