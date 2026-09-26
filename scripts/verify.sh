#!/bin/sh
# 一次性验收：代码测试 -> 等待 Web/API 健康 -> API 业务冒烟。
# 任一环节失败即以非零退出码结束（Compose 中 verify 服务据此报告验收结果）。
set -eu

cd /app

echo "==> [verify] 1/2 运行代码测试"
python -m pytest -q

echo "==> [verify] 2/2 API 业务冒烟（目标 ${BASE_URL}）"
exec python scripts/smoke.py
