# 受控分析查询（`controlled_query/`）

在同一份 SQLite 事实仓之上新增一个**本地受控分析查询入口**。管理员提供
**关系目录**（哪些公开关系、每个关系的可查询字段、每行属于哪个企业的
路径）和**授权策略**（身份、组、按企业二元组的 full / summary / deny
授权）；调用者明确指定一个**已登记身份**和一条 SQL，程序在该身份可见
的数据上执行查询并返回带类型的结果。

## 信任边界

* 这条 API/命令接收的 **SQL 是不受信输入**。防护不依赖字符串黑名单：
  通过结构检查的语句在一个**私有的内存 SQLite 数据库**上执行，该库只
  含为此身份物化的授权关系——物理表、视图、元数据表和其他企业的行在
  执行环境里**根本不存在**，任何形状的 SQL 都摸不到它们。
* 结构检查（SQLGlot 解析 + 显式允许清单）是第二道防线：让支持的语法
  范围明确、遇到不支持的结构**响亮地拒绝**，而不是静默忽略或降级。
* 持有宿主数据库文件的人本就可以自行打开文件；本入口面向"只给查询、
  不给文件"的分析人员，不需要登录服务器。
* 目录与策略文件由管理员掌握，与调用者分离；程序不修改源表、源结构
  或持久策略，也不在源库创建任何辅助对象（以 `mode=ro` 打开，不产生
  `-wal` / `-shm` / journal 侧车文件）。

## 依赖

```
Python >= 3.9（验证于 3.11）；SQLite >= 3.37；sqlglot == 30.31.0
```

SQLGlot 只用于结构检查；所有标量与聚合语义由 SQLite 本体在私有库上
求值。

## 关系目录（catalog JSON）

```json
{
  "version": 1,
  "relations": [
    {
      "name": "fact_lines",
      "source": "facts",
      "fields": [
        {"name": "fact id", "column": "id"},
        {"name": "concept"},
        {"name": "raw value", "column": "value_text"}
      ],
      "owner": {
        "path": [
          {"table": "contexts",
           "left": ["context_digest"], "right": ["context_digest"]}
        ],
        "entity": {"scheme": "entity_scheme", "identifier": "entity_identifier"}
      },
      "summary_hidden": ["raw value"]
    }
  ]
}
```

* `name` 是公开关系名，**必须与源物理表名不同**，也不得与库中任何
  表/视图同名（不分大小写），不得以 `sqlite_` 开头；公开名之间不得
  重复。
* `fields` 是可查询字段（`column` 缺省等于 `name`，可改名）。**未公开
  的字段对所有身份都不存在**，不能用于 WHERE 或计算。
* `owner.path` 是找到行属主的**多对一关联链**：每跳列出当前侧
  （`left`）与目标表（`right`）按位置对应的键，支持复合键与多跳；
  路径为空表示源表直接带企业列。`owner.entity` 给出路径终点表上的
  两个 TEXT 列。企业身份始终是 `(scheme, identifier)` **二元组**，
  全程不拼接成字符串，不同 scheme 相同 identifier 不会碰撞。
* `summary_hidden` 列出 summary 级别下要隐藏的公开字段。

### 目录校验（执行用户查询之前完成）

结构层（不连库）：JSON 形状、键数量相符、路径无环、公开名无重复。
模式层（在查询的读取快照内）：源表与路径目标必须是 `main` 库中的
**普通表**（拒绝视图、虚表）；被引用的列必须存在、不得是生成列、
必须是 BINARY 排序规则，声明类型必须是普通类型名（可带 `(n)` /
`(n,m)`）；企业列必须是 TEXT 亲和性。数据契约（同一
读取时点）：每一跳的目标键必须**非空且唯一**——违反则**整次查询
失败**并定位到具体关系与跳，绝不复制明细或任选一个属主。任一跳的
本方键为 NULL 或找不到目标时，该行没有可确认的属主，**所有身份都
不可见**。源表之间不需要事先声明外键。

## 授权策略（policy JSON）

```json
{
  "version": 1,
  "identities": [
    {"name": "alice", "groups": ["reporting"]}
  ],
  "grants": [
    {"subject": "alice",     "scheme": "...", "identifier": "...", "effect": "full"},
    {"subject": "reporting", "scheme": "...", "identifier": "...", "effect": "summary"}
  ]
}
```

* 每条授权指向一个**已登记身份或组**和一个**完整企业二元组**，效果
  为 `full` / `summary` / `deny`；身份名与组名必须不相交。身份与组的
  名字是精确字符串（区分大小写），与 SQL 标识符的大小写折叠无关。
* 对某个身份和企业，汇总该身份及其各组的所有匹配授权：**有 deny 则
  不可见；没有任何允许项也不可见；有任意 summary 按 summary；只有
  允许项全为 full 才是 full**。结果与授权书写顺序无关。
* **未知身份是调用错误**（退出码 2）；已登记但没有授权的身份得到
  空关系（可查询、恒为空）。
* summary 隐藏发生在**暂存物化时**——隐藏字段在该行上变成 SQL
  NULL，先于用户查询的筛选、连接、分组、聚合与排序。`COUNT(*)` 计入
  可见行，`COUNT(隐藏列)` 不计；`WHERE 隐藏列 = 原值` 选不中它。
* 结果与报告只说明所用身份与公开关系，不回显隐藏原值、未授权企业
  名或他人的授权细节。

## 一次查询的执行过程

1. 在策略中查到身份（未知 → 退出码 2），汇总出该身份的实体效果表；
2. SQLGlot 以 SQLite 方言解析 SQL，按显式允许清单做结构检查（未知
   结构一律拒绝），表名在 CTE 名字作用域下解析到公开关系；
3. 以 `mode=ro` 打开源库并 `BEGIN`：**WAL 模式下这就是一个一致的
   读取时点**，属主链、策略裁定与全部业务表行都对应它，源库可继续
   写入；
4. 在快照内做目录模式校验与目标键非空/唯一校验，然后按属主链
   `LEFT JOIN` 逐行解析属主、应用效果、遮蔽 summary 字段，物化出
   每个公开关系的授权行；
5. 在私有内存库中建出公开关系（只含公开字段，**列声明类型与源列
   一致**，SQLite 类型亲和性因此与"授权后同结构小库"完全相同），
   执行调用者原始 SQL 文本；
6. 结果按列返回带类型值；要求落盘时原子发布（临时文件 +
   `os.replace`，失败不留看似完整的结果文件，默认拒绝覆盖）。

## 支持的 SQL 范围

单条只读语句；非递归 `WITH`（每条 CTE 只见声明在前的 CTE，不见自身；
同层不重名）；`SELECT` 投影（含 `*`、`别名.*`）；`FROM` 派生表；相关
`EXISTS` / `NOT EXISTS`；`IN (SELECT ...)` 与 `IN (值, ...)`；
`INNER` / `CROSS` / `LEFT [OUTER] JOIN ... ON`（逗号连接视同交叉连
接）；`WHERE`；`GROUP BY` / `HAVING`；`UNION ALL`；`ORDER BY` /
`LIMIT` / `OFFSET`；比较（`= != < <= > >= IS [NOT] BETWEEN IN LIKE
[ESCAPE]`）、`AND/OR/NOT`、算术 `+ - * / %`、`||`、一元负号；`CASE`、
`CAST`、`COLLATE`；函数 **COUNT、SUM、AVG、MIN、MAX、COALESCE、
NULLIF、ABS、LENGTH、SUBSTR、TYPEOF**（`IFNULL`、`IIF` 因 SQLGlot 归
一化为 COALESCE/CASE 而同义接受）；数值/文本/BLOB 字面量、`NULL`、
`TRUE/FALSE`；双引号标识符（可含空格）。

明确拒绝（非零退出、无部分结果）：写语句、`PRAGMA`、`ATTACH`、多语
句、`main.` 等模式限定名、物理表与 `sqlite_master`/`sqlite_schema`
等元数据（它们不是公开关系）、表值函数、递归 CTE、标量子查询、
`VALUES`、绑定参数、`DISTINCT`（含聚合内）、窗口函数、`FILTER`、
`RIGHT`/`FULL`/`NATURAL` 连接、`USING`、`UNION`（去重）、
`EXCEPT`/`INTERSECT`，以及一切未批准函数（含 `load_extension`、
`random`）。CTE/别名有自己的名字作用域：合法 CTE 可以与物理表同名
（只遮蔽、不误拒），但用户无法借 CTE 遮蔽、嵌套查询或猜测内部名摸
到原始数据——私有库里只有授权关系。

## 结果编码

结果文件是一个 JSON 文档：所用身份、实际用到的公开关系、SQL 文本、
输出列名、带类型行。值编码与 `provenance_query` 一致：

```
NULL  -> {"t": "null"}
整数  -> {"t": "int",  "v": "<十进制文本>"}   （64 位整数不丢精度）
实数  -> {"t": "real", "v": <JSON 数>}        （非有限值用文本）
文本  -> {"t": "text", "v": "..."}
BLOB  -> {"t": "blob", "v": "<base64>"}       （可逆）
```

## Python 接口

```python
from controlled_query import ControlledQueryService

service = ControlledQueryService("warehouse.sqlite", "catalog.json", "policy.json")
service.check_config()                       # 只校验目录+策略+模式+目标键
report = service.query("alice", sql="SELECT ...", result_path="out.json")
print(report.identity, report.relations, report.columns)
for row in report.rows:                      # 每格是 TyValue（带存储类别）
    print([v.display() for v in row])
```

## 命令行

```bash
python3 -m controlled_query.cli run DB --catalog C.json --policy P.json \
    --identity alice --sql-file q.sql --result out.json
python3 -m controlled_query.cli run DB --catalog C.json --policy P.json \
    --identity alice --sql 'SELECT COUNT(*) AS c FROM fact_lines' --json
python3 -m controlled_query.cli check-config DB --catalog C.json --policy P.json
```

退出码：`0` 成功；`2` 调用错误（参数/文件/未知身份）；`3` 目录、策略
或数据契约错误（含目标键非空/唯一违反）；`4` SQL 超出批准范围；`5`
批准后的语句在授权关系上执行失败（如未知列、歧义）。

## 演示与测试

```bash
python3 -I demo/controlled_query_demo/run_controlled_query_demo.py   # 116 项验收断言
python3    demo/controlled_query_demo/plain_sqlite_oracle.py         # 独立预言机打印预期
bash       demo/controlled_query_demo/run_cli_demo.sh                # 终端走查
python3 -m unittest discover -s tests                                # 全仓库测试
```

演示用**现有 XBRL 接入器**建出两个 scheme 不同但 identifier 相同的真
实实体及事实，`facts` 经 `contexts` 作为一条公开关系；另建带复合引用
与两跳属主路径的业务表（含未匹配行、空键、同名字段、BLOB）。各身份
每张关系应有的行与遮蔽值全部**人工列出**（`expectations.py`），独立
检查脚本 `plain_sqlite_oracle.py` 把这些预期行放进同声明类型的小
SQLite 库、用同一 SQL 算答案——不调用正式权限裁定或关系构造函数。
并发阶段让写进程在一个事务里同时变更两张表的业务标记，用同步点安排
查询跨过该提交，证明一次结果不会拼出两种提交的标记。

## 模块结构

```
controlled_query/
  catalog.py     # 关系目录：结构校验 + 模式校验 + 目标键数据契约
  policy.py      # 身份/组/授权与裁定（deny > summary > full）
  sqlcheck.py    # SQL 结构检查（SQLGlot，CTE 作用域，显式允许清单）
  stage.py       # 快照内属主解析与授权关系物化（summary 遮蔽）
  service.py     # 编排：快照 -> 校验 -> 物化 -> 私有库执行
  resultfile.py  # 结果 JSON 与原子发布
  cli.py         # 终端命令
demo/controlled_query_demo/
  expectations.py            # 人工列出的各身份预期行与演示查询
  plain_sqlite_oracle.py     # 独立检查脚本（纯 SQLite，不用正式实现）
  run_controlled_query_demo.py
  run_cli_demo.sh
tests/test_controlled_query_*.py
```

原有接入、导出、计算核查、备份、WAL 解码、来源查询与分类包入口均保
持不变；本包只**读取**它们使用的仓库，并复用 `provenance_query` 的
带类型值编码与 DDL 解析工具。
