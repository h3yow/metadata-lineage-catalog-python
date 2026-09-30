# 元数据血缘目录服务（Metadata Lineage Catalog）

一个内存实现的元数据目录服务：让多个上游系统注册**数据集**、**字段**以及它们之间的
**血缘关系**，在重复上报、多来源冲突、血缘成环等情况下给出**确定、可解释**的结论，
并提供双向血缘展开、组合检索和完整的变更审计。

- 语言/框架：Python 3.14 + FastAPI
- 持久化：进程内内存（无外部依赖），服务重启数据清空
- 测试：pytest（35 个用例）

## 1. 快速开始

```bash
uv sync                       # 安装依赖
uv run python main.py         # 启动服务（127.0.0.1:8000）
uv run pytest                 # 运行全部测试（-s 会打印每条用例的输入与判定依据）
```

启动后访问 `http://127.0.0.1:8000/docs` 可见 OpenAPI 文档。

## 2. 实体合并规则

所有上报都先做合法性校验；不合法上报**不会被静默忽略**，而是返回带 `error` 原因码、
`message` 说明与 `details` 上下文的结构化错误（HTTP 422）。

### 2.1 拒绝原因码（可区分）

| 原因码 | 触发场景 |
| --- | --- |
| `dataset_name_empty` | 数据集名称为空白 |
| `dataset_not_found` | 引用了不存在的数据集 |
| `field_name_empty` | 字段名缺失 |
| `field_type_empty` | 字段缺少类型声明 |
| `field_not_found` | 引用了数据集下不存在的字段 |
| `field_type_conflict` | 同名字段以不同类型重复上报（类型不可变） |
| `field_duplicate_in_batch` | 一次全量快照里同名字段出现多次 |
| `field_missing_on_update` | 全量快照遗漏了既有字段（禁止静默丢字段） |
| `source_conflict` | 跨来源上报了不同 owner；或从未上报过的来源发起更新 |
| `edge_endpoint_not_found` | 血缘端点实体不存在 |
| `edge_self_loop` | 血缘边的上下游是同一实体 |
| `edge_duplicate` / `edge_cycle` / `edge_kind_conflict` | 以边标记形式呈现，见第 3 节 |

### 2.2 数据集重复上报的确定性合并

设数据集首次由来源 S1 上报：

1. **名称为空白** → 直接拒绝。
2. **首次上报** → 创建，记录权威来源 `source=S1` 与 `sources={S1}`。
3. **同来源重复上报**：
   - owner/description/tags 完全一致 → 幂等，不修改数据，仅留一条
     `register_dataset_idempotent` 审计；
   - owner 不同（仍是 S1）→ 允许后写覆盖 owner。
4. **不同来源重复上报（S2≠S1）**：
   - owner 不同 → `source_conflict` 拒绝（跨来源不允许变更 owner）；
   - owner 相同 → 接受合并：
     - `tags`：取**并集**（只增不删）；
     - `description`：**非空者先写为准**，后到的空/非空值都不覆盖已有非空描述；
     - `sources`：累积为 `{S1, S2}`；
     - owner 保持不变。

### 2.3 字段规则

- 字段必须有非空 `name` 与 `data_type`，否则分别返回 `field_name_empty` /
  `field_type_empty`。
- **字段类型不可变**：任何来源以不同 `data_type` 上报同名字段 →
  `field_type_conflict`，旧类型保留，错误 `details` 同时给出既有类型、上报类型与来源。
- 同类型重复上报：幂等合并（tags 取并集、description 先写非空者为准、sources 累积）。
- **全量快照更新**（`PATCH /datasets/{name}`，带 `fields_snapshot`）：
  - 只有在 `sources` 中的来源可以提交；
  - 快照中每个字段都要合法且名字唯一；任何字段类型与既有冲突 → 整次拒绝；
  - 快照必须包含**全部既有字段**，缺失即 `field_missing_on_update`；
  - 以上全部通过才落库——**整次更新是原子的**，不允许静默写丢一半。

## 3. 血缘语义

- 边方向 `upstream -> downstream`：**上游产出的数据被下游消费**。
- 支持数据集级与字段级两种端点（字段引用名约定为 `dataset.field`）。

### 3.1 写入规则

1. 自环（上下游同一实体）→ `edge_self_loop` 拒绝。
2. 任一端点当前不存在 → `edge_endpoint_not_found` 拒绝，`details` 指明缺失端，
   服务不会登记“写入即悬空”的边。
3. 同 `(upstream, downstream)` 的重复边**不会插入第二条**，而是在原边上叠加标记并留审计：
   - 恒加 `duplicate`；
   - 若重复上报来自不同来源，再加 `kind_conflict`（边保留，来源分歧可解释、可排查）。
4. 成环的新边**既不拒绝也不丢弃**：边正常写入，图中所有参与环的边统一带上 `cycle`
   标记；`GET /lineage/cycles` 可列出全部简单环。

### 3.2 边标记（flags）

| 标记 | 含义 |
| --- | --- |
| `cycle` | 该边参与至少一个环 |
| `duplicate` | 同方向同端点被重复上报 |
| `kind_conflict` | 重复上报来自不同来源 |
| `dangling` | 端点实体当前不存在（边仍保留） |

`POST /lineage/refresh-dangling` 会重新扫描全图：端点缺失的边叠加 `dangling`，
实体重新注册后标记可自动清除。

### 3.3 双向展开

- `GET /lineage/{kind}/{name}/upstream`：谁的数据流入该实体（逐级上游）。
- `GET /lineage/{kind}/{name}/downstream`：该实体流向了谁（逐级下游）。
- 遍历采用 **BFS + visited 集合**，每个实体在结果中只出现一次（距离取最短跳数），
  并支持 `max_depth` 上限——**有环时不会死循环**。
- 结果按 `(距离, 实体层级, 名称)` 稳定排序；经过异常边到达的节点，其结果项会带上
  对应 flags，便于解释血缘为何可疑。

## 4. 检索行为

`POST /search` 接受组合条件，条件之间为 **AND**：

| 条件 | 语义 |
| --- | --- |
| `name_contains` | 数据集名称子串匹配，**大小写不敏感** |
| `tags` | 数据集必须包含**全部**给定标签（子集匹配） |
| `owner` | owner 精确相等 |
| `field_name_contains` | 任一字段名包含该子串（大小写不敏感） |

结果恒按数据集名称字典序返回；相同输入在任何时候顺序一致。

## 5. 审计（最近一次变更是谁、何时、改了什么）

每次注册、合并、字段新增、快照更新、血缘写入/重复上报都会追加一条不可变审计记录：

```json
{
  "sequence": 2,
  "timestamp": "2026-10-01T...",
  "actor": "bob",
  "action": "merge_dataset_report",
  "target": "dataset:orders",
  "changes": [{"path": "tags", "old": ["t1"], "new": ["t1", "t2"]}]
}
```

- `actor` 缺省取上报来源 `source`；
- `target` 形如 `dataset:orders`、`field:orders.id`、`edge:a->b`；
- 幂等重复上报也会留痕（`*_idempotent`），`changes` 为空；
- `GET /audit` 可查全部，`GET /audit?target=dataset:orders` 可只查某实体，
  结果按 `sequence` 升序。

## 6. HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/datasets` | 注册/重复上报数据集（合并或拒绝） |
| PATCH | `/datasets/{name}` | 更新元数据 / 提交全量字段快照 |
| GET | `/datasets`、`/datasets/{name}` | 列表 / 详情 |
| POST | `/fields` | 登记单个字段（合并或冲突拒绝） |
| POST | `/lineage/edges` | 写入血缘边（重复打标、成环打标） |
| GET | `/lineage/edges` | 列出全部边及其标记 |
| GET | `/lineage/{kind}/{name}/{upstream\|downstream}` | 双向血缘展开 |
| GET | `/lineage/cycles` | 列出全部环 |
| POST | `/lineage/refresh-dangling` | 重扫悬空边标记 |
| POST | `/search` | 组合条件检索 |
| GET | `/audit` | 变更历史（可按 target 过滤） |

## 7. 验证方法

```bash
uv run pytest                 # 全部 35 个用例，-v -s 已在 pyproject 中默认开启
uv run pytest tests/test_entities_duplicate.py   # 仅重复上报
uv run pytest tests/test_entities_conflict.py    # 仅字段冲突/非法上报
uv run pytest tests/test_lineage.py              # 仅血缘成环/重复/悬空
uv run pytest tests/test_search_audit.py         # 检索与审计
uv run pytest tests/test_api.py                  # HTTP 端到端
```

测试覆盖重点：

- **重复上报**：同来源幂等、跨来源同 owner 合并标签、跨来源 owner 冲突拒绝、
  字段同类型合并、审计可追溯（`test_entities_duplicate.py`）。
- **字段冲突**：名称/类型缺失、类型冲突保留旧值、快照丢字段原子拒绝、
  未知来源更新拒绝、数据集不存在与字段不存在原因码可区分（`test_entities_conflict.py`）。
- **血缘成环**：闭合边打 `cycle` 而不丢弃、环上双向遍历不死循环且结果稳定、
  重复边打标不增边、自环拒绝、悬空端点拒绝、实体消失后 `dangling` 标记、
  多环检测（`test_lineage.py`）。

每条用例都通过 `caselog.log_case` 在日志中打印**输入**与**判定依据**（以及关键结果），
形如：

```
用例: 字段类型冲突
  输入 field          = 'id'
  输入 existing_type  = 'bigint'
  输入 incoming       = ('string', 'spark')
  判定依据: 字段 data_type 不可变：bigint != string -> FIELD_TYPE_CONFLICT，保留旧类型
  结果: {'reason': 'field_type_conflict', 'details': {...}}
```

### 手工冒烟（curl）

```bash
curl -s -X POST localhost:8000/datasets -H 'Content-Type: application/json' \
  -d '{"name":"orders","owner":"alice","source":"hive","tags":["finance"]}'
curl -s -X POST localhost:8000/lineage/edges -H 'Content-Type: application/json' \
  -d '{"upstream_kind":"dataset","upstream_name":"orders",
       "downstream_kind":"dataset","downstream_name":"orders","source":"s"}'
# => 422 {"error":"edge_self_loop", ...}
```

## 8. 代码结构

```
catalog/
  errors.py     # RejectReason 原因码枚举 + CatalogError
  models.py     # DatasetInfo / FieldInfo / LineageEdge / EntityRef / AuditEntry
  audit.py      # AuditLog 顺序审计
  entities.py   # EntityRegistry 注册/合并/拒绝规则（含全量快照原子性）
  lineage.py    # LineageGraph 边写入、BFS 双向展开、环检测、flags
  search.py     # SearchCriteria 组合检索
  service.py    # CatalogService 门面
  api.py        # FastAPI 路由与错误序列化
main.py         # uvicorn 入口
tests/          # pytest 用例 + caselog 日志辅助
```
