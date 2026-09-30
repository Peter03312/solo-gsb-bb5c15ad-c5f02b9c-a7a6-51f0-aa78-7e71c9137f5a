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
  -d '{"edge_id": 1, "start_minute": 1, "end_minute": 4}' || true ; echo

echo "== 6. 新增人群组（可单对象，也可传数组；仅重排未出发组）=="
curl -sS -X POST "$BASE/api/groups" \
  -H 'Content-Type: application/json' \
  -d '{"id": 7, "people": 2, "source": "A", "earliest_departure": 0, "priority": 8, "deadline": 30}' \
  | python3 -m json.tool

echo "== 7. 删除未出发组 =="
curl -sS -X DELETE "$BASE/api/groups/2" | python3 -m json.tool || true

echo "== 8. 预演一批封闭（dry-run，不写状态）=="
# 此时时钟已推进 1 分钟，组1（通道1, dep0, 占用{0,1}）与
# 组2（通道2, dep0, 占用{0,1,2,3}）均已出发；
# 新封闭取边界相接时段：通道1 [2,6)、通道2 [4,8)，均不触及剩余占用。
# 返回：credential 确认凭据（绑定批次内容与 state_version）、plan 拟执行后完整编排、
# changes 逐组出发/路线/到达/受阻原因相对现状的变化
PREVIEW=$(curl -sS -X POST "$BASE/api/closures/preview" \
  -H 'Content-Type: application/json' \
  -d '[{"edge_id": 1, "start_minute": 2, "end_minute": 6},
       {"edge_id": 2, "start_minute": 4, "end_minute": 8}]')
echo "$PREVIEW" | python3 -m json.tool

# 预演绝不写入：组1 仍已出发、封闭集仍为空
curl -sS "$BASE/api/plan" | python3 -c 'import sys,json; p=json.load(sys.stdin); print("预演后未写入，now =", p["now"])'

TOKEN=$(echo "$PREVIEW" | python3 -c 'import sys,json; print(json.load(sys.stdin)["credential"]["token"])')

echo "== 9. 凭据确认：同版本原子提交，响应与预演 plan 一致 =="
curl -sS -X POST "$BASE/api/closures/confirm" \
  -H 'Content-Type: application/json' -d "{\"token\": \"$TOKEN\"}" | python3 -m json.tool

echo "== 10. 凭据一次性 / 过期语义 =="
# 已使用的凭据 -> 404 credential_not_found
curl -sS -o /dev/null -w "重复确认 -> HTTP %{http_code}\n" -X POST "$BASE/api/closures/confirm" \
  -H 'Content-Type: application/json' -d "{\"token\": \"$TOKEN\"}" || true

# 先发一张凭据，再推进时钟（状态版本前进），确认被拒 409 credential_invalid 且不写入
TOKEN2=$(curl -sS -X POST "$BASE/api/closures/preview" \
  -H 'Content-Type: application/json' -d '{"edge_id": 2, "start_minute": 40, "end_minute": 50}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["credential"]["token"])')
curl -sS -X POST "$BASE/api/clock/advance" -H 'Content-Type: application/json' -d '{"minutes": 1}' >/dev/null
curl -sS -w "\n过期确认 -> HTTP %{http_code}\n" -X POST "$BASE/api/closures/confirm" \
  -H 'Content-Type: application/json' -d "{\"token\": \"$TOKEN2\"}" || true

echo "== 11. 预演触及已出发组剩余路线 -> 409 planning_conflict，不发凭据 =="
# 时钟已到第 2 分钟：组1（通道1, dep0 通行2分）无剩余占用；
# 组2（通道2, dep0 通行4分）仍占用第 2、3 分钟，封闭通道2 [2,3) 与其相交
curl -sS -w "\nHTTP %{http_code}\n" -X POST "$BASE/api/closures/preview" \
  -H 'Content-Type: application/json' -d '{"edge_id": 2, "start_minute": 2, "end_minute": 3}' || true

echo "== 12. 恢复内置演示场景 =="
curl -sS -X POST "$BASE/api/reset" >/dev/null && echo "reset ok"
