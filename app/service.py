"""服务层：把 HTTP 入参、SQLite 事务与 planner 重排串成原子变更。

并发保证
--------
- 进程内全局互斥锁串行化所有“读状态 → 重排 → 落库”变更；
- 每次变更在单个 SQLite 事务（BEGIN IMMEDIATE）内完成，失败整体回滚，
  不会出现容量超用或只重排一部分组的情况。

封闭预演（dry-run）
-------------------
- ``preview_closures`` 只读状态：在内存副本上重排，生成绑定“封闭批次内容 +
  当前状态版本”的凭据，不写库；
- ``confirm_closures`` 在同一把锁、同一个事务内核对凭据内容与状态版本，
  重算并提交。任何已提交的变更（组/封闭增删、场景替换/重置、时钟推进）
  都会推进状态版本，使旧凭据失效；凭据仅存在于服务进程内存中，进程重启
  同样使凭据失效。
"""
from __future__ import annotations

import copy
import secrets
import sqlite3
import threading
from dataclasses import dataclass

from . import database
from .models import Closure, Edge, Exit, Group, State
from .planner import PlanningError, closure_conflicts_with_fixed, replan
from .schemas import ClosureIn, GroupIn, ScenarioIn

REASON_TEXT = {
    "no_path_to_exit": "图中不存在从该起点到达任何出口的路线",
    "no_capacity_before_deadline": "受通道每分钟容量或封闭时段限制，无法在规划截止分钟前安排",
    "deadline_already_passed": "当前分钟已超过该组的规划截止分钟",
}

# 逐组对比时关注的编排字段（相对现状的变化）
_DIFF_FIELDS = (
    ("status", "status"),
    ("route", "route"),
    ("departure_minute", "departure"),
    ("arrival_minute", "arrival"),
    ("reason", "reason"),
)


class RequestError(Exception):
    """请求语义错误（编号冲突、引用不存在等），对应 HTTP 400。"""


class CredentialNotFound(Exception):
    """确认凭据不存在或已被使用/随进程重启丢失，对应 HTTP 404。"""


class CredentialInvalid(Exception):
    """确认凭据绑定的状态版本已过期（其间发生过任意已提交变更），对应 HTTP 409。"""


@dataclass(frozen=True)
class _PreviewRecord:
    """一条预演凭据的服务端记录（仅存内存）。"""
    token: str
    version: int
    closures: tuple[Closure, ...]


class EvacuationService:
    def __init__(self, conn: sqlite3.Connection, max_paths: int = 10000):
        self._conn = conn
        self._lock = threading.RLock()
        self.max_paths = max_paths
        # token -> _PreviewRecord：预演凭据（进程内，重启即失效）
        self._previews: dict[str, _PreviewRecord] = {}

    # ------------------------------------------------------------ 序列化

    def snapshot(self) -> dict:
        with self._lock:
            state = database.load_state(self._conn)
        return self._serialize(state)

    @staticmethod
    def _serialize(state: State) -> dict:
        fixed = [g for g in state.groups.values()
                 if g.status == "scheduled" and g.departure is not None and g.departure < state.now]
        rest = [g for g in state.groups.values() if g.id not in {f.id for f in fixed}]
        ordered = (
            sorted(fixed, key=lambda g: (g.departure, g.id))
            + sorted(rest, key=lambda g: (-g.priority, g.earliest_departure, g.id))
        )
        return {
            "now": state.now,
            "groups": [EvacuationService._group_json(g, state.now) for g in ordered],
        }

    @staticmethod
    def _group_json(g: Group, now: int) -> dict:
        departed = g.status == "scheduled" and g.departure is not None and g.departure < now
        return {
            "id": g.id,
            "people": g.people,
            "source": g.source,
            "earliest_departure": g.earliest_departure,
            "priority": g.priority,
            "deadline": g.deadline,
            "status": g.status,  # scheduled / blocked
            "departed": departed,
            "route": g.route,
            "departure_minute": g.departure,
            "arrival_minute": g.arrival,
            "reason": g.reason,
            "reason_text": REASON_TEXT.get(g.reason) if g.reason else None,
        }

    # ------------------------------------------------------------ 事务

    def _transaction(self) -> sqlite3.Connection:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        return conn

    @staticmethod
    def _commit_replan(conn: sqlite3.Connection, state: State, max_paths: int) -> None:
        """在 state 上重排，并把全部组的最新编排结果落库（须已在事务中）。"""
        replan(state.edges, state.exits, state.closures, state.groups,
               state.now, max_paths=max_paths)
        database.add_groups(conn, list(state.groups.values()))

    # ------------------------------------------------------------ 场景

    def replace_scenario(self, scenario: ScenarioIn) -> dict:
        edges, exits, closures, groups = self._build_scenario_objects(scenario)
        state = State(edges=edges, exits=exits, closures=closures, groups=groups, now=0)
        with self._lock:
            conn = self._transaction()
            try:
                try:
                    replan(edges, exits, closures, groups, 0, max_paths=self.max_paths)
                except PlanningError as exc:
                    # 整体下发场景的结构性错误属于请求问题（400）
                    raise RequestError(str(exc)) from exc
                database.replace_state(conn, list(edges.values()),
                                       list(exits.values()), list(closures.values()),
                                       list(groups.values()))
                database.bump_state_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    @staticmethod
    def _build_scenario_objects(scenario: ScenarioIn):
        edge_ids, exit_ids, group_ids, closure_ids = set(), set(), set(), set()
        edges: dict[int, Edge] = {}
        for e in scenario.edges:
            if e.id in edge_ids:
                raise RequestError(f"通道编号 {e.id} 重复")
            edge_ids.add(e.id)
            edges[e.id] = Edge(e.id, e.source, e.target,
                               e.travel_minutes, e.capacity_per_minute)
        exits: dict[int, Exit] = {}
        for x in scenario.exits:
            if x.id in exit_ids:
                raise RequestError(f"出口编号 {x.id} 重复")
            exit_ids.add(x.id)
            exits[x.id] = Exit(x.id, x.node)
        groups: dict[int, Group] = {}
        for g in scenario.groups:
            if g.id in group_ids:
                raise RequestError(f"人群组编号 {g.id} 重复")
            group_ids.add(g.id)
            groups[g.id] = Group(g.id, g.people, g.source, g.earliest_departure,
                                 g.priority, g.deadline)
        closures: dict[int, Closure] = {}
        next_id = 1
        for c in scenario.closures:
            if c.id is not None:
                if c.id in closure_ids:
                    raise RequestError(f"封闭记录编号 {c.id} 重复")
                closure_ids.add(c.id)
                next_id = max(next_id, c.id + 1)
        for c in scenario.closures:
            cid = c.id if c.id is not None else next_id
            if c.id is None:
                next_id += 1
            if c.edge_id not in edges:
                raise RequestError(f"封闭记录引用了不存在的通道 {c.edge_id}")
            closures[cid] = Closure(cid, c.edge_id, c.start_minute, c.end_minute)
        return edges, exits, closures, groups

    # ------------------------------------------------------------ 人群组

    def add_groups(self, items: list[GroupIn]) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                seen: set[int] = set()
                for item in items:
                    if item.id in state.groups or item.id in seen:
                        raise RequestError(f"人群组编号 {item.id} 已存在")
                    seen.add(item.id)
                    nodes = {n for e in state.edges.values() for n in (e.source, e.target)}
                    if item.source not in nodes:
                        raise RequestError(f"人群组 {item.id} 的起点 {item.source} 不存在")
                    state.groups[item.id] = Group(
                        item.id, item.people, item.source,
                        item.earliest_departure, item.priority, item.deadline,
                    )
                self._commit_replan(conn, state, self.max_paths)
                database.bump_state_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    def delete_group(self, group_id: int) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                g = state.groups.get(group_id)
                if g is None:
                    raise RequestError(f"人群组 {group_id} 不存在")
                if g.status == "scheduled" and g.departure is not None and g.departure < state.now:
                    raise PlanningError(
                        f"人群组 {group_id} 已出发，路线与占用已固定，不能删除"
                    )
                del state.groups[group_id]
                database.delete_group(conn, group_id)
                self._commit_replan(conn, state, self.max_paths)
                database.bump_state_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    # ------------------------------------------------------------ 封闭

    @staticmethod
    def _fixed_groups(state: State) -> list[Group]:
        """当前已出发（路线/占用固定）的组。"""
        return [g for g in state.groups.values()
                if g.status == "scheduled" and g.departure is not None
                and g.departure < state.now]

    @staticmethod
    def _prepare_closures(items: list[ClosureIn], state: State) -> list[Closure]:
        """校验封闭批次并分配编号（显式编号优先，否则沿现有编号续号）。

        编号规则与既有 POST /api/closures 完全一致；不做任何写入。
        """
        next_id = max(state.closures.keys(), default=0) + 1
        used = set(state.closures.keys())
        prepared: list[Closure] = []
        for item in items:
            if item.edge_id not in state.edges:
                raise RequestError(f"封闭记录引用了不存在的通道 {item.edge_id}")
            if item.id is not None:
                if item.id in used:
                    raise RequestError(f"封闭记录编号 {item.id} 已存在")
                cid = item.id
                used.add(cid)
                next_id = max(next_id, cid + 1)
            else:
                while next_id in used:
                    next_id += 1
                cid = next_id
                used.add(cid)
                next_id += 1
            prepared.append(Closure(cid, item.edge_id,
                                    item.start_minute, item.end_minute))
        return prepared

    @staticmethod
    def _check_fixed_conflicts(prepared: list[Closure], state: State) -> None:
        """任一封闭触及任一已出发组剩余路线即抛 PlanningError（整次拒绝）。"""
        fixed = EvacuationService._fixed_groups(state)
        for closure in prepared:
            conflict = closure_conflicts_with_fixed(
                closure, state.edges, fixed, state.now)
            if conflict is not None:
                raise PlanningError(
                    f"封闭 [{closure.start_minute},{closure.end_minute}) 触及已出发组 "
                    f"{conflict} 通道 {closure.edge_id} 的剩余路线，整次变更拒绝"
                )

    def add_closures(self, items: list[ClosureIn]) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                prepared = self._prepare_closures(items, state)
                self._check_fixed_conflicts(prepared, state)
                for closure in prepared:
                    state.closures[closure.id] = closure
                database.add_closures(conn, prepared)
                self._commit_replan(conn, state, self.max_paths)
                database.bump_state_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    def delete_closure(self, closure_id: int) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                if closure_id not in state.closures:
                    raise RequestError(f"封闭记录 {closure_id} 不存在")
                del state.closures[closure_id]
                database.delete_closure(conn, closure_id)
                self._commit_replan(conn, state, self.max_paths)
                database.bump_state_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    # ------------------------------------------------------------ 预演与确认

    @staticmethod
    def _group_brief(g: Group) -> dict:
        """逐组对比用的编排要点（现状/拟执行后同构，便于直接 diff）。"""
        return {
            "status": g.status,
            "route": list(g.route),
            "departure_minute": g.departure,
            "arrival_minute": g.arrival,
            "reason": g.reason,
        }

    def _diff_groups(self, before: State, after: State) -> list[dict]:
        """逐组对比现状与拟执行后编排：出发时间、路线、到达时间或受阻原因的变化。"""
        fixed_before = {g.id for g in self._fixed_groups(before)}
        fixed_after = {g.id for g in self._fixed_groups(after)}
        result: list[dict] = []
        for gid in sorted(after.groups):
            gb = before.groups.get(gid)
            ga = after.groups[gid]
            cur_brief = self._group_brief(gb) if gb is not None else None
            new_brief = self._group_brief(ga)
            changes = {
                json_key: {"before": cur_brief[json_key] if cur_brief else None,
                           "after": new_brief[json_key]}
                for json_key, _attr in _DIFF_FIELDS
                if cur_brief is None or cur_brief[json_key] != new_brief[json_key]
            }
            result.append({
                "id": gid,
                "departed": gid in fixed_before or gid in fixed_after,
                "changed": bool(changes),
                "current": cur_brief,
                "projected": new_brief,
                "changes": changes,
            })
        return result

    def preview_closures(self, items: list[ClosureIn]) -> dict:
        """预演一批封闭：只读状态、内存重排、发放绑定版本的凭据，绝不写库。"""
        with self._lock:
            state = database.load_state(self._conn)
            prepared = self._prepare_closures(items, state)
            # 与已出发组剩余占用冲突 -> PlanningError(409)，且不生成凭据
            self._check_fixed_conflicts(prepared, state)

            # 在副本上重排，保证预演不改动任何已加载/已落库状态
            projected = State(
                edges=state.edges, exits=state.exits,
                closures=dict(state.closures),
                groups={gid: copy.deepcopy(g) for gid, g in state.groups.items()},
                now=state.now,
            )
            for closure in prepared:
                projected.closures[closure.id] = closure
            replan(projected.edges, projected.exits, projected.closures,
                   projected.groups, projected.now, max_paths=self.max_paths)

            token = secrets.token_urlsafe(24)
            self._previews[token] = _PreviewRecord(
                token=token, version=state.version, closures=tuple(prepared))

            return {
                "now": projected.now,
                "state_version": state.version,
                "credential": {
                    "token": token,
                    "state_version": state.version,
                    "closures": [
                        {"id": c.id, "edge_id": c.edge_id,
                         "start_minute": c.start_minute, "end_minute": c.end_minute}
                        for c in prepared
                    ],
                },
                "plan": self._serialize(projected),
                "changes": self._diff_groups(state, projected),
            }

    def confirm_closures(self, token: str) -> dict:
        """凭预演凭据原子提交：同一锁/事务内核对内容与状态版本，重算并落库。"""
        with self._lock:
            record = self._previews.get(token)
            if record is None:
                raise CredentialNotFound(
                    "确认凭据不存在、已使用或随服务重启失效，请重新预演"
                )
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                # 版本核对：其间任何已提交变更（组/封闭增删、场景替换/重置、
                # 时钟推进）都使版本前进 -> 凭据失效，拒绝且不写入
                if state.version != record.version:
                    self._previews.pop(token, None)
                    raise CredentialInvalid(
                        f"凭据绑定版本 {record.version}，当前状态版本 "
                        f"{state.version}：其间状态已变更（人群组或封闭变更、"
                        "场景替换或重置、时钟推进），凭据失效，请重新预演"
                    )
                # 内容核对：版本相同的前提下重放全部语义校验，
                # 确认凭据绑定的封闭批次仍可提交
                items = [
                    ClosureIn(id=c.id, edge_id=c.edge_id,
                              start_minute=c.start_minute, end_minute=c.end_minute)
                    for c in record.closures
                ]
                prepared = self._prepare_closures(items, state)
                self._check_fixed_conflicts(prepared, state)
                if [c.id for c in prepared] != [c.id for c in record.closures] or \
                        any(a.edge_id != b.edge_id or
                            a.start_minute != b.start_minute or
                            a.end_minute != b.end_minute
                            for a, b in zip(prepared, record.closures)):
                    self._previews.pop(token, None)
                    raise CredentialInvalid("凭据内容与当前状态核对失败，请重新预演")

                for closure in prepared:
                    state.closures[closure.id] = closure
                database.add_closures(conn, prepared)
                # 按现有优先级、容量和固定路线规则重算
                self._commit_replan(conn, state, self.max_paths)
                database.bump_state_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            # 一次性凭据：提交成功即摘除（版本已前进，其余旧凭据同时失效）
            self._previews.pop(token, None)
        return self._serialize(state)

    # ------------------------------------------------------------ 时钟

    def advance(self, minutes: int) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                state.now += minutes
                database.set_now(conn, state.now)
                database.bump_state_version(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    def reset_seed(self, scenario: ScenarioIn) -> dict:
        return self.replace_scenario(scenario)
