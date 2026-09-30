"""服务层：把 HTTP 入参、SQLite 事务与 planner 重排串成原子变更。

并发保证
--------
- 进程内全局互斥锁串行化所有“读状态 → 重排 → 落库”变更；
- 每次变更在单个 SQLite 事务（BEGIN IMMEDIATE）内完成，失败整体回滚，
  不会出现容量超用或只重排一部分组的情况。
"""
from __future__ import annotations

import secrets
import sqlite3
import threading
from collections import OrderedDict
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


class RequestError(Exception):
    """请求语义错误（编号冲突、引用不存在等），对应 HTTP 400。"""


class CredentialError(Exception):
    """预演凭据无效（不存在、已使用、内容或版本不匹配），对应 HTTP 409。"""


@dataclass(frozen=True)
class _PreviewRecord:
    """一张预演凭据绑定的全部信息（仅存内存，进程重启即失效）。"""
    version: int
    # 规范化后的批次：(分配编号, edge_id, start, end)，顺序与请求一致
    closures: tuple[tuple[int, int, int, int], ...]
    used: bool = False

    def content(self) -> tuple[tuple[int, int, int], ...]:
        """去掉编号后的内容指纹 multiset（用于内容核对）。"""
        return tuple(sorted((e, s, t) for _cid, e, s, t in self.closures))

    def closure_ids(self) -> tuple[int, ...]:
        return tuple(cid for cid, _e, _s, _t in self.closures)


class EvacuationService:
    # 内存中保留的预演凭据上限（超出后最早的被淘汰）
    MAX_PREVIEWS = 1000

    def __init__(self, conn: sqlite3.Connection, max_paths: int = 10000):
        self._conn = conn
        self._lock = threading.RLock()
        self.max_paths = max_paths
        self._previews: "OrderedDict[str, _PreviewRecord]" = OrderedDict()

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
            "state_version": state.version,
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

    def _finalize_commit(self, conn: sqlite3.Connection, state: State) -> None:
        """提交变更前统一推进版本号并使所有预演凭据失效（须已在事务中）。"""
        state.version = database.bump_version(conn, state.version)
        self._previews.clear()

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
                conn.commit()
                # 场景替换/重置也属于状态变更：旧凭据一律失效；版本随整库替换归 0
                state.version = 0
                self._previews.clear()
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
                self._finalize_commit(conn, state)
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
                self._finalize_commit(conn, state)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    # ------------------------------------------------------------ 封闭

    @staticmethod
    def _fixed_groups(state: State) -> list[Group]:
        return [g for g in state.groups.values()
                if g.status == "scheduled" and g.departure is not None
                and g.departure < state.now]

    @staticmethod
    def _prepare_closures(
        state: State,
        items: list,
    ) -> list[Closure]:
        """校验一批待新增封闭并分配编号；含逐条冲突检测。

        - 引用不存在的通道 / 显式编号重复：RequestError（400）
        - 任一封闭触及已出发组剩余路线：PlanningError（409，整批拒绝）
        编号分配规则与原 POST /api/closures 完全一致（显式优先，其余取
        max(现有/显式编号)+1 起顺序递增的空闲编号）。
        """
        fixed = EvacuationService._fixed_groups(state)
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
            closure = Closure(cid, item.edge_id, item.start_minute, item.end_minute)
            conflict = closure_conflicts_with_fixed(
                closure, state.edges, fixed, state.now)
            if conflict is not None:
                raise PlanningError(
                    f"封闭 [{item.start_minute},{item.end_minute}) 触及已出发组 "
                    f"{conflict} 通道 {item.edge_id} 的剩余路线，整次变更拒绝"
                )
            prepared.append(closure)
        return prepared

    def add_closures(self, items: list[ClosureIn]) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                prepared = self._prepare_closures(state, items)
                for closure in prepared:
                    state.closures[closure.id] = closure
                database.add_closures(conn, prepared)
                self._commit_replan(conn, state, self.max_paths)
                self._finalize_commit(conn, state)
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
                self._finalize_commit(conn, state)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    # ---------------------------------------------------- 封闭预演 / 确认

    @staticmethod
    def _clone_state(state: State) -> State:
        """深拷贝状态用于预演重排（仅拷贝可变部分：closures/groups）。"""
        return State(
            edges=state.edges,
            exits=state.exits,
            closures=dict(state.closures),
            groups={
                gid: Group(
                    g.id, g.people, g.source, g.earliest_departure,
                    g.priority, g.deadline,
                    route=list(g.route), departure=g.departure,
                    arrival=g.arrival, status=g.status, reason=g.reason,
                )
                for gid, g in state.groups.items()
            },
            now=state.now,
            version=state.version,
        )

    @staticmethod
    def _group_plan_key(g: Group) -> tuple:
        """逐组编排指纹：用于预演前后差异比较（与 _group_json 字段对齐）。"""
        return (g.status, tuple(g.route), g.departure, g.arrival, g.reason)

    @classmethod
    def _diff_groups(
        cls,
        before: dict[int, Group],
        after: dict[int, Group],
        now: int,
    ) -> list[dict]:
        """逐组比较现状 -> 拟执行编排，仅包含发生变化的组，按组编号升序。"""
        diffs: list[dict] = []
        for gid in sorted(set(before) | set(after)):
            old, new = before.get(gid), after.get(gid)
            if old is not None and new is not None and (
                cls._group_plan_key(old) == cls._group_plan_key(new)
            ):
                continue
            diffs.append({
                "id": gid,
                "before": None if old is None else cls._group_json(old, now),
                "after": None if new is None else cls._group_json(new, now),
            })
        return diffs

    def preview_closures(self, items: list[ClosureIn]) -> dict:
        """预演一批封闭时段对疏散计划的影响（不写入状态、不推进版本）。

        复用与正式提交完全一致的字段校验、编号分配、已出发组剩余占用冲突
        检测与全量重排规则。冲突时抛 PlanningError（409）且不生成凭据。
        """
        with self._lock:
            state = database.load_state(self._conn)
            # 1) 与正式提交相同的校验/编号/冲突检测；冲突即 409，不发凭据
            prepared = self._prepare_closures(state, items)

            # 2) 拷贝状态后在内存中重排（当前状态与数据库保持不变）
            simulated = self._clone_state(state)
            before_groups = {
                gid: Group(
                    g.id, g.people, g.source, g.earliest_departure,
                    g.priority, g.deadline, route=list(g.route),
                    departure=g.departure, arrival=g.arrival,
                    status=g.status, reason=g.reason,
                )
                for gid, g in state.groups.items()
            }
            for closure in prepared:
                simulated.closures[closure.id] = closure
            replan(simulated.edges, simulated.exits, simulated.closures,
                   simulated.groups, simulated.now, max_paths=self.max_paths)

            # 3) 签发绑定“批次内容 + 当前版本”的确认凭据
            token = secrets.token_urlsafe(24)
            record = _PreviewRecord(
                version=state.version,
                closures=tuple(
                    (c.id, c.edge_id, c.start_minute, c.end_minute) for c in prepared
                ),
            )
            self._previews[token] = record
            self._previews.move_to_end(token)
            while len(self._previews) > self.MAX_PREVIEWS:
                self._previews.popitem(last=False)

            plan = self._serialize(simulated)
            return {
                "committed": False,
                "token": token,
                "state_version": state.version,
                "closures": [
                    {
                        "id": c.id,
                        "edge_id": c.edge_id,
                        "start_minute": c.start_minute,
                        "end_minute": c.end_minute,
                    }
                    for c in prepared
                ],
                "now": simulated.now,
                "plan": plan,
                "changes": self._diff_groups(before_groups, simulated.groups,
                                             simulated.now),
            }

    def confirm_closures(self, token: str, items: list[ClosureIn] | None) -> dict:
        """在单个原子操作内核验凭据内容与版本，按现行规则重算并提交。

        凭据不存在/已使用、批次内容不一致，或预演后状态版本发生过任何变化
        （人群组或封闭增删、场景替换或重置、时钟推进）均拒绝确认且不写入。
        """
        with self._lock:
            record = self._previews.get(token)
            if record is None:
                raise CredentialError("确认凭据不存在、已使用或已失效，请重新预演")
            if record.used:
                raise CredentialError("确认凭据已使用，请重新预演")

            conn = self._transaction()
            try:
                state = database.load_state(conn)

                # 原子内核验：版本必须仍是预演时的版本
                if state.version != record.version:
                    raise CredentialError(
                        f"预演基于状态版本 {record.version}，当前版本已为 "
                        f"{state.version}（期间发生过状态变更），凭据失效"
                    )

                # 内容核对：回传了批次则逐条匹配，否则核对凭据绑定内容
                if items is not None:
                    submitted = tuple(sorted(
                        (c.edge_id, c.start_minute, c.end_minute) for c in items
                    ))
                    if submitted != record.content():
                        raise CredentialError(
                            "待确认封闭批次与预演内容不一致，凭据失效"
                        )

                # 以凭据绑定的编号重建为显式编号批次，保证与预演完全一致地
                # 重新走一遍校验/冲突检测并按现行规则重算（与直连接口同规则）
                effective_items = [
                    ClosureIn(id=cid, edge_id=edge_id,
                              start_minute=start, end_minute=end)
                    for cid, edge_id, start, end in record.closures
                ]
                prepared = self._prepare_closures(state, effective_items)
                if tuple(c.id for c in prepared) != record.closure_ids():
                    # 版本不变时编号必然一致；防御性核对
                    raise CredentialError("封闭编号分配与预演不一致，凭据失效")
                for closure in prepared:
                    state.closures[closure.id] = closure
                database.add_closures(conn, prepared)
                self._commit_replan(conn, state, self.max_paths)
                self._finalize_commit(conn, state)
                conn.commit()
            except Exception:
                conn.rollback()
                # 凭据一次性：任何失败（含版本失效）都移除，不可重试
                self._previews.pop(token, None)
                raise
            # 提交成功：凭据已使用（_finalize_commit 已清空全部预演）
            response = self._serialize(state)
            response["committed"] = True
            response["token"] = token
            return response

    # ------------------------------------------------------------ 时钟

    def advance(self, minutes: int) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                state.now += minutes
                database.set_now(conn, state.now)
                self._finalize_commit(conn, state)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    def reset_seed(self, scenario: ScenarioIn) -> dict:
        return self.replace_scenario(scenario)
