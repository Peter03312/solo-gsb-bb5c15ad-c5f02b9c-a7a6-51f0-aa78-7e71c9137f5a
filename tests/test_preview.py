"""封闭预演（dry-run）与凭据确认（confirm）的 HTTP API 测试。"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
_tmp.close()
os.environ["EVAC_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.schemas import EdgeIn, ExitIn, GroupIn, ScenarioIn  # noqa: E402

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


def _preview(client, payload):
    return client.post("/api/closures/preview", json=payload)


# ---------------------------------------------------------------- 基本语义

def test_plan_carries_state_version(client):
    plan = client.get("/api/plan").json()
    assert plan["state_version"] == 0


def test_preview_returns_token_full_plan_and_per_group_changes(client):
    before = _groups(client.get("/api/plan"))
    resp = _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 200
    body = resp.json()

    # 凭据与版本绑定
    assert body["committed"] is False
    assert isinstance(body["token"], str) and body["token"]
    assert body["state_version"] == 0
    assert body["now"] == 0
    # 拟分配编号与正式提交一致（场景无既有封闭 -> 1）
    assert body["closures"] == [
        {"id": 1, "edge_id": 1, "start_minute": 1, "end_minute": 3}
    ]

    # 拟执行后的完整编排
    proposed = {g["id"]: g for g in body["plan"]["groups"]}
    assert proposed[1]["departure_minute"] == 3
    assert proposed[2]["route"] == [2]

    # 逐组差异：受影响组（1、2）出现，未变化的组 3 不出现
    changed = {d["id"]: d for d in body["changes"]}
    assert set(changed) == {1, 2}
    assert changed[1]["before"]["departure_minute"] == before[1]["departure_minute"] == 0
    assert changed[1]["after"]["departure_minute"] == 3
    assert changed[2]["before"]["route"] == [1]
    assert changed[2]["after"]["route"] == [2]


def test_preview_does_not_mutate_state(client):
    plan_before = client.get("/api/plan").json()
    resp = _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 200
    plan_after = client.get("/api/plan").json()
    assert plan_after == plan_before  # 含 state_version 均不变


def test_preview_batch_closures(client):
    resp = _preview(client, [
        {"edge_id": 1, "start_minute": 1, "end_minute": 3},
        {"edge_id": 2, "start_minute": 0, "end_minute": 10},
    ])
    assert resp.status_code == 200
    body = resp.json()
    assert [c["id"] for c in body["closures"]] == [1, 2]
    assert len(body["closures"]) == 2


# ---------------------------------------------------------------- 冲突

def test_preview_conflict_with_departed_group_returns_409_and_no_token(client):
    # g1 dep0、edge1 通行 2 分钟；推进到 now=1 后剩余占用分钟 {1}
    client.post("/api/clock/advance", json={"minutes": 1})
    assert _groups(client.get("/api/plan"))[1]["departed"] is True

    resp = _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "planning_conflict"
    assert "整次变更拒绝" in body["detail"]
    assert "token" not in body  # 冲突不生成可确认凭据

    # 同批带另一条合法封闭也整批拒绝，且状态不变
    resp = _preview(client, [
        {"edge_id": 1, "start_minute": 1, "end_minute": 3},
        {"edge_id": 1, "start_minute": 90, "end_minute": 95},
    ])
    assert resp.status_code == 409
    assert "token" not in resp.json()
    plan = client.get("/api/plan").json()
    assert plan["now"] == 1


def test_preview_validation_semantics_match_closures_endpoint(client):
    # 左闭右开 end > start -> 422
    assert _preview(client, {"edge_id": 1, "start_minute": 5, "end_minute": 5}).status_code == 422
    # 引用不存在的通道 -> 400
    resp = _preview(client, {"edge_id": 99, "start_minute": 0, "end_minute": 2})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_request"


# ---------------------------------------------------------------- 确认

def test_confirm_commits_and_matches_preview(client):
    pv = _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3}).json()
    resp = client.post("/api/closures/confirm", json={"token": pv["token"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["committed"] is True
    assert body["token"] == pv["token"]
    assert body["state_version"] == 1

    # 提交结果与预演编排逐组一致
    actual = {g["id"]: (g["route"], g["departure_minute"], g["arrival_minute"],
                        g["status"], g["reason"]) for g in body["groups"]}
    proposed = {g["id"]: (g["route"], g["departure_minute"], g["arrival_minute"],
                          g["status"], g["reason"]) for g in pv["plan"]["groups"]}
    assert actual == proposed

    # 封闭真正落库：GET 计划版本已推进，且按新封闭编排
    plan = client.get("/api/plan").json()
    assert plan["state_version"] == 1
    assert _groups(client.get("/api/plan"))  # sanity


def test_confirm_with_echoed_batch_single_object(client):
    pv = _preview(client, {"edge_id": 1, "start_minute": 1, "end_minute": 3}).json()
    resp = client.post("/api/closures/confirm", json={
        "token": pv["token"],
        "closures": {"edge_id": 1, "start_minute": 1, "end_minute": 3},  # 单对象
    })
    assert resp.status_code == 200


def test_unknown_and_reused_token_rejected(client):
    resp = client.post("/api/closures/confirm", json={"token": "no-such-token"})
    assert resp.status_code == 409
    assert resp.json()["error"] == "credential_invalid"

    pv = _preview(client, {"edge_id": 1, "start_minute": 90, "end_minute": 95}).json()
    assert client.post("/api/closures/confirm", json={"token": pv["token"]}).status_code == 200
    # 一次性：重复使用拒绝
    again = client.post("/api/closures/confirm", json={"token": pv["token"]})
    assert again.status_code == 409
    assert again.json()["error"] == "credential_invalid"


def test_content_mismatch_rejected_without_write(client):
    pv = _preview(client, {"edge_id": 1, "start_minute": 90, "end_minute": 95}).json()
    resp = client.post("/api/closures/confirm", json={
        "token": pv["token"],
        "closures": [{"edge_id": 1, "start_minute": 91, "end_minute": 95}],
    })
    assert resp.status_code == 409
    assert resp.json()["error"] == "credential_invalid"
    # 未写入：版本未推进
    assert client.get("/api/plan").json()["state_version"] == 0
    # 失败后原凭据不可再用（一次性）
    again = client.post("/api/closures/confirm", json={"token": pv["token"]})
    assert again.status_code == 409


# ------------------------------------------------- 版本失效的各类变更

def _stale_after_mutation(client, mutate):
    pv = _preview(client, {"edge_id": 1, "start_minute": 90, "end_minute": 95}).json()
    version_at_preview = pv["state_version"]
    mutate()
    resp = client.post("/api/closures/confirm", json={"token": pv["token"]})
    assert resp.status_code == 409
    assert resp.json()["error"] == "credential_invalid"
    # 预演的封闭未写入，版本停留在变更后的版本（即预演本身无版本效果）
    plan = client.get("/api/plan").json()
    assert plan["state_version"] != version_at_preview


def test_credential_stale_after_clock_advance(client):
    _stale_after_mutation(
        client, lambda: client.post("/api/clock/advance", json={"minutes": 1}))


def test_credential_stale_after_group_add(client):
    _stale_after_mutation(client, lambda: client.post(
        "/api/groups",
        json={"id": 50, "people": 1, "source": "A", "earliest_departure": 0,
              "priority": 1, "deadline": 50}))


def test_credential_stale_after_closure_add(client):
    def mutate():
        r = client.post("/api/closures",
                        json={"edge_id": 1, "start_minute": 80, "end_minute": 85})
        assert r.status_code == 200
    _stale_after_mutation(client, mutate)


def test_credential_stale_after_closure_delete(client):
    # 先直接提交一条封闭，再预演，随后删除它
    assert client.post("/api/closures",
                       json={"edge_id": 1, "start_minute": 80, "end_minute": 85}).status_code == 200
    pv = _preview(client, {"edge_id": 1, "start_minute": 90, "end_minute": 95}).json()
    assert client.delete("/api/closures/1").status_code == 200
    resp = client.post("/api/closures/confirm", json={"token": pv["token"]})
    assert resp.status_code == 409


def test_credential_stale_after_scenario_replace_and_reset(client):
    pv = _preview(client, {"edge_id": 1, "start_minute": 90, "end_minute": 95}).json()
    client.post("/api/scenario", json=json.loads(SCENARIO.model_dump_json()))
    assert client.post("/api/closures/confirm",
                       json={"token": pv["token"]}).status_code == 409

    pv = _preview(client, {"edge_id": 1, "start_minute": 90, "end_minute": 95}).json()
    client.post("/api/reset", json={})
    assert client.post("/api/closures/confirm",
                       json={"token": pv["token"]}).status_code == 409


def test_failed_confirm_does_not_partially_write_batch(client):
    # 预演两条；确认前用一次直接提交占用编号（并改变版本），确认须整体失败不写入
    pv = _preview(client, [
        {"edge_id": 1, "start_minute": 20, "end_minute": 22},
        {"edge_id": 2, "start_minute": 20, "end_minute": 22},
    ]).json()
    # 场景替换使凭据失效（原子拒绝）
    client.post("/api/scenario", json=json.loads(SCENARIO.model_dump_json()))
    resp = client.post("/api/closures/confirm", json={"token": pv["token"]})
    assert resp.status_code == 409
    plan = client.get("/api/plan").json()
    # 场景重置后没有任何封闭导致的延迟
    assert {g["id"]: g for g in plan["groups"]}[1]["departure_minute"] == 0


def test_preview_after_any_change_gets_fresh_version_and_can_commit(client):
    client.post("/api/clock/advance", json={"minutes": 1})
    pv = _preview(client, {"edge_id": 1, "start_minute": 90, "end_minute": 95}).json()
    assert pv["state_version"] == 1
    resp = client.post("/api/closures/confirm", json={"token": pv["token"]})
    assert resp.status_code == 200
    assert resp.json()["state_version"] == 2
