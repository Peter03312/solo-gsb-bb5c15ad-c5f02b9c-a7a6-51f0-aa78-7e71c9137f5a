# 场馆疏散通道编排服务

基于 **Python 3.11 + FastAPI + SQLite** 的疏散通道编排 HTTP 服务。输入有向通道
（通行分钟数、每分钟容量、左闭右开封闭时段）、出口位置与人群组（人数、起点、
最早出发分钟、优先级、规划截止分钟），服务按规则逐组编排路线与出发时间，并在
时间推进、新增组/封闭时对未出发组进行原子重排。

## 一、编排规则

- **组不可拆分**；通行期间每一个整数分钟，同一条通道上“各组人数之和
  ≤ 该通道每分钟容量”。
- 通道有向；通行耗时 `travel_minutes`，组在第 `t` 分钟进入则占用该通道的分钟为
  左闭右开区间 `[t, t+travel)`，第 `t+travel` 分钟到达下一节点。
- 封闭时段 `[start, end)` 为左闭右开；与占用区间相交即冲突（边界相接合法）。
- 每组按顺序贪心安排（前组占用视为既定），顺序为：
  **优先级降序 → 最早出发分钟升序 → 组编号升序**。
- 择优依据（字典序）：**到达分钟最早 → 出口编号最小 → 通道编号序列最小
  → 出发分钟最早**。路线为简单路径，中途不等待（等待等价于整体推迟出发）。
- 当前分钟从 `0` 起。时钟推进后，**已出发组（departure < now）路线与占用固定**；
  新增人群组或封闭时段时只重排未出发组。
- 新增封闭若与任一已出发组的**剩余路线**（`now` 之后仍占用的分钟）相交，
  则整次变更拒绝（HTTP 409），不写入任何数据。
- 每次变更在单个 SQLite 事务内完成“全量重排 + 落库”：并发变更不会超用容量，
  也不会留下只重排一部分组的中间状态。
- 每次成功提交的变更（人群组或封闭增删、场景替换/重置、时钟推进）都会使
  **状态版本号 `state_version` 单调递增**；封闭预演凭据绑定生成时的版本，
  确认时在同一事务内核验内容与版本，任一不符即拒绝且不写入。
- 无法在截止分钟前（到达分钟 `> deadline`）安排的组返回原因码：
  - `no_path_to_exit`：图上不存在到出口的路线；
  - `no_capacity_before_deadline`：容量/封闭限制导致截止前无法安排
    （含组人数超过通道单分钟容量）；
  - `deadline_already_passed`：当前分钟已超过规划截止分钟。

## 二、一键启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://0.0.0.0:8000
```

容器首次启动时自动安装依赖、初始化 SQLite（位于数据卷 `evac-data`，
容器内 `/data/evacuation.db`），并写入内置演示场景完成初始编排。
删除数据卷即可恢复初始演示数据：

```bash
docker compose down -v
```

### 本机直接运行

```bash
python3 -m venv --without-pip .venv        # 若系统自带 pip 可省略 --without-pip
# 引导 pip（仅在 ensurepip 缺失的系统需要）
curl -sSf https://bootstrap.pypa.io/get-pip.py | .venv/bin/python3 -
.venv/bin/pip install -r requirements.txt
EVAC_DB_PATH=./data/evacuation.db .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后：

- API 根地址：`http://127.0.0.1:8000`
- 健康检查：`GET http://127.0.0.1:8000/health`
- 交互式文档（Swagger UI）：`http://127.0.0.1:8000/docs`
- OpenAPI Schema：`http://127.0.0.1:8000/openapi.json`

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EVAC_DB_PATH` | `/data/evacuation.db`（容器内） | SQLite 数据库文件路径，目录自动创建 |
| `EVAC_MAX_PATHS` | `10000` | 单组编排枚举简单路径的数量上限；图极大、提示路径超限时调高 |

## 三、HTTP API

除 `DELETE` 与 `GET /health` 外，请求体均为 JSON。`groups` / `closures`
新增接口既接受单个对象，也接受对象数组（批量原子提交）。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET | `/api/plan` | 查询当前时钟与每组编排结果 |
| POST | `/api/scenario` | 整体替换场景（通道/出口/封闭/组），时钟归 0 并全量编排 |
| POST | `/api/groups` | 新增人群组，仅重排未出发组 |
| DELETE | `/api/groups/{id}` | 删除未出发人群组并重排（已出发组 409） |
| POST | `/api/closures` | 新增封闭时段；触及已出发组剩余路线则 409 整次拒绝 |
| POST | `/api/closures/preview` | **预演**一批封闭对计划的影响（不写入），返回确认凭据 |
| POST | `/api/closures/confirm` | 凭凭据在原子事务内核验内容与版本，重算并提交 |
| DELETE | `/api/closures/{id}` | 删除封闭时段并重排未出发组 |
| POST | `/api/clock/advance` | 时钟向前推进 N 分钟 |
| POST | `/api/reset` | 恢复内置演示场景 |

### 请求字段

**通道 edge**：`id`(正整数)、`source`、`target`（节点编号字符串）、
`travel_minutes`(正整数)、`capacity_per_minute`(正整数)。

**出口 exit**：`id`(正整数)、`node`（出口所在节点）。

**封闭 closure**：`id`(可选，缺省自动分配)、`edge_id`、
`start_minute`、`end_minute`（均为非负整数，要求 `end > start`）。

**人群组 group**：`id`(正整数)、`people`(正整数)、`source`、
`earliest_departure`(≥0)、`priority`(整数，越大越优先)、`deadline`(≥0，且不早于最早出发)。

### 响应示例：`GET /api/plan`

```json
{
  "now": 0,
  "state_version": 0,
  "groups": [
    {
      "id": 1,
      "people": 3,
      "source": "A",
      "earliest_departure": 0,
      "priority": 10,
      "deadline": 30,
      "status": "scheduled",
      "departed": false,
      "route": [4, 5],
      "departure_minute": 0,
      "arrival_minute": 8,
      "reason": null,
      "reason_text": null
    }
  ]
}
```

- `route` 为通道编号序列；起点本身位于出口节点时为 `[]`，
  出发/到达分钟均为最早出发分钟与 0 中的可行值（通常等于最早出发分钟）。
- `status=scheduled`：已安排；`status=blocked`：截止前无法安排，
  见 `reason` / `reason_text`。
- `departed=true` 表示该组在当前时钟下已出发，后续变更不会再改动它。

错误响应：参数结构错误返回 `422`；语义错误（编号重复、引用不存在等）
返回 `400 {"error":"bad_request",...}`；封闭触及已出发组或删除已出发组等
结构性冲突返回 `409 {"error":"planning_conflict",...}`；预演凭据不存在/已使用、
内容或版本不符返回 `409 {"error":"credential_invalid",...}`。

### 封闭预演与凭据确认（值班员流程）

值班员封闭通道前可先**预演**对疏散计划的影响，确认无误后再凭凭据**原子提交**。
预演完全不写入状态；字段、左闭右开规则、编号分配、冲突检测与重排规则与
`POST /api/closures` 完全一致（单个对象或数组均可）。

**第一步：预演** `POST /api/closures/preview`

请求体同 `POST /api/closures`。响应：

```json
{
  "committed": false,
  "token": "u3C9w...",
  "state_version": 7,
  "now": 4,
  "closures": [
    {"id": 3, "edge_id": 1, "start_minute": 10, "end_minute": 20}
  ],
  "plan": { "now": 4, "state_version": 7, "groups": [ ... ] },
  "changes": [
    {
      "id": 2,
      "before": {"status": "scheduled", "route": [1], "departure_minute": 4,
                 "arrival_minute": 6, "reason": null, "...": "..."},
      "after":  {"status": "scheduled", "route": [4, 5], "departure_minute": 4,
                 "arrival_minute": 12, "reason": null, "...": "..."}
    },
    {
      "id": 5,
      "before": {"status": "scheduled", "...": "..."},
      "after":  {"status": "blocked", "route": [], "departure_minute": null,
                 "arrival_minute": null, "reason": "no_capacity_before_deadline"}
    }
  ]
}
```

- `token`：绑定“该批封闭内容 + 当前 `state_version`”的确认凭据（仅存于服务进程
  内存，进程重启即失效）；`closures[].id` 为提交时将分配的编号。
- `plan`：**拟执行后的完整编排**，结构与 `GET /api/plan` 相同。
- `changes`：**逐组变化**，仅列出受影响组（按编号升序），每项给出 `before`/`after`
  的出发分钟、路线、到达分钟，或由可安排变为受阻的原因；未变化的组不出现。
- 封闭与任一**已出发组剩余占用**冲突时返回 `409 planning_conflict`，
  **不生成凭据**（响应中无 `token`），整批不落库。

**第二步：确认提交** `POST /api/closures/confirm`

```json
{
  "token": "u3C9w...",
  "closures": [ {"edge_id": 1, "start_minute": 10, "end_minute": 20} ]
}
```

- `closures` 可选：回传时逐条核对内容（编号不参与，须与预演一致，单对象或数组
  均可）；缺省时仅核对凭据内绑定的内容。
- 在**同一原子操作**（互斥锁 + 单 SQLite 事务）内：核对内容与 `state_version`，
  按现行优先级、容量与固定路线规则重算并提交，返回
  `{"committed": true, "token": ..., "now": ..., "state_version": ..., "groups": [...]}`。
- 预演之后只要发生过任何已提交变更——**人群组或封闭增删、场景替换或重置、
  时钟推进**——版本即不再匹配，确认返回 `409 credential_invalid` 且不写入；
  凭据**一次性**，确认成功或失败后均不可复用，需重新预演。

## 四、调用示例

```bash
# 纯 curl（零额外依赖）
bash examples/curl_examples.sh

# Python（仅标准库）
python3 examples/call_api.py http://127.0.0.1:8000
```

自定义环境变量 `BASE=http://host:port bash examples/curl_examples.sh` 可改地址。

## 五、内置演示场景

节点 `A~F`，出口 `1=F`、`2=D`；通道 1（A→B）在 `[0,10)` 封闭：

```
      1(3min,cap10)        2(3min,cap8)
  A ──────────────► B ──────────────► C ──3(2min,cap5)──► F [出口1]
  │                 │                                  ▲
  │4(5min,cap6)     │6(2min,cap10)                     │7/8(2min)
  ▼                 ▼                                  │
  E ──────────────► D [出口2] ─────────────────────────┘
      5(3min,cap8)
```

初始 4 个组的编排结果可通过 `GET /api/plan` 查看；推进时钟后再新增封闭，
可观察“整次拒绝”保护。

## 六、测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

测试覆盖：最短路径与封闭推迟、左闭右开边界、每分钟容量错峰（含跨多分钟通道）、
优先级/同级排序、出口与通道序列平局裁决、时钟推进后已出发组固定、
封闭触及剩余路线的冲突检测、批量变更原子性，以及 10 并发新增组不超卖容量；
另覆盖封闭预演不写状态、逐组差异、冲突不发凭据，以及凭据确认在时钟推进、
组/封闭增删、场景替换/重置后版本失效与并发确认的一次性语义。

## 七、目录结构

```
app/
  main.py       FastAPI 路由与启动（自动建库、首次播种）
  schemas.py    Pydantic 入参模型
  models.py     领域数据模型
  planner.py    编排核心（路径枚举、禁入区间合并、逐组贪心、全量重排）
  service.py    事务与并发锁、原子变更编排
  database.py   SQLite 建表与读写
  seed.py       内置演示场景
examples/       curl 与 Python 调用示例
tests/          算法与 HTTP API 测试
Dockerfile, docker-compose.yml
```
