# 可审计的批量数据导出交付（本地模拟器）

为数仓提供的批量导出交付功能的最小可运行实现：**本地导出模拟器 + SQLite 状态库**，
不连接真实仓库或网络。调用方提交查询版本、字段投影、过滤条件和目标接收方；系统生成
有序分片清单及每片摘要；接收方逐片返回带分片身份的回执；只有全部分片回执与本次导出
清单匹配后，导出任务才进入交付完成状态。

## 目录结构

```
export_delivery/
  models.py     # 请求身份、分片摘要、状态枚举、错误类型
  warehouse.py  # 本地仓库模拟器：确定性数据 + 显式查询版本
  receiver.py   # 接收方模拟器：可注入丢片 / 错误摘要 / 重复回执
  store.py      # SQLite 状态库：任务、分片清单、发送日志、回执
  service.py    # 导出服务：提交、派发、回执、撤销、恢复、审计
tests/
  test_export_delivery.py
```

## 核心设计

### 稳定请求身份（幂等提交）
`ExportRequest`（查询版本 + 投影 + 过滤条件 + 接收方 + 分片大小）经规范化 JSON
（键排序、紧凑分隔符）后取 SHA-256 作为 `job_id`。相同请求永远映射到同一个导出任务，
重复提交直接返回已有任务；过滤条件的键顺序不影响身份，投影顺序、版本、接收方等任一
变化都会产生新身份。

### 有序分片清单与每片摘要
提交时按钉住的查询版本取数，结果按主键 `id` 全局排序后切成有序分片，清单落库
（`shards` 表：序号、行偏移、行数、摘要）。每片摘要绑定请求身份与分片序号：

```
sha256(canonical_json({request_id, shard_ordinal, row_count, rows}))
```

空结果导出在提交时即进入 `DELIVERED`（零分片 = 零待确认回执），清单与请求定义照常保留。

### 数据版本不能悄悄混入
- 任务创建时钉住 `query_version`，之后每次派发都按钉住版本重新取数并**重算摘要与清单
  比对**；源头数据被静默改写会抛出 `SourceDataChangedError`，而不是混入新数据。
- 上游切换到新版本后，进行中的导出仍从钉住版本的快照切片，正常完成。
- 同一逻辑请求换版本提交是一个**新任务**，与旧任务互不影响。

### 回执处理
- 回执携带分片身份（`job_id` + 序号）与摘要；可重复、可乱序。
- 完全相同的回执重放是幂等 no-op；已确认分片的新回执记为 `DUPLICATE`。
- 摘要与清单不符 → 分片与任务进入 `CONFLICT`，冲突回执连同双方摘要一并留档。
- 未知分片、接收方不符、任务已撤销、分片未派发 → `REJECTED`，同样留档。
- 全部分片 `ACKED` 后任务才进入 `DELIVERED`。

### 部分重试、撤销与恢复
- `dispatch_unacknowledged()` 只发送未确认分片（`PENDING`/`SENT`），已确认分片绝不
  重发；每次发送记入 `send_log`（含第几次尝试）。
- `revoke()` 撤销未完成的交付（幂等）；已完成任务不可撤销；迟到回执被拒绝但保留。
- 全部状态在 SQLite 中，新进程打开同一状态库即可 `recover_incomplete()` 找到未完成的
  任务并继续派发，崩溃前的清单与回执完整保留。

### 审计
`audit_trail(job_id)` 返回查询定义、分片清单、发送日志与全部回执（含重复/冲突/拒绝），
回执通过 `job_id` + 分片序号与清单关联。

## 状态机

```
任务:  PREPARED → DELIVERING → DELIVERED   (终态)
                          └──→ CONFLICT → REVOKED (终态)
       PREPARED/DELIVERING ──→ REVOKED     (终态)
分片:  PENDING → SENT → ACKED
                  └──→ CONFLICT
```

## 运行测试

```bash
cd batch_export
python3 -m unittest discover -v
```

覆盖场景：空结果、跨分片排序稳定性、重复回执（重放/新回执/接收方级）、乱序回执、
错误摘要进冲突、数据版本变化（不混入 + 静默改写检测）、部分失败后只重发未确认分片、
撤销（含撤销后重提交保持身份）、进程恢复，以及请求身份稳定性与各类非法回执守卫。


---

## 本地 XBRL 2.1 实例接入（`xbrl_ingest/`）

除原有导出交付模拟器外，项目新增独立的本地 XBRL 接入模块：把普通 XBRL 2.1
实例解析成带业务坐标（实体/期间/维度/单位/语言）的事实，原子地存入 SQLite，
并按坐标查询、追溯原始事实。它不修改导出交付服务，也不扩充发送或回执协议。

```bash
python3 -m unittest discover -v        # 19 个原有测试 + 56 个新增测试
python3 demo/run_demo.py               # 小型实例 + 独立手写预期，逐项核对
```

支持范围、身份规则、命令行与取舍详见 [`xbrl_ingest/README.md`](xbrl_ingest/README.md)。


---

## 本地计算关系核查（`calc_check/`）

在已接入的 SQLite 事实库之上，新增一个**只读**的计算关系核查入口。调用者给出
本地 calculation linkbase XML、一份显式 `#片段ID → 展开概念名` 映射表和选中的
来源 ID，即可得到每条关系在每个业务坐标上的 `consistent` /
`inconsistent` / `indeterminate` 结论与闭区间解释。它不重新接入实例、不补数、
不改值，也不把任何结果写回事实库（以 SQLite `mode=ro` 打开，无 wal/journal 侧
车文件）；接入与导出入口保持不变。

```bash
python3 -m unittest discover -v        # 129 个测试（原 75 + 新增 54）
python3 -I demo/run_calc_demo.py       # 手写关系+事实，96 项手写断言逐项核对
bash demo/run_calc_cli_demo.sh         # 终端命令端到端走查（含退出码区分）

python3 -m calc_check.cli check warehouse.sqlite calculations.xml \
    --mapping locator_mapping.json --dict concepts.json \
    --source SRC1 --source SRC2 --report report.json
```

退出码区分三类结果：`2` 调用错误、`3` 关系定义错误（核查未运行）、
`4` 业务不相容（`inconsistent`）、`0` 无不相容（可能含 `indeterminate`）。
精度采用明确的封闭区间口径 `[v-0.5×10^(-d), v+0.5×10^(-d)]`（INF 为点区间），
全程精确 Decimal 运算，结论不构成对财务真实性的证明。支持范围、链接库 XML
结构、映射表格式、priority/prohibition/消重/环规则与报告字段详见
[`calc_check/README.md`](calc_check/README.md)。
