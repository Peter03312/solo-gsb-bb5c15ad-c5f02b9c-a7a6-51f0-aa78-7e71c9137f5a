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

echo "== 8. 封闭预演（不写入状态；返回凭据 token、拟执行编排 plan、逐组变化 changes）=="
echo "    通道2 封闭 [4,8) 会把未出发的组7 由“通道2 第1分出发”挤到通道1 第8分出发"
PREVIEW=$(curl -sS -X POST "$BASE/api/closures/preview" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 2, "start_minute": 4, "end_minute": 8}')
echo "$PREVIEW" | python3 -m json.tool
TOKEN=$(echo "$PREVIEW" | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
echo "拿到确认凭据：$TOKEN"

echo "== 9. 预演未写入：计划状态版本未变 =="
curl -sS "$BASE/api/plan" | python3 -c 'import sys,json; print("state_version =", json.load(sys.stdin)["state_version"])'

echo "== 10. 凭凭据确认提交（原子内核验内容与版本并重算）=="
curl -sS -X POST "$BASE/api/closures/confirm" \
  -H 'Content-Type: application/json' \
  -d "{\"token\": \"$TOKEN\", \"closures\": [{\"edge_id\": 2, \"start_minute\": 4, \"end_minute\": 8}]}" \
  | python3 -m json.tool

echo "== 11. 凭据一次性：重复确认 -> 409 credential_invalid =="
curl -sS -X POST "$BASE/api/closures/confirm" \
  -H 'Content-Type: application/json' -d "{\"token\": \"$TOKEN\"}"; echo

echo "== 12. 版本失效：先预演，再推进时钟，确认被拒且不写入 =="
TOKEN2=$(curl -sS -X POST "$BASE/api/closures/preview" \
  -H 'Content-Type: application/json' \
  -d '{"edge_id": 1, "start_minute": 40, "end_minute": 50}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["token"])')
curl -sS -X POST "$BASE/api/clock/advance" \
  -H 'Content-Type: application/json' -d '{"minutes": 1}' >/dev/null
curl -sS -X POST "$BASE/api/closures/confirm" \
  -H 'Content-Type: application/json' -d "{\"token\": \"$TOKEN2\"}"; echo

echo "== 13. 恢复内置演示场景 =="
curl -sS -X POST "$BASE/api/reset" >/dev/null && echo "reset ok"
