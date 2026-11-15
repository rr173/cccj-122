# 带逐行来源的查询（`provenance_query/`）

在现有 SQLite 事实仓之上新增一个**只读**的查询入口。它执行一条受限的关系
SELECT，把重复结果压成一个有序的、带存储类别的值元组和它的**精确重数**，
同时给出这个重数如何由基础行推导出来的**来源表达式**。之后只凭结果文件就能：

* 读取压缩后的结果与来源图；
* 给定一组要排除的基础行身份，在来源表达式上重新求值，得到删除这些行以后
  每个既有结果的重数（允许变成 0）。

这两项操作只依赖结果文件，源库搬走或删除后仍然可用；原库不会被写入，也不会
产生 WAL / `-shm` / journal 侧车文件。

本文档解释一次**读取时点**的查询推导：不读取 WAL、不登记人工加工关系、
不统计历史提交、不调整已接入的事实冲突。

---

## 1. 安装与依赖

```
Python  >= 3.9（本仓库验证于 3.11）
SQLite  >= 3.37（验证于 3.40；需要生成列与 STRICT 之外的标准特性）
sqlglot == 30.31.0
```

`sqlglot` 是唯一第三方依赖，仅用于 **SQL 解析**（`sqlglot.parse(read="sqlite")`）。
标量语义（类型亲和性、比较、三值逻辑、COLLATE、数值转换）**全部交给 SQLite
本体**在私有临时工作区里求值，不由本包重写。

```bash
python3 -m pip install sqlglot==30.31.0
```

> 本仓库的演示与测试以隔离模式运行（`python3 -I`）；若在同一环境里用 `-I`
> 执行，请确保 `sqlglot` 位于系统 site-packages（`-I` 不加载用户 site）。

---

## 2. 概念

### 2.1 基础行令牌（token）

每个**基础表行**有一个令牌，由

```
表身份（普通表的规范名） + 按声明主键顺序排列的完整主键值（带类型）
```

组成。同一基础行经不同别名（自连接、CTE 两次引用、UNION ALL 两次）引用时是
**同一个令牌**；不同基础行是不同的令牌。主键值的类型是 SQLite 存储类别：
NULL、整数、浮点、文本、BLOB 各自区分。

基础表必须是**具有显式 PRIMARY KEY 的普通表**，且该主键在库中非空且唯一
（由 UNIQUE 索引保证）；SQLite 隐藏的 `rowid` 不被当作永久身份。视图、虚拟表
和表函数都不是基础表。

### 2.2 来源表达式（代数）

| 构造 | 表达式 |
|---|---|
| 基础行扫描 | 令牌 |
| 常量查询（无 FROM） | `1` |
| INNER JOIN / CROSS JOIN | 左推导 `×` 右推导 |
| UNION ALL | 左推导 `+` 右推导 |
| WHERE / 投影 | 推导不变 |
| 无推导 | `0` |

性质：

* 所有令牌取 1 时，表达式的值等于原查询结果的重数；
* 指定要排除的令牌取 0、其余取 1，就得到删掉这些行以后每个结果的重数；
* 加法、乘法与令牌存成**可共享的有向无环图**（DAG），节点按结构驻留
  （intern）共享，不展开成乘积项，也不为每条推导复制整棵树；
* 同一个令牌可以在自连接中多次相乘，**不是集合、不去重**；
* 计数使用任意精度非负整数（Python `int`，文件中存为十进制文本）。

例如两行 `a1,a2` 与两行 `b1,b2` 交叉连接、投影成同一个值：

```
原重数 4  ==  (a1 + a2) × (b1 + b2)
只排除 a1 -> 2        （(a2)×(b1+b2)）
排除 a1 与 b1 -> 1    （a2×b2）
同分支 UNION ALL 两次 -> 8
同表两次 CROSS JOIN -> (a1+a2)²；排除 a1 -> 1（不是 2）
```

### 2.3 紧凑性：为什么不会展开

每个节点在暴露结果之前就按**带类型的值元组**合并重复，注解取各推导之和。
因此把 2000 条基础行投影成同一个常量元组的 CTE 只有**一行**，其注解是 2000
个令牌之和；对该 CTE 再做六次 CROSS JOIN 只是把这一个节点乘六次——仍然是
**一个结果元组**，重数为 `2000^6`。中间笛卡尔积从不逐行展开。

---

## 3. 支持的 SQL 子集

命令接收库路径和一个**只含单条查询**的 SQL 文件，用 SQLGlot 以 SQLite 方言
解析，显式按 SQLite 的名字与表达式语义绑定。

支持：

* 普通表扫描、`SELECT` 投影（含 `*` 与 `别名.*`）；
* `WHERE`；
* `INNER JOIN … ON` 与 `CROSS JOIN`；
* 非递归、按声明顺序可见的 CTE；
* `FROM` 派生表（可带列名列表）；
* `UNION ALL`；
* 标量表达式：限定列与星号、显式别名、数值与文本常量、比较、`AND/OR/NOT`、
  基础算术与位运算、`||`、`CASE`、`CAST`、`COALESCE`、`NULLIF`、`LIKE`、
  `GLOB`、`IN (值列表)`、`BETWEEN`、`IS [NOT]`、`COLLATE`。

规则：

* 输出列必须有**唯一且非空**的名称；非列引用的表达式必须显式命名（`AS`）；
* CTE 可声明按位置改名的列名列表，数量必须相符；派生表亦可；
* 星号按可见输入的列顺序展开；`UNION ALL` 按位置合并、沿用第一分支名称；
* 列引用不能越过子查询作用域；同名无限定列必须报**歧义**；
* 双引号标识符与单引号字符串区分；表名/列名可含空格；
* 空文本与空 BLOB 是不同值；重复元组按带类型的值判等；
* `UNION ALL` 同一列位置的各分支若**亲和性或排序规则不一致**，该查询被拒绝。
  SQLite 文档明确说明此时该列的亲和性是**不确定的**（甚至在同一条查询内
  可能因比较位置而变化），因此本工具不猜规则、也不近似成某一种；同一列
  位置分支一致（例如都是 TEXT、都是 NUMERIC，或都是表达式）时正常工作。

**明确拒绝**（不近似成支持的操作）：

`DISTINCT`、聚合、窗口函数、`GROUP BY/HAVING`、`ORDER BY`、`LIMIT/OFFSET`、
外连接（`LEFT/RIGHT/FULL`）、`NATURAL`/`USING` 连接、`EXISTS` 及一切表达式内
子查询、递归 CTE、集合差/交（`EXCEPT/INTERSECT`）、`UNION`（去重语义）、
表函数/虚拟表/视图、用户函数、`VALUES` 列表、绑定参数，以及任何写语句。

同样拒绝隐藏在 CTE 或派生表里的这些结构。对于无 FROM 的常量查询，
`WHERE` 仍适用（三值逻辑下仅保留为真的行）。

---

## 4. 结果文件格式

结果文件是一个独立 SQLite 数据库，可重新打开：

| 表 | 内容 |
|---|---|
| `result_meta` | 格式版本、构建时间、SQL 文本、源库路径、值列数、结果元组数、原重数总和、图节点/边/令牌数 |
| `result_columns` | 有序输出列：名称、亲和性、排序规则 |
| `result_rows` | 每个不同值元组一行：序号、带类型值元组的 JSON、**十进制文本重数**、图根节点 |
| `result_terms` | 每个作为令牌参与的基础行：令牌号、表名、表身份、主键列名、带类型主键值 JSON |
| `graph_nodes` | 共享 DAG：节点号、种类（one/zero/token/add/mul）与至多两个子节点 |

值的编码（`values.py`）区分五类存储类别：

```
NULL   -> n
整数   -> i:<十进制>          （保持任意精度，不经过 JSON 数字）
浮点   -> f:<IEEE-754 位模式 hex>（NaN/Inf/-0.0 可往返）
文本   -> t:<原文>             （空文本合法）
BLOB   -> b:<base64>           （空 BLOB 合法，且与空文本不同）
```

重数始终以**十进制字符串**保存，任何 JSON 消费者都不会丢精度。

### 原子性

构建先写入同目录的临时文件，**全部成功后才 `os.replace` 到目标路径**。失败
（不支持 SQL、坏主键、求值错误、目标已存在）会删除临时文件并返回非零，绝不
留下可正常读取的半份结果。目标已存在时默认拒绝覆盖（`--overwrite` 显式允许）。

---

## 5. 读取时点与只读保证

构建在一个 `BEGIN` 只读事务内完成：目录读取、基础行令牌、整条查询求值都在
同一快照里，**不会把不同提交的基础行混进一个来源结果**。源库以
`file:…?mode=ro` 打开；所有工作表和表达式节点落在私有 TEMP 模式。

---

## 6. Python 接口

```python
from provenance_query import (
    ProvenanceQueryBuilder,
    ProvenanceResultReader,
    evaluate_exclusion,
)

# 构建
builder = ProvenanceQueryBuilder("warehouse.sqlite")
report = builder.build("query.sql", "result.sqlite")
print(report.result_rows, report.original_multiplicity_total)

# 读取（只需结果文件）
with ProvenanceResultReader("result.sqlite") as reader:
    reader.raw_compressed()      # 结果元组 + 重数 + 列 + 元数据
    reader.base_identities()     # 每个图叶对应的真实表与主键
    reader.graph()               # 共享 DAG（可在排除集上求值）
    reader.validate()            # 校验保存的重数与图表达式一致

# 排除求值（只需结果文件；不修改它）
outcome = evaluate_exclusion("result.sqlite", ["left_t(id=1)", "right_t(id=2)"])
print(outcome["total_before"], outcome["total_after"])
```

排除身份写作 `表(列=值, ...)`，值用带类型字面量：`42`、`1.5`、`'文本'`、
`X'00ff'`、`NULL`。同一身份重复只排除一次；**不在本次输入快照中的身份报调用
错误**，不会被悄悄当作已排除。

---

## 7. 命令行

```bash
python3 -m provenance_query.cli build  warehouse.sqlite query.sql result.sqlite
python3 -m provenance_query.cli read   result.sqlite            # 人类可读
python3 -m provenance_query.cli read   result.sqlite --json     # 原始压缩 JSON
python3 -m provenance_query.cli read   result.sqlite --identities
python3 -m provenance_query.cli read   result.sqlite --graph
python3 -m provenance_query.cli eval   result.sqlite --exclude "left_t(id=1)" \
                                                   --exclude "right_t(id=1)"
python3 -m provenance_query.cli verify result.sqlite
```

退出码：

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `2` | 调用错误（参数/输入文件/未知或畸形排除身份） |
| `3` | 不支持的 SQL |
| `4` | 构建失败（坏主键、求值错误、目标已存在、结果文件损坏） |

---

## 8. 演示

```bash
python3 -I demo/run_provenance_query_demo.py
```

演示内容：

1. 两张小表讲清来源含义：
   * 交叉连接投影同值 -> 重数 4，表达式等价于 `(a1+a2)×(b1+b2)`；
     只排除 a1 -> 2，同时排除 a1 与 b1 -> 1（并要求与"真实删行后重跑 SQL"
     逐项一致）；
   * 同一分支 `UNION ALL` 两次 -> 8（排除 a1 -> 4，a1+b1 -> 2）；
   * 同表两次 `CROSS JOIN` -> `(a1+a2)²`；排除 a1 -> **1**（共享令牌不被当集合）；
   * 打印图结构与十六种排除子集的实际求值，不只打印最终行数。
2. 用**现有 XBRL 接入器**建立真实事实仓，查询连接 `facts`、`sources`、
   `contexts`，证明图叶的主键确实对应仓库中的输入行（逐行回查 `COUNT(*)=1`），
   并证明排除整个来源与真实删除该来源后的 SQL 结果逐项一致。

---

## 9. 测试

```bash
python3 -m unittest discover -s tests            # 全仓库 298 项
python3 -m unittest tests.test_provenance_query_*  # 新增 87 项
```

新增测试的独立预期：

* 同一 SQL 直接在原 SQLite 上执行，**逐行转成带类型的多重集合**（不经过
  来源引擎）；
* 在另一个库副本上**真实删除**选定行后再执行 SQL，与来源图的排除求值逐项
  核对；
* 覆盖：多层 CTE、前层常量投影后的重复合并、复合主键、自连接、`UNION ALL`
  不同分支重名但按位置对齐、带空格标识符、NULL 连接与空关系、列歧义、主键空值、
  未知排除身份、后向/递归引用、隐藏在 CTE 里的不支持结构。

### 天文数量验收

`tests/test_provenance_query_scale.py`：2000 条基础行 -> CTE 投影成一个常量
元组 -> 对该 CTE 六次 `CROSS JOIN`。

* 结果只有 **1** 个元组，原重数 `2000^6 = 64 000 000 000 000 000 000`；
* 排除一个基础令牌后重数恰为 `1999^6`；
* 记录表达式节点/边数、峰值 RSS 与实际耗时（实测：约 4000 节点 / 6000 边、
  ~30 MB、~0.1 s）；
* 输入行翻倍后图规模约线性增长（2000 -> 4000 行：4006 -> 8006 节点）；
* **不运行也不展开**普通查询的天文级结果。

---

## 10. 模块结构

```
provenance_query/
  cols.py        # 列元数据（名称、亲和性、排序规则、是否带亲和性）
  values.py      # 带类型值与稳定键
  graph.py       # 令牌全域 + 共享来源 DAG + 非递归求值
  catalog.py     # 基础表元数据与显式主键校验
  analysis.py    # 子集校验、作用域解析、CTE 引用顺序校验
  render.py      # 标量表达式 -> 暂存列上的 SQL 文本
  runtime.py     # 暂存表与关系节点（扫描/过滤/投影/连接/并）
  compile.py     # AST -> 计划树
  resultfile.py  # 结果文件 schema 与原子发布
  reader.py      # 读取、图重建、排除求值
  service.py     # 构建入口
  cli.py         # 终端命令
tests/test_provenance_query_*.py
demo/run_provenance_query_demo.py
```

原有入口（接入、导出、计算检查、备份、WAL 解码）保持不变。
