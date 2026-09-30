"""服务层：把 HTTP 入参、SQLite 事务与 planner 重排串成原子变更。

并发保证
--------
- 进程内全局互斥锁串行化所有“读状态 → 重排 → 落库”变更；
- 每次变更在单个 SQLite 事务（BEGIN IMMEDIATE）内完成，失败整体回滚，
  不会出现容量超用或只重排一部分组的情况。
"""
from __future__ import annotations

import sqlite3
import threading

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


class EvacuationService:
    def __init__(self, conn: sqlite3.Connection, max_paths: int = 10000):
        self._conn = conn
        self._lock = threading.RLock()
        self.max_paths = max_paths

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
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    # ------------------------------------------------------------ 封闭

    def add_closures(self, items: list[ClosureIn]) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                fixed = [g for g in state.groups.values()
                         if g.status == "scheduled" and g.departure is not None
                         and g.departure < state.now]
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
                for closure in prepared:
                    state.closures[closure.id] = closure
                database.add_closures(conn, prepared)
                self._commit_replan(conn, state, self.max_paths)
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
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    # ------------------------------------------------------------ 时钟

    def advance(self, minutes: int) -> dict:
        with self._lock:
            conn = self._transaction()
            try:
                state = database.load_state(conn)
                state.now += minutes
                database.set_now(conn, state.now)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._serialize(state)

    def reset_seed(self, scenario: ScenarioIn) -> dict:
        return self.replace_scenario(scenario)
