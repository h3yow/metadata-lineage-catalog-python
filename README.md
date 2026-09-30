# 元数据血缘目录服务（Metadata Lineage Catalog）

让上游系统注册进来的**数据集、字段及其血缘关系**可以被查询和追踪；
对**重复上报、多来源上报**给出确定、可解释的结论；对不合法上报返回**可区分的拒绝原因**，
不静默忽略；血缘中的环、重复边、悬挂引用都会被**明确标记**，查询不会死循环。

## 目录结构

```
metadata_catalog/
  errors.py    # RejectionReason 枚举、Rejection、异常
  models.py    # Dataset / FieldInfo / NodeRef、字段类型校验
  audit.py     # 追加写审计日志：谁、什么时候、改了什么
  store.py     # 注册与确定性合并规则、多条件检索
  lineage.py   # 血缘图：结构/悬挂/成环校验、双向 BFS 展开
  service.py   # 门面类 MetadataCatalogService（推荐入口）
main.py        # FastAPI HTTP 接口
tests/         # 38 个用例，覆盖重复上报 / 字段冲突 / 血缘成环 / 检索 / 审计 / HTTP
```

## 快速开始

```bash
uv sync
uv run pytest -q                      # 运行全部测试
uv run python main.py                 # 启动 HTTP 服务（127.0.0.1:8000，含 /docs）
```

## 一、实体注册与合并规则

### 数据集

- **身份键**：`name` 去首尾空白后大小写敏感。首次上报创建，之后重复上报走更新。
- **owners / tags / sources**：一律取**并集**。因此两个来源以任意顺序上报，
  最终状态完全一致（见 `test_merge_order_independence`）。
- **description**：取“**最新一次非空值**”；空值/None 不覆盖旧值。

### 字段（随数据集批量上报，逐字段独立判定）

好字段收下、坏字段拒绝，一次批次的结果同时返回 `added_fields` / `merged_fields` /
`rejections`，不会因为一个坏字段丢掉整批：

| 情况 | 结论 | 拒绝原因（RejectionReason） |
| --- | --- | --- |
| 新字段，类型合法 | 收下 | — |
| 已存在、类型相同、新来源 | 合并（sources 并集） | — |
| 已存在、类型不同 | **拒绝，保留旧类型，不污染来源** | `field_type_conflict` |
| 字段名为空 | 拒绝 | `field_name_missing` |
| 类型为空 / 不支持 | 拒绝 | `field_type_missing` |
| 同一批次内字段名重复 | 拒绝 | `field_duplicated_in_batch` |
| 字段已被软删除后又收到上报 | 拒绝，需先显式复活 | `field_removed_then_reported` |
| 数据集名为空 | 整次请求 422 | `dataset_name_missing` |

- 字段类型比较前先 `strip().upper()` 归一化，故 `bigint` 与 `BIGINT` 是同一类型，
  不会误判为冲突。允许的类型见 `models.ALLOWED_FIELD_TYPES`。
- 字段删除是**软删除**（`remove_field`），复活需显式调用 `restore_field`。

每个 `Rejection` 都带 `reason / message / subject / details`：例如类型冲突时
`details` 里同时给出 `existing_type` 与 `reported_type`，判定依据可直接展示。

## 二、血缘语义

边是**有向边**：`upstream（生产者） -> downstream（消费者）`。

写入时按顺序校验：

1. **端点同构**：数据集只能连数据集，字段只能连字段（且字段归属的数据集/字段须存在）。
   不一致 → `lineage_kind_mismatch`；自己连自己 → `lineage_cycle`。
2. **重复边**：相同端点对、相同 `source` → `duplicate_edge`，不会产生第二条边；
   相同端点对但**新来源**不算重复，来源并入该边的 `sources`。
3. **悬挂边**：任一端点当前不存在（或字段已删除）时，边**不丢弃、不报错**，
   以 `status=dangling` 保留并附 `dangling_detail`；它不参与上下游展开。
   端点后续迟到注册 / 字段复活后会自动重新校验，合法即转为 `active`。
4. **成环检测**：只有两端都能解析时才检测。若“下游沿现有 active 边能走回上游”，
   则新边会成环，拒绝为 `lineage_cycle`，并在 `details.cycle_path`
   返回环上的节点路径（如 `c -> a -> b -> c`）。**成环的边不写入图。**

查询：

- `upstream_datasets/downstream_datasets`（字段同理）做双向 BFS 展开，
  返回节点、距起点的 `depth` 和可达 `path`；结果按 `(depth, node_id)` 稳定排序。
- 展开算法维护 `visited` 集合，**即使图里已存在环也会收敛，不会死循环**；
  `max_depth` 可截断。
- 可通过 `all_edges(include_dangling=False)` 只看有效边，
  `dangling_edges()` 单独列出悬挂边。

## 三、检索行为

`search_datasets(name_contains=..., tags=..., owners=..., source=...)`：

- **条件之间是 AND**；`tags`/`owners` 列表内部是 **OR**（命中任一即可）。
- `name_contains` 忽略大小写、去首尾空白，做子串匹配。
- `source` 按上报来源过滤。
- 结果固定按数据集名排序，同样输入永远得到同样顺序的输出。

## 四、变更审计（谁 / 什么时候 / 改了什么）

审计日志只增不改，每个动作记录 `seq / timestamp / actor / source / action /
target / changes / note`：

- `dataset_registered / dataset_updated`、`field_added / field_merged /
  field_removed / field_restored`、`lineage_added / lineage_marked_dangling /
  lineage_resolved`，以及所有 `rejection`。
- `changes` 给出字段级旧值/新值（如 owners 从 `["alice"]` 变为
  `["alice","bob"]`）。
- 查询：`dataset_history(name)`、`field_history(dataset, field)`、
  `latest_change(target)`；HTTP 见 `GET /history/dataset/{name}`。
- 时钟可注入（测试中使用单调递增的固定时钟），时间顺序可断言。

## 五、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/datasets` | 注册/更新数据集与字段；逐字段返回 `rejections` |
| GET  | `/datasets/{name}` | 数据集详情 |
| GET  | `/datasets?tag=&owner=&name_contains=&source=` | 组合检索（tag/owner 可重复传参） |
| POST | `/lineage/datasets` | 写数据集血缘；重复边/成环返回 409 与原因 |
| POST | `/lineage/fields` | 写字段血缘 |
| GET  | `/lineage/datasets/{name}/upstream|downstream` | 双向展开（`?max_depth=n`） |
| GET  | `/lineage/edges?include_dangling=true` | 列出边 |
| GET  | `/history/dataset/{name}` | 数据集变更历史 |
| GET  | `/health` | 健康检查 |

错误约定：数据集名为空等整体拒绝返回 **422**；血缘重复/成环返回 **409**；
实体不存在返回 **404**。响应体统一为 `{"rejections": [{"reason", "message",
"subject", "details"}]}`。

## 六、验证方法

```bash
# 全量单测（38 个用例）
uv run pytest -q

# 打开测试日志，可见每条用例打印的"输入 -> 判定依据"
uv run pytest -o log_cli=true -o log_cli_level=INFO

# 只跑需求点名的三类场景
uv run pytest tests/test_registration_merge.py   # 重复上报 / 合并
uv run pytest tests/test_field_conflicts.py     # 字段冲突 / 缺失 / 删除再报
uv run pytest tests/test_lineage.py             # 成环 / 重复边 / 悬挂 / 双向展开
```

单测日志示例：

```
用例[血缘成环] 输入=已有 a->b->c，再写 c->a => 判定=ok=False,
  reason=lineage_cycle, path=dataset:c -> dataset:a -> dataset:b -> dataset:c
用例[字段类型冲突] 输入=已存在 f1:int，再次上报 f1:string =>
  判定=reason=field_type_conflict, details={existing_type=INT, reported_type=STRING}
```

## 设计边界

当前实现为**进程内内存存储**，服务重启不保留数据；并发写入、持久化与分页
不在本版本范围内。核心规则全部集中在 `store.py` 与 `lineage.py`，
替换存储实现时不需要改动合并/血缘判定。
