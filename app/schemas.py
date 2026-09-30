"""HTTP 入参校验（Pydantic v2）。所有时间均为整数分钟，封闭时段左闭右开。"""
from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

# 正整数字段
PositiveInt = Annotated[int, Field(ge=1)]
NonNegInt = Annotated[int, Field(ge=0)]


class EdgeIn(BaseModel):
    id: int = Field(ge=1, description="通道编号，正整数")
    source: str = Field(min_length=1, description="起点节点编号")
    target: str = Field(min_length=1, description="终点节点编号")
    travel_minutes: PositiveInt = Field(description="通行分钟数（正整数）")
    capacity_per_minute: PositiveInt = Field(description="每分钟容量（正整数）")


class ExitIn(BaseModel):
    id: int = Field(ge=1, description="出口编号，正整数")
    node: str = Field(min_length=1, description="出口所在节点")


class ClosureIn(BaseModel):
    id: int | None = Field(default=None, ge=1, description="封闭记录编号，缺省自动分配")
    edge_id: int
    start_minute: NonNegInt
    end_minute: NonNegInt

    @model_validator(mode="after")
    def _check_half_open(self) -> "ClosureIn":
        if self.end_minute <= self.start_minute:
            raise ValueError("封闭时段必须满足 end_minute > start_minute（左闭右开）")
        return self


class GroupIn(BaseModel):
    id: int = Field(ge=1, description="人群组编号，正整数")
    people: PositiveInt
    source: str = Field(min_length=1)
    earliest_departure: NonNegInt = Field(description="最早出发分钟")
    priority: int = Field(description="优先级，数值越大越优先")
    deadline: NonNegInt = Field(description="规划截止分钟，到达时刻须 <= 该值")

    @model_validator(mode="after")
    def _check_window(self) -> "GroupIn":
        if self.deadline < self.earliest_departure:
            raise ValueError("规划截止分钟不能早于最早出发分钟")
        return self


class ScenarioIn(BaseModel):
    edges: list[EdgeIn]
    exits: list[ExitIn]
    closures: list[ClosureIn] = Field(default_factory=list)
    groups: list[GroupIn] = Field(default_factory=list)


class ClockIn(BaseModel):
    minutes: PositiveInt = Field(description="向前推进的分钟数")


class ConfirmIn(BaseModel):
    token: str = Field(min_length=1, description="预演接口返回的确认凭据")
