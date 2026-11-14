# 离线 WAL 变更解码（`wal_decode/`）

在已接入的 SQLite 事实仓之上，新增一个**离线**的逐事务行变更解码入口。给它
一份起点主库、随后产生的原生 SQLite WAL 文件，以及要追踪的表和行身份列，它
只根据 WAL 文件中的**页版本**，按真正的提交边界重建逐事务的行净变化流。输入
没有 SQL 文本，也没有预先准备好的 INSERT/UPDATE/DELETE 事件；本工具不解析
SQL，只校验 WAL 二进制、按提交覆盖工作映像，再用只读 SQLite 查询每个提交后的
行集做差异。

它**不是**新的备份存储、恢复命令或日志消费状态库：不保存多份映像、不维护重送
账本/修订号/跨进程接手状态、不自动重试。原有的事实接入（`xbrl_ingest`）、导出
交付（`export_delivery`）、计算核查（`calc_check`）和灾备（`warehouse_backup`）
入口全部保持不变。

```bash
python3 -m unittest discover -v          # 211 个测试（原 169 + 新增 42）
python3 -I demo/wal_decode_drill/run_wal_drill.py   # 146 项端到端验收检查
```

---

## 1. 输入前提与采集操作（写实）

解码正确性依赖一组明确的采集前提，这些是**采集方的责任**：

1. 用同一个写连接把库切到 WAL 模式，并关闭自动 checkpoint
   （`PRAGMA wal_autocheckpoint=0`）；
2. 起点内容就绪后执行一次 `PRAGMA wal_checkpoint(TRUNCATE)`：此时所有已提交页
   都在主库内，WAL 被截为零长度；
3. **保持同一写连接存活**，复制主库作为起点主库（baseline）；
4. 之后在该连接上产生的、属于**这一个 checkpoint 周期**的新 WAL，在每个定义好
   的静止点（无打开事务，通常在 COMMIT 之后）复制为交付的 WAL 文件。

明确**不涵盖**：

* 已被循环覆盖的旧 WAL（WAL 复位会换盐值，旧帧不属于本周期）；
* 采集中仍在变化的文件（复制必须发生在静止点）。

### 采集清单（manifest）

清单登记两个文件的**字节长度与 SHA-256**，以及每个追踪表**按顺序排列的身份
列**：

```json
{
  "manifest_version": 1,
  "database": {"path": "...", "byte_length": 126976, "sha256": "..."},
  "wal":      {"path": "...", "byte_length": 255472, "sha256": "..."},
  "tracked_tables": [
    {"table": "facts", "identity_columns": ["id"]},
    {"table": "aux",   "identity_columns": ["tenant", "code"]}
  ]
}
```

用采集侧子命令生成：

```bash
python3 -m wal_decode.cli build-manifest \
    --db baseline.sqlite --wal cycle.sqlite-wal \
    --table facts=id --table aux=tenant,code \
    --output manifest.json
```

工具在解码前重新哈希两个文件并与清单核对。**摘要只能证明交付的文件与清单一
致**；WAL 文件头本身不能证明它与任意主库配对——两份文件确实属于同一起点是采集
纪律保证的，工具另外会核对 WAL 与主库页长等可机械检查的一致性。

### 只读边界

* 输入一律以只读方式读取；所有 SQLite 访问都走 `immutable=1` 只读 URI；
* **不使用也不生成**输入旁的 `-shm`；若输入旁已存在 `-shm` 会直接拒绝；
* 从不把原库和 WAL 一起交给 SQLite 做自动恢复；
* 仅在工具自己的临时目录里维护一个工作映像，结束即清理，该映像不是对外产品。

### 支持范围（在读业务行之前明确诊断）

仅接受当前 SQLite 3、未加密、UTF-8、固定结构的**普通表**。身份列必须存在、非空
（NOT NULL 或主键，且基线数据中无 NULL）、组合唯一（PRIMARY KEY 或覆盖身份列的
UNIQUE 索引）。表名/列名按真实标识符校验后再加引号，**不能拼成任意 SQL**。

虚表、视图、附加数据库在读取任何业务行之前就报错拒绝；WAL 途中出现建表、改列等
结构变更，直接报 `unsupported`，不会沿用旧列序号继续解释值。

---

## 2. WAL 是怎么被真正理解的

* 校验 32 字节 WAL 头：magic、格式版本、页长、盐值与**头校验和**；
* 逐帧惰性读取（从不一次性读入整个 WAL）：页号、提交后的数据库页数、盐值、滚动
  校验和；
* 区分文件字段的**大端表示**与由 magic 决定的**校验计算字节序**
  （`0x377F0682` 小端累加、`0x377F0683` 大端累加），支持 512…65536 的合法 2 的
  幂页长，验收实跑 4096 与 65536；
* 页号为 0、帧越界（写页超过提交声明页数）、盐值变化、完整帧校验和不符，都抛出
  带**字节偏移/帧序号**的损坏错误，绝不当作正常结束静默略过；
* 帧头 `db_size_after` 非零才结束一笔事务；同事务多次写同一页只取最后版本，前面
  已提交事务改过的页保留在工作映像；
* 末尾只有未提交帧时不产生业务变化；末尾不足一帧时报告截断位置与未提交尾部，**不
  补零、不认作提交**。

### 三类命令结果

| 结果 | 含义 | 退出码 |
|---|---|---|
| `ok` | 完整解析，所有帧都属于已完成提交（空 WAL、无业务变化的提交也算成功） | 0 |
| `incomplete` | 已提交事务全部解码，但尾部有未提交完整帧或半帧 | 10 |
| `corrupt` / `unsupported` | WAL 违反格式/校验失败，或命中不支持的结构 | 3 |
| 调用/输入校验错误 | 清单不符、身份列非法等 | 2 |

`incomplete` 输出目录会带 `INCOMPLETE.txt` 标记，`status.json` 也标明
`incomplete`，退出码不是成功；`corrupt`/`unsupported`/校验失败时不留下可能被误认
为完整结果的文件（先写 `.partial`，失败即删除）。

### 工作映像规则

允许在工具临时目录内，把**已确认提交**的页覆盖到工作映像，再用 SQLite 只读查询
该映像（不要求自写 B-tree 解码器）。每次提交严格按帧声明的提交后页数做增长或截
短（VACUUM 缩小页数时截断文件），且只有该提交的全部页生效后才读表行。禁止为取
最终状态而让 SQLite 一次性打开整个 WAL 再猜测历史。内存中每张表只保留相邻两个
快照（状态 N 替换 N-1），不堆积每个提交的全表行。

---

## 3. 输出含义

`commits.jsonl` 每行一个提交，字段：

* `commit_seq`：提交序号（从 1 开始）；
* `commit_frame_no`：终止提交的帧号（0 基）；
* `frame_range`：该事务涉及的帧范围 `[首帧, 末帧]`；
* `db_page_count_after`：提交后数据库页数；
* `state_before` / `state_after`：行变化的 before/after 分别取自哪个提交状态
  （基线为状态 0）；
* `changes`：每个追踪表的净变化事件列表，顺序稳定（delete → insert → update，组
  内按身份列的有序元组排序，复合身份**按元组逐分量比较，不做字符串拼接**）。

事件语义（都是提交前后的**净差异**）：

* 身份在前一提交不存在、本次存在 → `insert`；
* 身份本次消失 → `delete`；
* 身份相同而业务列不同 → `update`，携带完整 `before`/`after`；
* 身份列本身变化 → 旧身份 `delete` + 新身份 `insert`；
* 同事务插入后又删除、改值后又改回 → **不产生虚构事件**；
* 事务只改未追踪表 → 仍输出提交边界，变化列表为空。

### SQLite 存储类型的机器可读表示

五类存储类型用显式标签区分，NULL、空文本、零长度 BLOB 不会混淆；64 位整数用十
进制字符串承载，避免 JSON number 丢精度；BLOB 用标准 base64 可逆编码；以文本存
储的高精度数仍是 text，保留原始字符串：

```json
{"type": "null",    "value": null}
{"type": "integer", "value": "9223372036854775807"}
{"type": "real",    "value": 3.14}
{"type": "text",    "value": ""}
{"type": "blob",    "value": "Zm9v", "encoding": "base64"}
```

### 重要限制：净变化不能反推逐语句历史

本工具只观察**提交前后的行集差异**，不是逐 SQL 语句审计。一个事务内部一条语句
还是一百条语句、语句的先后顺序、是否曾改后又改回，都无法从净变化推断，工具也不
作此类声称。

---

## 4. Python 迭代接口与命令行

```python
from wal_decode import WalChangeDecoder, TrackedTable

tables = (TrackedTable("facts", ("id",)),
          TrackedTable("aux", ("tenant", "code")))
with WalChangeDecoder("baseline.sqlite", "cycle.sqlite-wal", tables) as dec:
    for commit in dec.iter_commits():       # 逐事务惰性产出
        for change in commit.changes:
            for event in change.events:     # RowEvent: kind/before/after
                ...
    print(dec.outcome, dec.tail)             # ok / incomplete + 尾部诊断
```

命令行：

```bash
# 用清单
python3 -m wal_decode.cli decode --manifest manifest.json --out out/
# 或直接给文件与追踪定义
python3 -m wal_decode.cli decode --db baseline.sqlite --wal cycle.sqlite-wal \
    --table facts=id --table aux=tenant,code --stdout
```

可消费变更流的示例（从基线追踪行集开始，逐事务应用并核对每个中间提交状态）：

```bash
python3 -I demo/wal_decode_drill/example_consume_stream.py \
    baseline.sqlite cycle.sqlite-wal facts id
```

---

## 5. 验收演练（`demo/wal_decode_drill/`）

`run_wal_drill.py` 从**现有 XBRL 接入器**实际建仓，保留 TRUNCATE 后的起点，在同一
WAL 周期内：接入两份产生同值重复、冲突与 nil 的来源，再用受控维护事务修改事实文
本、删除一条事实、变更一个追踪身份；并用辅助表制造同事务同页多次写、数据库扩
张、删除与 VACUUM 缩页，覆盖复合身份、非 UTF-8 BLOB、空值与 64 位整数。每个
COMMIT 后通过**普通只读 SQL 独立保存完整追踪行集**，另写的行事件应用器
（`applier.py`，不导入生产解码器）重放解码流，逐提交与 SQL 快照吻合。

另有独立实验覆盖：未提交完整帧与截断尾帧、复制件中破坏头/盐值/页号/帧校验和（可
重算外层摘要以确保测到的是内部 WAL 校验）、4096 与 65536 两种页长、另一种合法
校验 magic、途中结构改变拒绝、未知/重复/空身份拒绝、纯未追踪表提交，以及一组长
WAL 的逐事务消费。真实 WAL 全部由 SQLite 生成，不用自定义事件文件冒充。
