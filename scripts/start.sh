#!/bin/bash
# 启动交易系统（Mac mini 上使用）
cd "$(dirname "$0")/.."
nohup venv/bin/python main.py >> logs/trading.log 2>&1 &
echo $! > /tmp/trading.pid
echo "$(date): 服务已启动 PID=$(cat /tmp/trading.pid)" >> logs/auto_update.log
