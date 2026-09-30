"""HTTP API 与并发原子性测试（FastAPI TestClient + 临时 SQLite）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading

import pytest

# 在导入应用前指定临时数据库
_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
_tmp.close()
os.environ["EVAC_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.schemas import ClosureIn, EdgeIn, ExitIn, GroupIn, ScenarioIn  # noqa: E402


SCENARIO = ScenarioIn(
    edges=[
        EdgeIn(id=1, source="A", target="F", travel_minutes=2, capacity_per_minute=2),
        EdgeIn(id=2, source="A", target="F", travel_minutes=5, capacity_per_minute=10),
    ],
    exits=[ExitIn(id=1, node="F")],
    closures=[],
    groups=[
        GroupIn(id=1, people=2, source="A", earliest_departure=0, priority=10, deadline=20),
        GroupIn(id=2, people=2, source="A", earliest_departure=0, priority=5, deadline=20),
        GroupIn(id=3, people=2, source="A", earliest_departure=0, priority=1, deadline=3),
    ],
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset(client):
    client.post("/api/scenario", json=json.loads(SCENARIO.model_dump_json()))
    yield


def _groups(resp) -> dict:
    return {g["id"]: g for g in resp.json()["groups"]}


def test_health_and_seed(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_initial_plan_is_greedy_by_priority(client):
    groups = _groups(client.get("/api/plan"))
    # 边1 cap2/分钟、通行2分钟：g1 dep0 占用 {0,1}；g2 dep2 占用 {2,3}；
    # g3 最晚 dep=1（arrival3<=3）但 0、1 都被占 → 走边2(5分钟)来不及（arrival=5>3）→ blocked
    assert groups[1]["route"] == [1]
    assert groups[1]["departure_minute"] == 0
    assert groups[2]["departure_minute"] == 2
    assert groups[3]["status"] == "blocked"
    assert groups[3]["reason"] == "no_capacity_before_deadline"


def test_group_within_capacity_can_share_minute(client):
    client.post("/api/scenario", json=json.loads(ScenarioIn(
        edges=[EdgeIn(id=1, source="A", target="F", travel_minutes=1, capacity_per_minute=10)],
        exits=[ExitIn(id=1, node="F")],
        groups=[GroupIn(id=1, people=4, source="A", earliest_departure=0, priority=1, deadline=10),
                GroupIn(id=2, people=6, source="A", earliest_departure=0, priority=1, deadline=10)],
    ).model_dump_json()))
    groups = _groups(client.get("/api/plan"))
    assert groups[1]["departure_minute"] == 0
    assert groups[2]["departure_minute"] == 0  # 4+6=10 恰好满载


def test_closure_addition_atomic_and_fixed_conflict(client):
    # 推进到第 1 分钟：g1（dep0, 通行2分钟）已出发，剩余占用分钟 {1}
    client.post("/api/clock/advance", json={"minutes": 1})
    groups = _groups(client.get("/api/plan"))
    assert groups[1]["departed"] is True

    # 封闭触及 g1 剩余路线 → 409，整次拒绝
    resp = client.post("/api/closures", json={"edge_id": 1, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 409
    assert "整次变更拒绝" in resp.json()["detail"]

    # 同请求还带了另一条合法封闭，但因整次原子性，两者都不能落库
    resp = client.post("/api/closures", json=[
        {"edge_id": 1, "start_minute": 1, "end_minute": 3},
        {"edge_id": 1, "start_minute": 90, "end_minute": 95},
    ])
    assert resp.status_code == 409
    plan = client.get("/api/plan").json()
    assert plan["now"] == 1
    assert len([g for g in plan["groups"]]) == 3  # 数据未被部分修改

    # 不触及剩余路线（只覆盖过去 [0,1)）→ 接受，并触发未出发组重排
    resp = client.post("/api/closures", json={"edge_id": 1, "start_minute": 0, "end_minute": 1})
    assert resp.status_code == 200


def test_future_closure_replans_undeparted(client):
    # 封闭 [1,3) 边1（通行2分钟）：dep0 占用{0,1} 撞封闭；
    # 边1 最早 dep3（占用{3,4}，到达5）；边2 dep0 到达5（同到达，序列 [1] 更优）。
    # g1 优先级最高 → 边1 dep3；g2 随后取边2 dep0（到达同为5）。
    resp = client.post("/api/closures", json={"edge_id": 1, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 200
    groups = _groups(resp)
    assert groups[1]["route"] == [1]
    assert groups[1]["departure_minute"] == 3
    assert groups[2]["route"] == [2]
    assert groups[2]["departure_minute"] == 0


def test_delete_closure_frees_capacity(client):
    # 全新场景，封闭 id 自增从 1 开始：唯一封闭解除后出发时间应提前
    client.post("/api/scenario", json=json.loads(ScenarioIn(
        edges=[EdgeIn(id=1, source="A", target="F", travel_minutes=1, capacity_per_minute=5)],
        exits=[ExitIn(id=1, node="F")],
        closures=[ClosureIn(edge_id=1, start_minute=0, end_minute=10)],
        groups=[GroupIn(id=1, people=1, source="A", earliest_departure=0, priority=1, deadline=20)],
    ).model_dump_json()))
    groups = _groups(client.get("/api/plan"))
    assert groups[1]["departure_minute"] == 10  # 被封闭推迟
    resp = client.delete("/api/closures/1")
    assert resp.status_code == 200
    groups = _groups(resp)
    assert groups[1]["departure_minute"] == 0  # 封闭解除后提前

    assert client.delete("/api/closures/999").status_code == 400


def test_add_group_and_duplicate_rejected(client):
    resp = client.post("/api/groups", json=GroupIn(
        id=9, people=1, source="A", earliest_departure=0, priority=1, deadline=20
    ).model_dump())
    assert resp.status_code == 200
    assert 9 in _groups(resp)

    resp = client.post("/api/groups", json={"id": 9, "people": 1, "source": "A",
                                            "earliest_departure": 0, "priority": 1, "deadline": 20})
    assert resp.status_code == 400


def test_delete_departed_group_rejected(client):
    client.post("/api/clock/advance", json={"minutes": 1})
    resp = client.delete("/api/groups/1")
    assert resp.status_code == 409
    assert client.delete("/api/groups/3").status_code == 200  # 未出发可删，随后重排


def test_batch_group_add_is_atomic(client):
    # 批量新增中一个编号重复 -> 整批 400，另一个合法组也不落库
    resp = client.post("/api/groups", json=[
        {"id": 50, "people": 1, "source": "A", "earliest_departure": 0,
         "priority": 1, "deadline": 20},
        {"id": 1, "people": 1, "source": "A", "earliest_departure": 0,
         "priority": 1, "deadline": 20},  # 已存在
    ])
    assert resp.status_code == 400
    groups = _groups(client.get("/api/plan"))
    assert 50 not in groups and 1 in groups


def test_validation_errors(client):
    # 左闭右开要求 end > start
    resp = client.post("/api/closures", json={"edge_id": 1, "start_minute": 5, "end_minute": 5})
    assert resp.status_code == 422
    # 引用不存在的通道
    resp = client.post("/api/closures", json={"edge_id": 99, "start_minute": 0, "end_minute": 2})
    assert resp.status_code == 400


def test_concurrent_mutations_never_oversell_capacity(client):
    # 窄通道 cap=3，10 个并发请求各加 1 人组，deadline 相同；
    # 结果必须满足：每分钟合计 <= 3，且每个被安排的组路线/占用合法
    client.post("/api/scenario", json=json.loads(ScenarioIn(
        edges=[EdgeIn(id=1, source="A", target="F", travel_minutes=1, capacity_per_minute=3)],
        exits=[ExitIn(id=1, node="F")],
        groups=[],
    ).model_dump_json()))

    errors = []

    def add(i):
        r = client.post("/api/groups", json={"id": 100 + i, "people": 1, "source": "A",
                                             "earliest_departure": 0, "priority": 1, "deadline": 50})
        if r.status_code != 200:
            errors.append((i, r.status_code, r.text))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    groups = _groups(client.get("/api/plan"))
    assert len(groups) == 10
    # 容量校验：通行1分钟，同出发分钟人数 <= 3
    by_minute: dict[int, int] = {}
    for g in groups.values():
        assert g["status"] == "scheduled"
        assert g["route"] == [1]
        by_minute[g["departure_minute"]] = by_minute.get(g["departure_minute"], 0) + 1
    assert all(v <= 3 for v in by_minute.values())
    # 每组出发分钟应铺满 0,1,2,3（每分钟3个，最后一个在3）
    assert sum(by_minute.values()) == 10


# ------------------------------------------------------------ 封闭预演与凭据确认

def _preview(client, payload):
    return client.post("/api/closures/preview", json=payload)


def test_preview_does_not_write_state_and_reports_diff(client):
    # 现状：g1 dep0 走边1（2分钟）；g2 dep2 走边1；g3 blocked
    before = client.get("/api/plan").json()

    # 预演在边1上加封闭 [0,2)：g1 占用 {0,1} 撞封闭 -> 边1 dep2 到达4；
    # g2 比较边1 dep4 到达6 与边2 dep0 到达5，改走边2
    resp = _preview(client, {"edge_id": 1, "start_minute": 0, "end_minute": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["state_version"] >= 1
    token = body["credential"]["token"]
    assert token and isinstance(token, str)
    # 凭据回显绑定的批次内容（自动分配的编号）
    assert body["credential"]["closures"] == [
        {"id": 1, "edge_id": 1, "start_minute": 0, "end_minute": 2}
    ]
    assert body["credential"]["state_version"] == body["state_version"]

    # 拟执行后的完整编排
    pg = {g["id"]: g for g in body["plan"]["groups"]}
    assert pg[1]["route"] == [1]
    assert pg[1]["departure_minute"] == 2
    assert pg[2]["route"] == [2]
    assert pg[2]["departure_minute"] == 0

    # 逐组变化
    diffs = {d["id"]: d for d in body["changes"]}
    assert diffs[1]["changed"] is True
    assert diffs[1]["changes"]["departure_minute"] == {"before": 0, "after": 2}
    assert diffs[1]["changes"]["arrival_minute"] == {"before": 2, "after": 4}
    assert diffs[2]["changed"] is True
    assert diffs[2]["changes"]["route"] == {"before": [1], "after": [2]}
    assert diffs[2]["changes"]["departure_minute"] == {"before": 2, "after": 0}
    assert diffs[2]["changes"]["arrival_minute"] == {"before": 4, "after": 5}

    # 预演绝不写入：现状编排与封闭集均不变
    after = client.get("/api/plan").json()
    assert after == before


def test_preview_conflict_with_departed_returns_409_and_no_credential(client):
    client.post("/api/clock/advance", json={"minutes": 1})  # g1 已出发，剩余占用 {1}
    resp = _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "planning_conflict"
    assert "整次变更拒绝" in body["detail"]
    assert "credential" not in body and "token" not in body

    # 批量中一条冲突 -> 整次拒绝、无凭据、不写状态
    resp = _preview(client, [
        {"edge_id": 1, "start_minute": 90, "end_minute": 95},
        {"edge_id": 1, "start_minute": 1, "end_minute": 3},
    ])
    assert resp.status_code == 409
    plan = client.get("/api/plan").json()
    assert plan["now"] == 1 and len(plan["groups"]) == 3

    # 边界相接（仅覆盖过去 [0,1)）可预演
    assert _preview(client, {"edge_id": 1, "start_minute": 0, "end_minute": 1}).status_code == 200


def test_preview_validation_semantics_compatible(client):
    # 左闭右开：end > start，结构错误 422（与 POST /api/closures 一致）
    assert _preview(client, {"edge_id": 1, "start_minute": 5, "end_minute": 5}).status_code == 422
    # 引用不存在的通道：400
    assert _preview(client, {"edge_id": 99, "start_minute": 0, "end_minute": 2}).status_code == 400


def test_confirm_commits_and_matches_preview(client):
    body = _preview(client, {"edge_id": 1, "start_minute": 0, "end_minute": 2}).json()
    token = body["credential"]["token"]
    projected = body["plan"]

    resp = client.post("/api/closures/confirm", json={"token": token})
    assert resp.status_code == 200
    committed = resp.json()
    # 提交结果与预演的拟执行编排完全一致
    assert committed["now"] == projected["now"]
    assert committed["groups"] == projected["groups"]

    # 封闭确实落库（编号与预演一致），删除该编号应成功
    assert client.delete("/api/closures/1").status_code == 200

    # 凭据一次性：再次确认 -> 404
    again = client.post("/api/closures/confirm", json={"token": token})
    assert again.status_code == 404
    assert again.json()["error"] == "credential_not_found"


def test_unknown_token_is_404(client):
    resp = client.post("/api/closures/confirm", json={"token": "not-a-real-token"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "credential_not_found"


@pytest.mark.parametrize("mutation", [
    lambda c: c.post("/api/clock/advance", json={"minutes": 1}),
    lambda c: c.post("/api/groups", json={"id": 50, "people": 1, "source": "A",
                                          "earliest_departure": 0, "priority": 1, "deadline": 50}),
    lambda c: c.delete("/api/groups/3"),
    lambda c: c.post("/api/closures", json={"edge_id": 2, "start_minute": 30, "end_minute": 40}),
    lambda c: c.post("/api/scenario", json=json.loads(SCENARIO.model_dump_json())),
    lambda c: c.post("/api/reset", json={}),
])
def test_any_committed_change_invalidates_credential(client, mutation):
    # 先造一条已存在封闭，供“删除封闭”变更使用
    client.post("/api/closures", json={"edge_id": 2, "start_minute": 30, "end_minute": 40})
    body = _preview(client, {"edge_id": 1, "start_minute": 0, "end_minute": 2}).json()
    token = body["credential"]["token"]
    version_before = body["state_version"]

    assert mutation(client).status_code in (200,)

    resp = client.post("/api/closures/confirm", json={"token": token})
    assert resp.status_code == 409
    err = resp.json()
    assert err["error"] == "credential_invalid"
    assert str(version_before) in err["detail"]

    # 拒绝确认且不写入：预演中的封闭（边1 [0,2)）未落库
    groups = _groups(client.get("/api/plan"))
    # 若落库，g1 出发将被推迟到 2；未落库则保持 0（场景重置/reset 后同样为 0）
    assert groups[1]["departure_minute"] == 0


def test_delete_closure_invalidates_credential(client):
    # 独立演示“删除封闭”这一变更也使凭据失效
    client.post("/api/closures", json={"edge_id": 2, "start_minute": 30, "end_minute": 40})
    token = _preview(client, {"edge_id": 1, "start_minute": 0, "end_minute": 2}
                     ).json()["credential"]["token"]
    assert client.delete("/api/closures/1").status_code == 200  # 删除边2 上的封闭
    resp = client.post("/api/closures/confirm", json={"token": token})
    assert resp.status_code == 409
    assert resp.json()["error"] == "credential_invalid"


def test_credential_bound_to_version_only_not_token_count(client):
    # 同一版本可发放多张凭据；提交其中一张后版本前进，其余全部失效
    t1 = _preview(client, {"edge_id": 1, "start_minute": 0, "end_minute": 2}
                  ).json()["credential"]["token"]
    t2 = _preview(client, {"edge_id": 2, "start_minute": 0, "end_minute": 5}
                  ).json()["credential"]["token"]
    assert client.post("/api/closures/confirm", json={"token": t1}).status_code == 200
    resp = client.post("/api/closures/confirm", json={"token": t2})
    assert resp.status_code == 409
    assert resp.json()["error"] == "credential_invalid"


def test_confirm_after_advance_then_conflict_rejected(client):
    # 预演时合法；其间时钟推进使组已出发，确认时内容重核发现触及剩余路线 -> 拒绝
    token = _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3}
                     ).json()["credential"]["token"]
    client.post("/api/clock/advance", json={"minutes": 1})
    resp = client.post("/api/closures/confirm", json={"token": token})
    assert resp.status_code == 409
    assert resp.json()["error"] == "credential_invalid"
    # 该封闭未落库：再预演同一时段应直接得到 409 planning_conflict（而非 400 编号冲突）
    assert _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3}).status_code == 409


def test_batch_preview_assigns_ids_and_confirm_commits_all(client):
    resp = _preview(client, [
        {"edge_id": 1, "start_minute": 40, "end_minute": 45},
        {"edge_id": 2, "start_minute": 50, "end_minute": 55},
    ])
    assert resp.status_code == 200
    body = resp.json()
    ids = [c["id"] for c in body["credential"]["closures"]]
    assert ids == [1, 2]
    token = body["credential"]["token"]
    assert client.post("/api/closures/confirm", json={"token": token}).status_code == 200
    # 两条均按预演编号落库，均可删除
    assert client.delete("/api/closures/1").status_code == 200
    assert client.delete("/api/closures/2").status_code == 200


def test_preview_diff_captures_blocked_group(client):
    # 现状 g3 已 blocked(no_capacity_before_deadline)；
    # 封闭边2 [0,10) 不改变其结论 -> changed=False；封闭边1 [0,10) 同样不改变原因码。
    resp = _preview(client, {"edge_id": 2, "start_minute": 0, "end_minute": 10})
    assert resp.status_code == 200
    diffs = {d["id"]: d for d in resp.json()["changes"]}
    assert diffs[3]["changed"] is False
    assert diffs[3]["current"]["reason"] == "no_capacity_before_deadline"
    assert diffs[3]["projected"]["reason"] == "no_capacity_before_deadline"

    # 解除性场景对比：换一个只有边2 能走的组，封闭边2 会把 scheduled 变 blocked
    client.post("/api/scenario", json=json.loads(ScenarioIn(
        edges=[EdgeIn(id=1, source="A", target="F", travel_minutes=2, capacity_per_minute=2),
               EdgeIn(id=2, source="A", target="F", travel_minutes=5, capacity_per_minute=10)],
        exits=[ExitIn(id=1, node="F")],
        groups=[GroupIn(id=1, people=2, source="A", earliest_departure=0,
                        priority=10, deadline=20)],
    ).model_dump_json()))
    # 先把边1 在 [0,20) 封死：现状唯一可行路线边2 dep0
    client.post("/api/closures", json={"edge_id": 1, "start_minute": 0, "end_minute": 20})
    g = _groups(client.get("/api/plan"))[1]
    assert g["route"] == [2] and g["status"] == "scheduled"
    # 预演再封边2 [0,20)：边1/边2 均不可行 -> blocked，原因变化体现在 diff
    resp = _preview(client, {"edge_id": 2, "start_minute": 0, "end_minute": 20})
    d = {x["id"]: x for x in resp.json()["changes"]}[1]
    assert d["changed"] is True
    assert d["changes"]["status"] == {"before": "scheduled", "after": "blocked"}
    assert d["changes"]["reason"]["before"] is None
    assert d["changes"]["reason"]["after"] == "no_capacity_before_deadline"
