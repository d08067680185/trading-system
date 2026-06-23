#!/bin/bash
# Mac mini 上运行：每2分钟检测 GitHub 是否有新提交，有则自动更新重启
# 由 launchd 调用，见 launchd/com.trading.updater.plist

cd ~/trading-system

# 拉取最新信息（不合并）
git fetch origin main --quiet 2>&1

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0  # 无更新
fi

echo "$(date): 检测到新版本 ${LOCAL:0:7} → ${REMOTE:0:7}，开始更新..." >> logs/auto_update.log

# 拉取代码
git pull origin main --quiet >> logs/auto_update.log 2>&1

# 安装新依赖（requirements.txt 有变化时）
venv/bin/pip install -r requirements.txt --quiet >> logs/auto_update.log 2>&1

# 停止旧进程
pkill -f "main.py" 2>/dev/null
sleep 3

# 重新启动
nohup venv/bin/python main.py >> logs/trading.log 2>&1 &
echo $! > /tmp/trading.pid

echo "$(date): 更新完成，新 PID=$(cat /tmp/trading.pid)" >> logs/auto_update.log
