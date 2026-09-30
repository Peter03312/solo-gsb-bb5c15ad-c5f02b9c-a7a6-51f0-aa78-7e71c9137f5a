"""领域模型：通道、出口、封闭时段、人群组、系统状态。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Edge:
    id: int
    source: str
    target: str
    travel_minutes: int
    capacity_per_minute: int


@dataclass(frozen=True)
class Exit:
    id: int
    node: str


@dataclass(frozen=True)
class Closure:
    id: int
    edge_id: int
    start_minute: int
    end_minute: int


@dataclass
class Group:
    id: int
    people: int
    source: str
    earliest_departure: int
    priority: int
    deadline: int
    # 编排结果：route 为通道编号序列（起点即出口时为 []）
    route: list[int] = field(default_factory=list)
    departure: int | None = None
    arrival: int | None = None
    status: str = "unscheduled"  # scheduled / unscheduled / blocked
    reason: str | None = None


@dataclass
class State:
    edges: dict[int, Edge]
    exits: dict[int, Exit]
    closures: dict[int, Closure]
    groups: dict[int, Group]
    now: int = 0
    # 状态版本号：任何已提交变更（组/封闭增删、场景替换/重置、时钟推进）后单调递增；
    # 预演凭据绑定生成时的版本，确认时版本不一致即拒绝。
    version: int = 0
