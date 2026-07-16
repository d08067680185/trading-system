#!/bin/bash
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

REPO_DIR="/Users/xiaofengdai/Documents/claude/trading-system"
LOG_FILE="$REPO_DIR/auto-pull.log"

# ── 并发锁：构建期间后续 cron 直接退出，防连续 push 叠加并发构建压垮整机
#    （2026-07-16 japanese 事故：5 个 build 并发、load 9.8、全站超时）
LOCK_DIR="/tmp/trading-autopull.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    lock_age=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
    if [ "$lock_age" -lt 7200 ]; then exit 0; fi
    rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR" || exit 0
fi
trap 'rm -rf "$LOCK_DIR"' EXIT
LOCAL_PORT=8091

cd "$REPO_DIR"

# 日志自截断：超过 2000 行时只保留最后 500 行，防止无限增长
if [ -f "$LOG_FILE" ] && [ "$(wc -l < "$LOG_FILE")" -gt 2000 ]; then
    tail -n 500 "$LOG_FILE" > "$LOG_FILE.tmp" && mv "$LOG_FILE.tmp" "$LOG_FILE"
fi

# 静默 fetch：无更新时不写日志（此前每分钟 2 行 "From github" 噪音）
if ! FETCH_OUT=$(git fetch origin main --quiet 2>&1); then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] git fetch 失败: $FETCH_OUT" >> "$LOG_FILE"
    "$HOME/bin/kids-alert.sh" trading-fetch "trading git fetch 失败（认证或网络断链）" "$(tail -3 "$LOG_FILE")"
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
    "$HOME/bin/kids-alert.sh" trading-pull "trading git pull 失败（工作树脏或分叉）" "$(tail -3 "$LOG_FILE")"
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

# 等待健康检查通过（最多 120 秒 — 首次启动可能要重建 DB 索引）
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 等待容器健康..." >> "$LOG_FILE"
for i in $(seq 1 60); do
    sleep 2
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 4 "http://127.0.0.1:${LOCAL_PORT}/health" 2>/dev/null)
    if [ "$code" = "200" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ 更新完成，服务健康 (用时 $((i*2))s)" >> "$LOG_FILE"
        exit 0
    fi
done

# 120 秒后仍不健康 → 告警
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ 容器启动超时，服务可能不可用，请检查 docker logs" >> "$LOG_FILE"
osascript -e 'display notification "trading 更新后容器启动超时，请检查 docker logs" with title "自动同步告警" sound name "Basso"' 2>/dev/null || true
exit 1
