# 山火指挥部 · 现场离线调度

现场断网时，调度员按**现场单号**登记火线长度、风向、任务区和资源占用；回网后把离线批次交给系统合并。

## 核心规则

1. **按单号幂等合并**：同一单号重复回传且内容一致（规范化哈希）→ 沿用首次结果（`reused`），正式记录不动。
2. **内容不同不覆盖**：哈希不一致 → 挂待核对（`needs_review`），保留首次正式记录，由 `incident_commander` 裁决（apply 更新 / reject 驳回）。
3. **资源占用单赢家**：同一资源同时只允许一笔有效占用（数据库唯一索引兜底并发）；落败方在 409 响应中拿到占用对象（holder）和重新计算后的可用资源清单。
4. **原子写入与保留下次重试**：资源占用、任务状态、离线条目在同一事务入库；写入失败整体回滚占用，批次保留原始 payload 为 `pending_retry`，可凭 `batch_no` 原样重试（提交返回 202，成功返回 200）。
5. **输入改动触发结论失效重算**：火线长度、风向（含风速）、任务区改动后，该单号关联任务的结论按新输入重算，`done` 任务退回 `active`；其他单号照常。任务结论带 `basis_hash`，读取时还会标记 `stale`。

## 分层结构（规则 / 存储 / 入口分开）

- `src/domain.py`：取值域、数据校验、错误类型（冲突携带结构化 payload）。
- `src/rules.py`：纯规则——风险评估、下风向、规范化内容哈希、任务结论 basis、角色矩阵、可用资源、状态机。
- `src/repository.py`：SQLite 建表、事务、唯一占用索引、批次合并/回滚/保留、待核对、审计哈希链。
- `src/service.py`：入参规范化、用例编排、冲突落败方反馈、结论重算。
- `src/http_api.py`：JSON 路由与统一错误响应。
- `src/audit.py`：UTC 时间与 SHA-256 审计事件。
- `app.py`：参数解析、依赖组装、HTTP 服务启动。
- `static/index.html`：最小演示页。

角色：`field_commander`（登记/离线回传/占用/改动）、`logistics`（资源注册/占用）、`incident_commander`（注册资源、改动、关单、核对裁决、审计）、`viewer`（只读/审计）。用 `X-Actor`、`X-Role` 请求头传身份。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/resources` | 注册资源（logistics/incident_commander） |
| GET | `/api/resources` | 资源、当前可用资源、活动占用 |
| POST | `/api/tickets` | 在线直接登记（任一资源被占则整笔不成立） |
| GET | `/api/tickets` / `/api/tickets/{no}` | 单号列表/详情（含任务、占用、stale 标记） |
| POST | `/api/tickets/{no}/occupations` | 占用资源；冲突返回 409 + holder + 可用资源 |
| POST | `/api/tickets/{no}/amend` | 改动火线/风向/任务区，关联任务结论重算 |
| POST | `/api/tickets/{no}/close` | 关单（任务须 done/cancelled，占用须释放） |
| POST | `/api/tasks/{id}/transition` | 任务状态推进 pending→active→done，可 cancel |
| POST | `/api/occupations/{id}/release` | 释放占用 |
| POST | `/api/batches` | 提交离线批次（成功 200，待重试 202） |
| POST | `/api/batches/{batch_no}/retry` | 重试 `pending_retry` 批次 |
| GET | `/api/batches` / `/api/batches/{no}` | 批次及条目结果 |
| GET | `/api/reviews` | 待核对列表 |
| POST | `/api/reviews/{id}/resolve` | `{"decision":"apply|reject"}`（incident_commander） |
| GET | `/api/audit` | 审计事件（哈希链） |

离线批次请求体：

```json
{"entries":[{"ticket_no":"WF-1","fireline_length_km":3,"wind_direction":"N",
  "wind_speed_kmh":10,"zone_kind":"forest","zone_name":"东坡","note":"",
  "tasks":[{"name":"巡线","status":"active"}],
  "occupy_resources":["E1"]}]}
```

风向为八方位 `N/NE/E/SE/S/SW/W/NW`；任务区 `forest/residential/critical_infra`。
条目结果 `applied / reused / needs_review`，资源落败记录在该条目的 `blocked`，并附 `available_resources`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
