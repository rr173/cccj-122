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


---

## 事实仓灾备演练（`warehouse_backup/`）

在已接入的 SQLite 事实仓之上，新增**可重复演练**的灾备能力：生产接入还在
运行时（WAL 模式、持续写入均可）用 SQLite 在线备份 API 取出一致映像，按
65536 字节定长切块、按内容摘要复用地保存多份有独立编号的备份；后续备份可
显式指定父备份，只登记变化的块，未变块沿父链继承。恢复只凭备份目录与离线
目标路径：逐层解析父链、逐块与整映像校验、SQLite 完整性检查全部通过后才
原子交出；保留清理先预览再应用，父链依赖与跨分支共享块绝不误删，损坏的仓
拒绝破坏性操作。它不筛选业务行、不发送分片、不重做接入或核查，也不修改
既有事实表。

```bash
python3 -m unittest discover -v        # 169 个测试（原 129 + 新增 40）
python3 demo/run_backup_drill.py       # 灾备演练：12 个阶段、163 项检查

python3 -m warehouse_backup.cli create  backups --db warehouse.sqlite
python3 -m warehouse_backup.cli create  backups --db warehouse.sqlite --parent 1
python3 -m warehouse_backup.cli check   backups
python3 -m warehouse_backup.cli restore backups --id 2 --target restored.sqlite
python3 -m warehouse_backup.cli prune   backups --keep 2 --keep 4 --apply
```

退出码：`2` 调用错误、`3` 备份仓损坏、`4` 目标写出失败。目录格式、父链
块来源规则、离线恢复前提、保留语义与演练各阶段说明详见
[`warehouse_backup/README.md`](warehouse_backup/README.md)。


---

## 离线 WAL 变更解码（`wal_decode/`）

新增一个**离线**的逐事务行变更解码入口：给定一份起点主库、随后产生的原生
SQLite WAL 与追踪表/行身份列，只凭 WAL 中的**页版本**（无 SQL 文本、无预置
INSERT/UPDATE/DELETE 事件）按真实提交边界重建逐事务的行净变化流。它校验 WAL
32 字节头与逐帧盐值/滚动校验和（区分大端字段与 magic 决定的校验字节序，支持
4096/65536 页长与两种合法 magic），在工具自有临时目录把已提交页覆盖到工作映像后
用只读 SQLite 查询差异，从不把原库+WAL 交给 SQLite 自动恢复、不生成 `-shm`、
不一次性读入整个 WAL，也不堆积每个提交的全表行。

```bash
python3 -m unittest discover -v          # 211 个测试（原 169 + 新增 42）
python3 -I demo/wal_decode_drill/run_wal_drill.py   # 146 项端到端验收检查

python3 -m wal_decode.cli build-manifest --db base.sqlite --wal c.sqlite-wal \
    --table facts=id --output manifest.json
python3 -m wal_decode.cli decode --manifest manifest.json --out out/
```

三类结果区分退出码：`0` 完整解析（空 WAL、无变化提交也算成功）、`10` 保留完整
提交但尾部未完成（输出带 `INCOMPLETE.txt` 标记）、`3` 损坏/不支持、`2` 输入校验
错误。净变化只能描述提交前后差异，**不能反推逐语句历史**。采集前提（TRUNCATE
checkpoint 后复制、连接存活、关自动 checkpoint、单 checkpoint 周期、静止点复制）、
输出字段与限制详见 [`wal_decode/README.md`](wal_decode/README.md)。该入口不是备份
存储、恢复命令或日志消费状态库，原有四个入口均保持不变。


---

## 带逐行来源的查询（`provenance_query/`）

在已接入的 SQLite 事实库之上，新增一个**只读**的带逐行来源的查询入口。它执行
一条受限的关系 SELECT，把重复结果压成一个带存储类别的值元组和**精确重数**，
并给出可计算的来源表达式（基础行令牌上的加/乘共享 DAG）。之后只凭结果文件就能
读取压缩结果与来源图，或指定要排除的基础行、在表达式上重新求值，得到删行后
每个既有结果的重数；源库搬走或删除后仍然可用。它不读取 WAL、不登记人工加工
关系、不统计历史提交、不调整已接入的事实冲突，也不修改原库。

```bash
python3 -m unittest discover -s tests      # 298 个测试（原 211 + 新增 87）
python3 -I demo/run_provenance_query_demo.py   # 33 项手写断言，含真实 XBRL 仓

python3 -m provenance_query.cli build  warehouse.sqlite query.sql result.sqlite
python3 -m provenance_query.cli read   result.sqlite --json
python3 -m provenance_query.cli eval   result.sqlite --exclude "facts(id=1)"
python3 -m provenance_query.cli verify result.sqlite
```

退出码：`2` 调用错误、`3` 不支持的 SQL、`4` 构建失败。支持范围、来源代数、
结果文件格式、排除身份语法、只读快照保证与天文数量紧凑性验收详见
[`provenance_query/README.md`](provenance_query/README.md)。原有接入、导出、
计算检查、备份与 WAL 解码入口均保持不变。


---

## 离线 XBRL Taxonomy Package 接入（`taxonomy_package/`）

新增一个**离线的 taxonomy package 登记目录**：把符合 XBRL Taxonomy Package
1.0 布局的 ZIP 以原始 ZIP 的 SHA-256 登记，严格定位共同顶层目录下的
`META-INF/taxonomyPackage.xml` 与 `META-INF/catalog.xml`，按命名空间 URI
读取 identifier/名称/版本/入口（入口 href 折叠生效的 `xml:base`），只支持
OASIS `rewriteURI` 且对未知目录规则明确拒绝；再从指定入口 schema 出发，
解析 import/include/redefine 与 linkbase reference 的**实际依赖闭包**。
相对引用先按文档逻辑 URI 求绝对 URI，再做最长前缀 rewrite，绝不先当本地
文件名，也不为无匹配规则的公开 URI 联网。绝对路径、盘符、`..` 越界、重复
规范化路径与符号链接成员全部拒绝；同摘要重复接入复用身份不复制，同
identifier/version 不同内容按版本冲突拒绝。目录原子发布、拒绝覆盖，离线
核验能对缺文件、字节改动、解析关系不一致报出具体成员。它不读企业实例、
不写事实仓、不做计算核查，原有六个入口均保持不变。

```bash
python3 -m unittest tests.test_taxonomy_package_parsing \
                     tests.test_taxonomy_package_service \
                     tests.test_taxonomy_package_cli -v   # 74 个新增测试
python3 -I demo/taxonomy_package_demo/run_taxonomy_demo.py  # 41 项端到端验收
bash demo/taxonomy_package_demo/run_taxonomy_cli_demo.sh    # 终端退出码走查

python3 -m taxonomy_package.cli init     REGISTRY
python3 -m taxonomy_package.cli register REGISTRY PKG.zip
python3 -m taxonomy_package.cli list     REGISTRY --json
python3 -m taxonomy_package.cli show     REGISTRY --digest D --json
python3 -m taxonomy_package.cli resolve  REGISTRY --digest D --json
python3 -m taxonomy_package.cli verify   REGISTRY --json
python3 -m taxonomy_package.cli query    REGISTRY --digest D --uri URI --json
```

退出码：`2` 调用错误、`3` 登记目录损坏、`4` 包格式/解析失败、`5` 原子发布
失败。目录格式、URI 重写与越界防护、依赖环处理、离线搬迁方法与支持范围详见
[`taxonomy_package/README.md`](taxonomy_package/README.md)。


---

## 受控分析查询（`controlled_query/`）

在同一份 SQLite 事实仓之上新增一个**本地受控分析查询入口**：管理员提供
关系目录（公开关系、可查询字段、行属主 `(scheme, identifier)` 的关联路径）
和授权策略（身份、组、按企业二元组的 full / summary / deny），调用者明确
指定一个已登记身份和一条 SQL，程序在该身份可见的数据上执行查询并返回带
类型结果。防护不靠字符串黑名单：通过结构检查（SQLGlot，显式允许清单）
的语句在**只含授权行的私有内存 SQLite 库**上执行，物理表在执行环境里
根本不存在；summary 遮蔽在暂存时完成，先于筛选、连接、分组、聚合与排序；
属主链、策略裁定与全部业务行对应 WAL 下的同一个读取时点。它不修改源库、
不产生侧车文件，原有七个入口均保持不变。

```bash
python3 -m unittest discover -s tests                              # 481 个测试
python3 -I demo/controlled_query_demo/run_controlled_query_demo.py  # 116 项验收断言
bash demo/controlled_query_demo/run_cli_demo.sh                     # 终端走查

python3 -m controlled_query.cli run warehouse.sqlite \
    --catalog catalog.json --policy policy.json \
    --identity alice --sql-file query.sql --result out.json
python3 -m controlled_query.cli check-config warehouse.sqlite \
    --catalog catalog.json --policy policy.json
```

退出码：`2` 调用错误（含未知身份）、`3` 目录/策略/数据契约错误、`4` SQL
超出批准范围、`5` 执行失败。目录与策略格式、属主路径与目标键契约、支持
的 SQL 子集、结果编码与信任边界详见
[`controlled_query/README.md`](controlled_query/README.md)。
