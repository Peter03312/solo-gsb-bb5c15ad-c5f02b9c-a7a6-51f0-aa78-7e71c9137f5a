"""直接针对 planner 的编排语义测试。"""
from __future__ import annotations

import pytest

from app.models import Closure, Edge, Exit, Group
from app.planner import (
    PlanningError,
    closure_conflicts_with_fixed,
    merge_intervals,
    replan,
)


def _edge(id_, s, t, travel, cap):
    return Edge(id_, s, t, travel, cap)


def _groups_dict(*groups: Group) -> dict[int, Group]:
    return {g.id: g for g in groups}


def test_merge_intervals():
    assert merge_intervals([]) == []
    assert merge_intervals([(1, 3), (3, 5)]) == [(1, 5)]  # 左闭右开，相邻合并
    assert merge_intervals([(1, 2), (4, 6), (5, 7)]) == [(1, 2), (4, 7)]


def test_simple_routing_shortest_path():
    # 1 A->B(1min,cap10), 2 B->F(1), 3 A->F(3)，出口 F=1
    edges = {1: _edge(1, "A", "B", 1, 10), 2: _edge(2, "B", "F", 1, 10),
             3: _edge(3, "A", "F", 3, 10)}
    exits = {1: Exit(1, "F")}
    g = Group(1, 1, "A", 0, 1, 10)
    replan(edges, exits, {}, _groups_dict(g), now=0)
    assert g.status == "scheduled"
    assert g.route == [1, 2]
    assert (g.departure, g.arrival) == (0, 2)


def test_closure_delays_departure():
    # 唯一通道 1 A->F 封闭 [0,2)，占用时长 2：需 dep=2 才能走
    edges = {1: _edge(1, "A", "F", 2, 10)}
    exits = {1: Exit(1, "F")}
    closures = {1: Closure(1, 1, 0, 2)}
    g = Group(1, 1, "A", 0, 1, 10)
    replan(edges, exits, closures, _groups_dict(g), now=0)
    assert g.route == [1]
    assert g.departure == 2 and g.arrival == 4


def test_closure_touching_only_end_boundary_is_ok():
    # 封闭 [0,2)，dep=2 时占用 [2,4)，不相交（边界相接合法）
    edges = {1: _edge(1, "A", "F", 2, 10)}
    exits = {1: Exit(1, "F")}
    closures = {1: Closure(1, 1, 0, 2)}
    g = Group(1, 1, "A", 2, 1, 10)
    replan(edges, exits, closures, _groups_dict(g), now=0)
    assert g.departure == 2


def test_capacity_staggers_groups():
    # 单通道 cap=5/分钟，通行 1 分钟：g1=2 人 dep0；g2=3 人若 dep0 恰好满载可同行；
    # 再放一个 g3=3 人（同优先级）则 dep0 超载，需错峰。
    edges = {1: _edge(1, "A", "F", 1, 5)}
    exits = {1: Exit(1, "F")}
    g1 = Group(1, 2, "A", 0, 10, 10)
    g2 = Group(2, 3, "A", 0, 5, 10)
    g3 = Group(3, 3, "A", 0, 5, 10)
    replan(edges, exits, {}, _groups_dict(g1, g2, g3), now=0)
    assert (g1.departure, g1.arrival) == (0, 1)
    assert g2.departure == 0          # 2+3=5 恰好满载
    assert (g3.departure, g3.arrival) == (1, 2)


def test_group_larger_than_capacity_is_blocked():
    edges = {1: _edge(1, "A", "F", 1, 2)}
    exits = {1: Exit(1, "F")}
    g = Group(1, 3, "A", 0, 1, 10)
    replan(edges, exits, {}, _groups_dict(g), now=0)
    assert g.status == "blocked"
    assert g.reason == "no_capacity_before_deadline"


def test_capacity_over_multi_minute_edge():
    # 通行 2 分钟、cap=5：g1=3人 dep0 占用分钟 {0,1}；g2=3人 dep1 时
    # 分钟 1 上合计 6>5，故最早 dep2
    edges = {1: _edge(1, "A", "F", 2, 5)}
    exits = {1: Exit(1, "F")}
    g1 = Group(1, 3, "A", 0, 10, 10)
    g2 = Group(2, 3, "A", 0, 5, 10)
    replan(edges, exits, {}, _groups_dict(g1, g2), now=0)
    assert g1.departure == 0
    assert g2.departure == 2
    assert g2.arrival == 4


def test_priority_and_earliest_departure_order():
    # 单通道 cap=5/分钟通行1分钟，三人均 5 人；低优先级组最早出发也须让路
    edges = {1: _edge(1, "A", "F", 1, 5)}
    exits = {1: Exit(1, "F")}
    g_low = Group(1, 5, "A", 0, 1, 10)
    g_high = Group(2, 5, "A", 3, 10, 10)
    g_mid = Group(3, 5, "A", 2, 5, 10)
    replan(edges, exits, {}, _groups_dict(g_low, g_high, g_mid), now=0)
    # 高优先级 id2 dep3；同级 id3(ed2) 在 id1(ed0) 前
    assert g_high.departure == 3
    assert g_mid.departure == 2
    assert g_low.departure == 0 or g_low.departure >= 4
    assert g_low.arrival <= 10


def test_tie_break_exit_then_edge_sequence():
    # A->F 经出口1（通道3），A->D 经出口2（通道1，1分钟）
    # 到达相同：A->D 1分钟, A->F 通道2 1分钟；出口1胜出
    edges = {
        1: _edge(1, "A", "D", 1, 10),
        2: _edge(2, "A", "F", 1, 10),
    }
    exits = {1: Exit(1, "F"), 2: Exit(2, "D")}
    g = Group(1, 1, "A", 0, 1, 10)
    replan(edges, exits, {}, _groups_dict(g), now=0)
    assert g.route == [2]
    assert g.arrival == 1


def test_blocked_reasons():
    edges = {1: _edge(1, "A", "B", 1, 10)}
    exits = {1: Exit(1, "Z")}
    g = Group(1, 1, "A", 0, 1, 10)
    with pytest.raises(PlanningError):
        # 出口所在节点 Z 不存在 —— 校验拒绝
        replan(edges, exits, {}, _groups_dict(g), now=0)

    exits = {1: Exit(1, "B")}

    # 通道存在但容量无法在截止前满足：通行1分钟、cap=10、5人组其实可行
    g2 = Group(2, 5, "A", 0, 1, 2)
    replan(edges, exits, {}, _groups_dict(g2), now=0)
    assert g2.status == "scheduled"

    # 截止=0：最迟出发 -1 < 下界0，无法安排
    g3 = Group(3, 6, "A", 0, 1, 0)
    replan(edges, exits, {}, _groups_dict(g3), now=0)
    assert g3.status == "blocked"
    assert g3.reason == "no_capacity_before_deadline"

    # 时钟推进越过截止：deadline_already_passed
    g4 = Group(4, 1, "A", 0, 1, 3)
    replan(edges, exits, {}, _groups_dict(g4), now=4)
    assert g4.status == "blocked"
    assert g4.reason == "deadline_already_passed"

    # 起点不可达任何出口
    edges2 = {1: _edge(1, "A", "B", 1, 10), 2: _edge(2, "X", "Y", 1, 10)}
    g5 = Group(5, 1, "X", 0, 1, 10)
    replan(edges2, exits, {}, _groups_dict(g5), now=0)
    assert g5.status == "blocked"
    assert g5.reason == "no_path_to_exit"


def test_group_starting_on_exit_uses_empty_route():
    edges = {1: _edge(1, "A", "F", 1, 10)}
    exits = {1: Exit(1, "F")}
    g = Group(1, 4, "F", 0, 1, 5)
    replan(edges, exits, {}, _groups_dict(g), now=0)
    assert g.status == "scheduled"
    assert g.route == []
    assert g.departure == 0 and g.arrival == 0


def test_clock_advance_fixes_departed_and_closure_conflict():
    edges = {1: _edge(1, "A", "F", 5, 10)}
    exits = {1: Exit(1, "F")}
    g = Group(1, 1, "A", 0, 1, 20)
    groups = _groups_dict(g)
    replan(edges, exits, {}, groups, now=0)
    assert g.departure == 0
    # 推进到 now=2：占用 [0,5)，剩余 [2,5)
    replan(edges, exits, {}, groups, now=2)
    assert g.route == [1] and g.departure == 0  # 已出发，不变

    touch = Closure(2, 1, 3, 8)
    assert closure_conflicts_with_fixed(touch, edges, [g], now=2) == 1
    past = Closure(3, 1, 0, 2)  # 只覆盖过去
    assert closure_conflicts_with_fixed(past, edges, [g], now=2) is None
    future_gap = Closure(4, 1, 5, 8)  # 边界相接，无剩余重叠
    assert closure_conflicts_with_fixed(future_gap, edges, [g], now=2) is None


def test_replan_only_undeparted_groups():
    # g1 dep0 走 5 分钟；now=3 后新增封闭其剩余路线应抛错（由 service 层拒绝）。
    # 此处验证：now 推进不会挪动已出发组，未出发组可被重新安排。
    edges = {1: _edge(1, "A", "F", 5, 1)}
    exits = {1: Exit(1, "F")}
    g1 = Group(1, 1, "A", 0, 10, 20)
    g2 = Group(2, 1, "A", 0, 1, 20)
    groups = _groups_dict(g1, g2)
    replan(edges, exits, {}, groups, now=0)
    first_dep, first_route = g1.departure, list(g1.route)
    replan(edges, exits, {}, groups, now=3)
    assert g1.departure == first_dep and g1.route == first_route
    # g2 未出发：在 now=3 之后找可行出发（cap=1，g1 剩余占用 3、4 分钟）
    assert g2.status == "scheduled"
    assert g2.departure >= 3
    assert g2.departure == 5  # 最早不与 g1 的 [3,5) 剩余占用冲突
