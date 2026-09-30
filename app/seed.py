"""初始演示场景：首次启动且数据库为空时写入。

拓扑（节点 A~F，出口 1=F、出口 2=D）：

        (1,3min,cap10)          (2,3min,cap8)
    A ───────────────► B ───────────────► C ──(3,2min,cap5)──► F  [出口1]
    │                 │                                   ▲
    │(4,5min,cap6)    │(6,2min,cap10)                     │(7,2min,cap8)
    ▼                 ▼                                   │
    E ───────────────► D ───────────────► ════════════════╘══
        (5,3min,cap8)      [出口2]      (8,2min,cap6 直达 F)

通道 1 在 [0,10) 封闭（左闭右开）。
"""
from __future__ import annotations

from .schemas import ClosureIn, EdgeIn, ExitIn, GroupIn, ScenarioIn


def seed_scenario() -> ScenarioIn:
    return ScenarioIn(
        edges=[
            EdgeIn(id=1, source="A", target="B", travel_minutes=3, capacity_per_minute=10),
            EdgeIn(id=2, source="B", target="C", travel_minutes=3, capacity_per_minute=8),
            EdgeIn(id=3, source="C", target="F", travel_minutes=2, capacity_per_minute=5),
            EdgeIn(id=4, source="A", target="E", travel_minutes=5, capacity_per_minute=6),
            EdgeIn(id=5, source="E", target="D", travel_minutes=3, capacity_per_minute=8),
            EdgeIn(id=6, source="B", target="D", travel_minutes=2, capacity_per_minute=10),
            EdgeIn(id=7, source="D", target="F", travel_minutes=2, capacity_per_minute=8),
            EdgeIn(id=8, source="D", target="F", travel_minutes=2, capacity_per_minute=6),
        ],
        exits=[ExitIn(id=1, node="F"), ExitIn(id=2, node="D")],
        closures=[ClosureIn(id=1, edge_id=1, start_minute=0, end_minute=10)],
        groups=[
            GroupIn(id=1, people=3, source="A", earliest_departure=0, priority=10, deadline=30),
            GroupIn(id=2, people=5, source="A", earliest_departure=0, priority=5, deadline=20),
            GroupIn(id=3, people=4, source="B", earliest_departure=1, priority=5, deadline=25),
            GroupIn(id=4, people=2, source="E", earliest_departure=0, priority=1, deadline=12),
        ],
    )
