# XBRL 2.1 实例本地接入（SQLite 事实仓）

把企业交来的普通 XBRL 2.1 实例文件解析成**有业务坐标**的可查事实，存入本地
SQLite；不做财务比率、预测或报表页面，也不改动原有的批量导出交付服务
（`export_delivery/`，其协议、发送与回执逻辑完全未动）。

核心原则：XML 里“看起来像数字的文本”不能直接拼成一列。每个事实必须能回答
“哪个概念、哪个主体与期间、哪些维度、什么单位、什么语言、来自哪个文件第几个
事实”，相同坐标下的数值即使写法不同（`1.0` / `1.00`）也归为一致重复，而真正
不同的值必须作为冲突整体返回，绝不按文件顺序挑一个冒充唯一结果。

---

## 1. 安装与运行

只依赖 Python 3.11+ 标准库（`sqlite3` + `xml.parsers.expat`），不需要 lxml，
不联网、不下载 taxonomy。

```bash
cd batch_export

# 全量测试（原有 19 个导出交付测试 + 新增 56 个 XBRL 测试）
python3 -m unittest discover -v

# 可运行演示：小型实例 + 独立手写预期，逐项核对
python3 demo/run_demo.py            # 最后一行应为 all 196 hand-written expectations held
python3 demo/run_demo.py --verbose  # 附带每个来源事实
bash demo/run_cli_demo.sh           # 终端命令端到端走查（接入/查询/搜索/追溯/描述）

# 终端命令
python3 -m xbrl_ingest.cli ingest warehouse.db \
    --dict demo/xbrl_fixtures/concepts.json demo/xbrl_fixtures/demo-A.xml
python3 -m xbrl_ingest.cli query  warehouse.db \
    --dict demo/xbrl_fixtures/concepts.json --concept-local Revenue
python3 -m xbrl_ingest.cli trace  warehouse.db \
    --dict demo/xbrl_fixtures/concepts.json --source <SOURCE_ID> --position 1
```

Python API：

```python
from xbrl_ingest import XbrlIngestor

ing = XbrlIngestor("warehouse.db", "concepts.json")
report = ing.ingest_file("instance.xml")        # 整份文件原子提交
for result in ing.query(concept_local="Revenue",
                        entity_identifier="LEI-001",
                        period_kind="duration",
                        dimensions=[("{http://example.com/co}Region",
                                     "{http://example.com/co}EMEA")]):
    print(result.status, len(result.candidates))  # "single"/"conflict"
fact = ing.trace_fact(report.source_id, position=1)   # 原始事实追溯
ing.close()
```

`ingest_file` 抛出 `IngestError` 子类时代表**整份文件被拒绝**，错误信息含
`文件:行:列`，库中不会留下任何该文件的 context/unit/fact。

---

## 2. 支持范围（明确边界）

支持：

* 普通（非 Inline）XBRL 2.1 实例的 `xbrli:xbrl` 根元素。
* `xbrli:context`：`entity/identifier`（保留 scheme 与原文）、`period` 的
  `instant` 或 `startDate`/`endDate`、`segment`/`scenario` 中的
  `xbrldi:explicitMember` 维度。
* `xbrli:unit`：单个 `measure`、多个 `measure` 的乘积、`divide` 的
  `unitNumerator`/`unitDenominator`。
* 概念字典允许的 decimal / text 事实，`xsi:nil`，`decimals`。
* context、unit、fact 任意先后顺序（事实先引用后声明可以接入）。

不支持，遇到即**拒绝整份文件**（不会是“跳过该元素”）：

* Inline XBRL、元组、footnote link、calculation linkbase 等链接库；
* `typedMember`、非维度 segment/scenario 内容；
* 未在本地概念字典登记的概念；
* 无法展开的 QName（前缀在该元素处未绑定）、重复 context/unit ID、缺失引用；
* 非 `YYYY-MM-DD` 日期、结束早于开始的 duration、非法 `decimals`、非法十进制
  字面量、`xsi:nil="true"` 但仍带值、text 事实带 `unitRef`、decimal 事实缺
  `unitRef`/`decimals`（nil 事实可省略 `decimals`，仍必须有 `unitRef`）。

---

## 3. 业务坐标：正确理解“同一条事实”

### 3.1 Context 身份

```
context = (entity.scheme, entity.identifier,
           period_kind, instant 或 (start, end),
           sorted( (展开后的维度 QName, 展开后的成员 QName) ))
```

* context ID **不参与**身份：同一业务坐标换 ID 仍合并。
* 前缀拼写**不参与**身份：维度名/成员名都在其**所在 XML 元素处生效**的前缀
  绑定下展开成 `{namespaceURI}localName`，不保存前缀字符串当身份。
* 维度书写顺序不影响身份；按维度名排序。
* entity scheme 或期间类型不同 → 不同坐标（同 identifier 不同 scheme 不合并）。
* 维度名重复 → 拒绝整份文件。

### 3.2 Unit 身份

```
unit = (sorted numerator measures with multiplicity,
        sorted denominator measures with multiplicity)
```

* measure 排列顺序、前缀拼写不影响身份；
* **保留每个 measure 的重复次数**（`USD USD` ≠ `USD`）；
* **保留分子/分母方向**：`USD/shares` 与 `shares/USD` 是两个单位；
* unit ID 只是文件内引用名，不进入身份，跨文件同名 unit 也不会混同。

### 3.3 Fact 坐标与结果状态

```
fact = (概念 QName, context 身份, unit 身份或 None, text 事实的 xml:lang)
```

* decimal 事实不参与语言坐标；text 事实保留 `xml:lang`，语言进入坐标；
* text 值**不去首尾空白**（`"  retail segment  "` 与 `"retail segment"` 是不同文本）；
* 同一坐标下：
  * 全部候选相等（decimal 按数值比较，`1.0 == 1.00`）→ `status="single"`，
    但**所有**来源事实仍保留（候选数、decimals、来源序位都可查）；
  * `nil` 与非 `nil`、不同数值、不同文本 → `status="conflict"`，
    返回全部候选，绝不按文件顺序取一个值；
  * 文件名、context ID、unit ID、无关前缀变化都不改变这项判断。

### 3.4 值与精度

* decimal 用 `decimal.Decimal` 精确解析，**不经过 float**，不根据 `decimals`
  擅自舍入；
* 保留原始文本（`value_text`）与精确数值的可比形式（`value_canonical`）；
* `decimals` 原样保存（有符号整数或 `INF`）；
* `xsi:nil="true"` 显式存为 `nil=1`，与数值 `0`、空文本 `""` 都不同；
* 巨大数（40 位整数）和细小十进制（`0.000000000001`）都逐位保留。

---

## 4. 来源、幂等与原子性

| 场景 | 行为 |
|---|---|
| 同一路径重复接入同一内容 | `already_ingested`，不新增事实 |
| 原文件搬家后接入 | 按字节摘要识别为同一 source，不新增事实 |
| 同一路径内容变了 | 作为**新 source** 接入；旧 source 保留且仍可查（`outcome="replaced"`，带 warning） |
| 任一结构/引用问题 | 抛错并给 `文件:行:列`；整份文件一个事务，**不留下半份事实** |

保存内容：

* `sources.content_sha256`（文件字节摘要）与 `byte_size`；
* `sources.ingest_seq`（接入顺序，用于稳定排序，不依赖文件名字典序）；
* `facts.position`（事实在文件中的 1-based 文档序位，含 context/unit 之后出现的事实）；
* `facts.line/column`（事实元素位置）；
* `context_id_in_source` / `unit_id_in_source`（仅作可读追溯，不是身份）。

### 4.1 进程退出点（仅供测试）

`XbrlIngestor.ingest_file(path, system_exit_hook=hook)` 会在**本文件所有行已写入
但事务尚未 COMMIT** 时调用 `hook(source_id)`。测试和演示用 `os._exit(77)` 真实
终止子进程；重新打开数据库只能看到接入前的状态（SQLite 回滚），再输入原文件可
完整完成。这是测试注入点，不在正常业务路径使用。

### 4.2 离线可查

查询与追溯只读 SQLite 中已保存的值、坐标、摘要和位置；源文件被删除或移动后
仍然可用（`demo/run_demo.py` 第 10 步会实际删除文件后重新打开库验证）。

---

## 5. 概念字典

本地 JSON，不联网下载 taxonomy。每个概念以 `{命名空间URI}本地名` 登记，类型只
允许 `decimal` 或 `text`：

```json
{
  "concepts": [
    {"namespace": "http://example.com/co", "name": "Revenue", "type": "decimal"},
    {"namespace": "http://example.com/co", "name": "Note",    "type": "text"}
  ]
}
```

也接受 `{"{uri}local": "decimal"}` 形式。重复登记、非法类型、空字典都会在打开
ingester 时直接报错。

---

## 6. 终端命令

所有命令通过 `--dict` 指定概念字典（接入之外的查询也用同一字典打开，保证类型
一致；字典本身不参与坐标）。

```bash
# 接入（可多个文件；拒绝的打印 REFUSED 文件:行:列，退出码 2）
python3 -m xbrl_ingest.cli ingest DB --dict DICT.json A.xml B.xml

# 按 实体/概念/期间/维度 组合筛选
python3 -m xbrl_ingest.cli query DB --dict DICT.json \
    --scheme http://example.com/lei --identifier LEI-001 \
    --period-kind duration --start 2024-01-01 --end 2024-12-31 \
    --concept '{http://example.com/co}Revenue' \
    --dimension '{http://example.com/co}Region={http://example.com/co}EMEA'

# 文本搜索（返回整个坐标的全部候选）
python3 -m xbrl_ingest.cli search DB --dict DICT.json retail

# 从结果追溯原始事实
python3 -m xbrl_ingest.cli trace DB --dict DICT.json --source SRC --position 3
# 按坐标摘要列出全部候选
python3 -m xbrl_ingest.cli show  DB --dict DICT.json --digest HEX

# 来源列表 / 某来源声明 / 某来源声明的 context、unit
python3 -m xbrl_ingest.cli sources  DB --dict DICT.json
python3 -m xbrl_ingest.cli describe DB --dict DICT.json SRC
python3 -m xbrl_ingest.cli context  DB --dict DICT.json SRC
python3 -m xbrl_ingest.cli unit     DB --dict DICT.json SRC
```

`query`/`search`/`trace`/`show` 支持 `--json`，便于脚本消费。

---

## 7. 代码结构

```
xbrl_ingest/
  errors.py      # 错误类型、位置、支持常量、ConceptSpec
  qname.py       # 前缀展开、QName 文本解析、严格日期/decimals/十进制解析
  models.py      # Entity/Period/ContextCoordinate/UnitCoordinate/FactCoordinate 等
  parser.py      # expat 流式解析：命名空间作用域、前后交错、引用解析
  store.py       # SQLite schema、原子接入、查询、追溯
  dictionary.py  # 本地概念字典加载与校验
  service.py     # XbrlIngestor 公共 API
  cli.py         # 终端命令
demo/
  xbrl_fixtures/ # 手写小实例（A/B/C/D）与 14 个应拒绝文件
  expected/demo_expectations.py  # 手写预期值，不调用规范化函数
  run_demo.py    # 可运行演示 + 逐项核对
tests/
  test_xbrl_ingest.py  # 端到端（含真实子进程 os._exit 回滚）
  test_xbrl_edge.py    # 结构/日期/单位/文本边界
```

---

## 8. 演示覆盖的场景

`python3 demo/run_demo.py` 逐项核对（共 196 项手写断言）：

1. 同一上下文在不同 ID/前缀/维度顺序下相认（demo-A `c-alpha` ↔ demo-B `ctx-shared`）；
2. 局部前缀重新绑定改变 measure 身份（demo-A `u-ratio` 分母的 `m:` 重绑定到
   legacy 命名空间）；
3. 相反方向的除法单位不合并（A `shares/USD` vs B `USD/shares`）；
4. 乘积单位换顺序合并（A `USD * shares` vs B `shares * USD`）；
5. 晚出现的引用目标（A 第一条 Revenue 先引用后声明的 context/unit）；
6. 不同 entity scheme 不合并（demo-C）；
7. instant 与 duration 不同坐标；
8. 巨大数（40 位）与细小十进制（`0.000000000001`）逐位保留；
9. `nil` 与 `0` 区分，nil 与 0 同坐标时是冲突；
10. 一致重复（`1.0`/`1.00`/`1.000`）为 single 但保留 3 个候选及各自 decimals；
11. 真正冲突（Revenue `1234.50` vs `1250.00`）返回冲突状态和全部候选；
12. 文本空白（`"  retail segment  "` vs `"retail segment"`）是冲突；
13. 文本语言差异（demo-D：en/fr/de 为不同坐标；同 en 下 alpha/beta 冲突）；
14. 14 个非法文件逐个验证整文件回滚；
15. 真实 `os._exit(77)` 终止接入子进程，重开库看不到任何该文件事实，重试成功；
16. 同一路径换内容作为另一来源；搬路径/重复接入不添事实；
17. 删除源文件后查询与追溯仍可读。

---

## 9. 与原有导出服务的关系

`xbrl_ingest` 是独立的本地接入模块，不导入、不修改 `export_delivery`。原有
19 个测试继续通过；本模块只把事实落到自己的 SQLite 库，不发送、不回执、不涉
及批量导出协议。需要让导出服务读取这些事实时，由调用方自行把查询结果映射为
导出请求的行，两者不共享状态库。
