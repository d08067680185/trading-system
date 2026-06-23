#!/bin/bash
# 本地使用：构建前端 + 提交 + 推送到 GitHub
# 用法：bash scripts/deploy.sh "提交说明"

set -e

MSG="${1:-update}"

echo "▶ 构建前端..."
cd frontend && npm run build && cd ..

echo "▶ 提交并推送..."
git add -A
git commit -m "$MSG" || echo "无变更，跳过提交"
git push origin main

echo "✅ 推送完成，Mac mini 将在2分钟内自动更新"
