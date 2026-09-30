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
  5. 封闭预演：不写状态，返回确认凭据、拟执行后完整编排与逐组变化
  6. 凭据确认：同版本原子提交；期间发生变更则凭据失效（409）
  7. 恢复内置演示场景
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


def print_diff(title: str, preview: dict) -> None:
    print(f"\n=== {title}（绑定状态版本 {preview['state_version']}）===")
    for d in preview["changes"]:
        if not d["changed"]:
            continue
        bits = []
        for field, pair in d["changes"].items():
            bits.append(f"{field}: {pair['before']} -> {pair['after']}")
        flag = "  [已出发，固定]" if d["departed"] else ""
        print(f"  组{d['id']:<2}{flag}  " + "；".join(bits))


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

    # 6) 预演一批封闭（dry-run）：沿用 POST /api/closures 的字段与左闭右开规则，
    #    单对象或数组均可；不写任何状态。
    #    时钟已推进到第 1 分钟，组1 已出发（通道1 剩余占用第 1 分钟），
    #    故通道1 只能取边界相接的 [2, 8)；通道2 取 [4, 8)（组1 不走边2）。
    code, preview = call("POST", f"{base}/api/closures/preview", [
        {"edge_id": 1, "start_minute": 2, "end_minute": 8},
        {"edge_id": 2, "start_minute": 4, "end_minute": 8},
    ])
    assert code == 200, (code, preview)
    token = preview["credential"]["token"]
    print(f"\n=== 预演凭据：token={token[:12]}... 绑定版本 "
          f"{preview['credential']['state_version']} ===")
    print("    预演分配的封闭编号：",
          [(c["id"], c["edge_id"], f"[{c['start_minute']},{c['end_minute']})")
           for c in preview["credential"]["closures"]])
    print_plan("拟执行后的完整编排", preview["plan"])
    print_diff("逐组相对现状的变化（出发/路线/到达/受阻原因）", preview)

    # 预演不写状态：再查一次现状，编排与预演前一致
    code, current = call("GET", f"{base}/api/plan")
    assert current["groups"] == plan["groups"], "预演意外修改了状态"

    # 演示凭据失效：确认前先推进时钟，状态版本前进 -> 确认被拒且不写入
    code, _ = call("POST", f"{base}/api/clock/advance", {"minutes": 1})
    assert code == 200
    code, body = call("POST", f"{base}/api/closures/confirm", {"token": token})
    print(f"\n=== 期间时钟推进后的确认 -> HTTP {code} ===")
    print("   ", body["detail"])
    assert code == 409 and body["error"] == "credential_invalid"

    # 重新预演并立即确认：同一原子操作内核对内容与版本后重算提交。
    # now=2：组4（dep1 通行4分）仍占用边2 的 2~4 分钟，边2 封闭须从 5 起。
    code, preview = call("POST", f"{base}/api/closures/preview", [
        {"edge_id": 1, "start_minute": 3, "end_minute": 8},
        {"edge_id": 2, "start_minute": 5, "end_minute": 12},
    ])
    assert code == 200, (code, preview)
    print_diff("重新预演：逐组变化（此时组4 亦已出发、路线固定，故无变化）", preview)
    changed = [d for d in preview["changes"] if d["changed"]]
    assert changed == []
    code, committed = call("POST", f"{base}/api/closures/confirm",
                           {"token": preview["credential"]["token"]})
    assert code == 200, (code, committed)
    assert committed["groups"] == preview["plan"]["groups"]
    print_plan("凭据确认提交完成（提交结果与预演一致）", committed)

    # 凭据一次性：再次使用 -> 404 credential_not_found
    code, body = call("POST", f"{base}/api/closures/confirm",
                      {"token": preview["credential"]["token"]})
    assert code == 404 and body["error"] == "credential_not_found"
    print(f"\n重复确认 -> HTTP {code}（{body['error']}）")

    # 7) 恢复内置演示场景
    code, plan = call("POST", f"{base}/api/reset", {})
    assert code == 200
    print_plan("已恢复内置演示场景", plan)


if __name__ == "__main__":
    base_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    main(base_url.rstrip("/"))
