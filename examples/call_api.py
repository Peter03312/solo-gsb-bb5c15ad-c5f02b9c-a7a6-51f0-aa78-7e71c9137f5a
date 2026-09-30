#!/usr/bin/env python3
"""场馆疏散通道编排服务 —— 纯标准库调用示例（无需安装 requests）。

用法：
    python examples/call_api.py [BASE_URL]
默认 BASE_URL=http://127.0.0.1:8000

流程演示：
  1. 整体下发一个小场馆场景（通道/出口/封闭/人群组）
  2. 查询编排结果（路线、出发/到达分钟、无法安排的原因）
  3. 时钟推进，新增“触及已出发组剩余路线”的封闭 -> 409 整次拒绝
  4. 新增人群组，仅未出发组被重排
  5. 恢复内置演示场景
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def call(method: str, url: str, payload=None) -> tuple[int, dict]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def print_plan(title: str, plan: dict) -> None:
    print(f"\n=== {title}（当前分钟 now={plan['now']}）===")
    for g in plan["groups"]:
        if g["status"] == "scheduled":
            print(f"  组{g['id']:<2} {g['people']}人 优先级{g['priority']:>2}  "
                  f"路线{g['route']}  出发第{g['departure_minute']}分"
                  f"  到达第{g['arrival_minute']}分"
                  f"{'  [已出发]' if g['departed'] else ''}")
        else:
            print(f"  组{g['id']:<2} {g['people']}人 优先级{g['priority']:>2}  "
                  f"无法安排：{g['reason']}（{g['reason_text']}）")


def main(base: str) -> None:
    print("服务健康检查：", call("GET", f"{base}/health"))

    # 1) 整体场景：
    #    1 A->F 2分钟 cap5；2 A->F 4分钟 cap10；3 F 出口
    #    通道1 在 [2,5) 封闭
    scenario = {
        "edges": [
            {"id": 1, "source": "A", "target": "F",
             "travel_minutes": 2, "capacity_per_minute": 5},
            {"id": 2, "source": "A", "target": "F",
             "travel_minutes": 4, "capacity_per_minute": 10},
        ],
        "exits": [{"id": 1, "node": "F"}],
        "closures": [
            {"edge_id": 1, "start_minute": 2, "end_minute": 5},
        ],
        "groups": [
            {"id": 1, "people": 3, "source": "A", "earliest_departure": 0,
             "priority": 10, "deadline": 30},
            {"id": 2, "people": 4, "source": "A", "earliest_departure": 0,
             "priority": 5, "deadline": 30},
            {"id": 3, "people": 8, "source": "A", "earliest_departure": 0,
             "priority": 1, "deadline": 6},
        ],
    }
    code, plan = call("POST", f"{base}/api/scenario", scenario)
    assert code == 200, (code, plan)
    print_plan("初始编排（组1/组2 错峰走通道1；组3 容量不足绕通道2，截止前来不及）", plan)

    # 2) 时钟推进到第 1 分钟：组1 已出发，路线固定
    code, plan = call("POST", f"{base}/api/clock/advance", {"minutes": 1})
    print_plan("时钟推进 1 分钟后", plan)

    # 3) 新增封闭 [1,4) 于通道1：组1 dep0 占用通道1 的分钟 {0,1}，剩余分钟 1 被触及
    code, body = call("POST", f"{base}/api/closures",
                      {"edge_id": 1, "start_minute": 1, "end_minute": 4})
    print(f"\n=== 触及已出发组剩余路线的封闭 -> HTTP {code} ===")
    print("   ", body["detail"])

    # 4) 不触及剩余路线的未来封闭 [9,12) 可以接受；同时新增人群组
    code, plan = call("POST", f"{base}/api/closures",
                      {"edge_id": 1, "start_minute": 9, "end_minute": 12})
    assert code == 200, (code, plan)
    code, plan = call("POST", f"{base}/api/groups",
                      {"id": 4, "people": 2, "source": "A",
                       "earliest_departure": 0, "priority": 8, "deadline": 30})
    assert code == 200, (code, plan)
    print_plan("新增通道1封闭[9,12) 与高优先级组4 后（仅未出发组重排）", plan)

    # 5) 恢复内置演示场景
    code, plan = call("POST", f"{base}/api/reset", {})
    assert code == 200
    print_plan("已恢复内置演示场景", plan)


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    main(base_url.rstrip("/"))
