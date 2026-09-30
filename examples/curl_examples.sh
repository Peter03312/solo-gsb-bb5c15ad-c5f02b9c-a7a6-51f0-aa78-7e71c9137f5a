#!/usr/bin/env bash
# 场馆疏散通道编排服务 —— curl 调用示例。
# 前置：服务已启动（docker compose up 或本机 uvicorn），监听 127.0.0.1:8000
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"

echo "== 1. 健康检查 =="
curl -sS "$BASE/health"; echo

echo "== 2. 查询初始编排（首次启动自动播种的演示场景）=="
curl -sS "$BASE/api/plan" | python3 -m json.tool

echo "== 3. 整体下发自定义场景（时钟归零、全量重排）=="
curl -sS -X POST "$BASE/api/scenario" \
  -H 'Content-Type: application/json' \
  -d '{
    "edges": [
      {"id": 1, "source": "A", "target": "F", "travel_minutes": 2, "capacity_per_minute": 5},
      {"id": 2, "source": "A", "target": "F", "travel_minutes": 4, "capacity_per_minute": 10}
    ],
    "exits": [{"id": 1, "node": "F"}],
    "closures": [{"edge_id": 1, "start_minute": 2, "end_minute": 5}],
    "groups": [
      {"id": 1, "people": 3, "source": "A", "earliest_departure": 0, "priority": 10, "deadline": 30},
      {"id": 2, "people": 4, "source": "A", "earliest_departure": 0, "priority": 5,  "deadline": 30}
    ]
  }' | python3 -m json.tool

echo "== 4. 时钟推进 1 分钟（已出发组路线/占用固定）=="
curl -sS -X POST "$BASE/api/clock/advance" \
  -H 'Content-Type: application/json' -d '{"minutes": 1}' | python3 -m json.tool

echo "== 5. 新增封闭：触及已出发组剩余路线 -> 期望 HTTP 409，整次拒绝 =="
curl -sS -X POST "$BASE/api/closures" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 1, "end_minute": 4}' ; echo

echo "== 6. 新增人群组（可单对象，也可传数组；仅重排未出发组）=="
curl -sS -X POST "$BASE/api/groups" \
  -H 'Content-Type: application/json' \
  -d '{"id": 7, "people": 2, "source": "A", "earliest_departure": 0, "priority": 8, "deadline": 30}' \
  | python3 -m json.tool

echo "== 7. 删除未出发组 =="
curl -sS -X DELETE "$BASE/api/groups/2" | python3 -m json.tool

echo "== 8. 恢复内置演示场景 =="
curl -sS -X POST "$BASE/api/reset" >/dev/null && echo "reset ok"
