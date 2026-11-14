# 计算关系核查（只读 XBRL calculation linkbase 检查器）

在**已经接入**的 SQLite 事实库之上，增加一个独立的计算关系核查入口。调用者给出：

1. 一个由 `xbrl_ingest` 接入产生的事实库（**只读打开**，绝不写回）；
2. 一个**本地**计算链接库 XML（`link:calculationLink` / `link:loc` /
   `link:calculationArc`）；
3. 一份**显式** locator 映射表（`#片段ID → 展开概念名`），因此不需要、也不会
   联网下载任何 taxonomy；
4. 一组**显式选中**的来源 ID（可多选）。

输出每条关系在每个业务坐标上的 `consistent` / `inconsistent` /
`indeterminate` 结论、完整坐标、参与边、候选事实的来源追溯，以及可人工核对的
闭区间上下界。核查只读取已保存事实：不重新接入实例、不补造缺失值、不改任何
事实，也不把结果或临时表写回事实库。原有的接入、查询、导出入口完全不变。

---

## 1. 安装与运行

仅依赖 Python 3.11 标准库（`sqlite3`、`xml.parsers.expat`、`decimal`、
`json`），不使用 lxml，不联网。

```bash
cd batch_export

# 单元测试（原 75 个测试 + 新增 54 个 calc_check 测试 = 129）
python3 -m unittest discover -v

# 端到端演示：先打印人工期望，再跑命令并逐项核对（96 项手写断言）
python3 -I demo/run_calc_demo.py
python3 -I demo/run_calc_demo.py --verbose
```

Python API：

```python
from calc_check import CalculationChecker

checker = CalculationChecker(
    "demo/calc_fixtures/calculations.xml",   # 本地 calculation linkbase
    "demo/calc_fixtures/locator_mapping.json",  # 显式 #片段 -> 概念 映射
    "demo/calc_fixtures/concepts.json",      # 与接入时一致的本地概念字典
)
report = checker.check_sources("warehouse.sqlite", ["src_aaa", "src_bbb"])
print(report["counts"])     # {'consistent': n, 'inconsistent': n, 'indeterminate': n}
for result in report["checks"]:
    print(result["verdict"], result["total"], result["coordinate"])
```

构造 `CalculationChecker` 时就会解析并校验链接库与映射；任何定义错误在打开事实
库**之前**抛出 `LinkbaseError`。

---

## 2. 接受的 linkbase XML 结构

命名空间固定为：

* `link` → `http://www.xbrl.org/2003/linkbase`
* `xlink` → `http://www.w3.org/1999/xlink`

只接受下面三种元素，其它结构（`footnoteLink`、未知元素、元素内文本、非良构
XML）一律**定位拒绝**，不会“跳过继续”：

```xml
<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"
               xmlns:xlink="http://www.w3.org/1999/xlink">

  <link:calculationLink xlink:type="extended"
                        xlink:role="http://example.com/role/some-role">

    <link:loc xlink:type="locator"
              xlink:href="#fragment_id"
              xlink:label="a-local-label"/>

    <link:calculationArc xlink:type="arc"
                         xlink:from="a-local-label"
                         xlink:to="another-local-label"
                         xlink:arcrole="http://www.xbrl.org/2003/arcrole/summation-item"
                         weight="1"
                         priority="0"
                         use="optional"/>
  </link:calculationLink>
</link:linkbase>
```

属性按语义逐项检查：

| 元素 | 属性 | 规则 |
|---|---|---|
| `calculationLink` | `xlink:type` | 必须为 `extended` |
| | `xlink:role` | 非空；同 role 的多个 link 组成一个网络 |
| `loc` | `xlink:type` | 必须为 `locator` |
| | `xlink:href` | **只允许**本文件形式 `#片段ID`；远程 URL、含 `/` 或 `?` 一律拒绝 |
| | `xlink:label` | 非空；**在同一 extended link 内**不得重复 |
| `calculationArc` | `xlink:type` | 必须为 `arc` |
| | `xlink:from` / `xlink:to` | 必须能在**本 link 内**解析到某个 `loc` 的 label |
| | `xlink:arcrole` | 只允许标准 summation-item URI `http://www.xbrl.org/2003/arcrole/summation-item` |
| | `weight` | 精确、有限、非零十进制，允许负数；禁止科学计数法、`0`、NaN/INF |
| | `priority` | 非负整数，缺省为 `0` |
| | `use` | `optional`（缺省）或 `prohibited` |

### Label 作用域

label 在**每个 extended link 内独立解析**。两个不同 `calculationLink` 里出现
相同 label 文本不会互相串用；弧的 `from`/`to` 找不到本 link 的同名 `loc` 即
“悬空端点”，定位拒绝。

### 同一 role 合并为一个网络

* 同一 `xlink:role` 的多个 `calculationLink` 合并成一个网络；不同 role 互不
  混算。
* 按 `(总项概念, 分项概念)` 归并候选关系，只看**最高 priority 层**：
  * 最高层存在 `prohibited` → 整条关系取消（不产生有效边，也不参与环判断）；
  * 否则最高层的 `optional` 边必须**权重一致**，相同 weight 的重复边合并成
    **一条**有效边（分项绝不被计算两次）；最高层 weight 不一致 → 拒绝整个
    链接库；
  * 低 priority 的边只保留追溯（`overridden`），其权重绝不参与相加。
* 每条原始弧都保留位置与 `adopted` / `merged` / `overridden` / `prohibited`
  的处置原因（报告 `arc_traces`）。
* 有效图若含自环或长环，给出概念路径并拒绝；被禁止的边不参与环判断，因此
  “本来成环、被 prohibited 切断”的图可以接受。

---

## 3. locator 映射表格式

本地 JSON，三种等价写法：

```json
{
  "mappings": [
    {"fragment": "#rev", "concept": "{http://example.com/calc}Revenue"},
    {"fragment": "#cost", "concept": "{http://example.com/calc}Cost"}
  ]
}
```

```json
[
  {"fragment": "#rev", "concept": "{http://example.com/calc}Revenue"}
]
```

```json
{"#rev": "{http://example.com/calc}Revenue"}
```

规则：

* key 必须是 `#片段ID`（带 `#`），value 必须是展开 QName
  `{命名空间URI}本地名`；
* 片段重复、概念名不是展开形式、空映射 → 拒绝；
* `loc` 引用的片段在映射中缺失、映射到字典里不存在的概念、或映射到
  **非 decimal** 概念 → 定位拒绝（即使该 loc 没有弧引用，也照样校验）。

---

## 4. 核查口径（明确的封闭区间，不实现完整 XBRL 一致性规范）

值 `v` 的 `decimals = d` 表示闭区间

```
[v - 0.5 × 10^(-d), v + 0.5 × 10^(-d)]
```

`decimals = INF` 表示点区间 `[v, v]`。

* 同坐标、**值相等**的多个候选不是多份分项：它们的区间取**交集**，候选事实
  全部保留用于来源追溯；
* 每个分项区间乘以该边 weight（负权重交换上下界），再把全部**直接分项**的
  区间相加（Minkowski 和）；
* 加权合计区间与总项区间**有交集即 `consistent`**，仅端点接触也算相容；
  完全不相交才是 `inconsistent`；
* 全程 `decimal.Decimal` 精确运算，并按参与运算的数字动态放大上下文精度，
  不经过 `float`，也不会被 Python 默认 28 位精度截断（40 位整数、
  `0.000000000001` 这类小数逐位保留）。

### 三种结论

* `consistent`：总项与每个有效分项都有确定、非 nil、无冲突的事实，且区间
  相交；
* `inconsistent`：两边都确定，区间不相交；
* `indeterminate`：缺总项、缺任一有效分项、出现 nil、或同坐标存在相互冲突的
  候选。缺值**不当 0**，冲突时**不任选**一个候选；显式申报的 `0` 正常参与
  计算。

### 业务坐标

坐标使用已保存的完整上下文与单位：实体 scheme/identifier、期间类型与日期、
**全部维度**、单位的分子/分母方向与重数都必须一致。不同 context ID 但业务
相同的事实可以配对；同实体同日期但维度或单位不同的事实不会串项。

### 只用直接分项、不补造中间合计

上层总项只使用其**直接分项的实际申报值**。即使下层关系能算出一个缺失的
中间合计，也不会把它当成已申报事实喂给上层检查；此时该上层坐标为
`indeterminate`（演示中的 `LayerTop`）。

### 零条适用事实

某网络的总项及其直接分项在所选来源中完全没有事实时，该总项进入报告的
`unchecked` 列表并写明原因，不会因为“零错误”被算作通过。

> 报告中的结论**仅针对上述 decimals 闭区间口径**，不构成对财务真实性的
> 证明。每份报告都带 `precision_notice` 说明这一点。

---

## 5. 终端命令与退出状态

```bash
# 只解析/解析网络，不碰事实库：查看有效边与每条弧的处置
python3 -m calc_check.cli networks CALCULATIONS.xml \
    --mapping locator_mapping.json --dict concepts.json

# 核查（可重复 --source 选择多个来源；--report 写机器可读 JSON）
python3 -m calc_check.cli check warehouse.sqlite CALCULATIONS.xml \
    --mapping locator_mapping.json --dict concepts.json \
    --source src_aaa --source src_bbb --report report.json
```

退出状态（为自动化区分三类失败而设计）：

| 退出码 | 含义 |
|---|---|
| `0` | 运行成功且没有 `inconsistent`（可能含 `indeterminate`/`unchecked`） |
| `2` | **调用错误**：未知来源 ID、事实库/字典不可读等（`InvocationError`） |
| `3` | **关系定义错误**：linkbase 或映射不合法，核查根本未运行（`LinkbaseError`） |
| `4` | 定义与调用都有效，但至少一个坐标 `inconsistent`（业务不相容） |
| `5` | 仅当显式加 `--fail-on-indeterminate` 且存在 `indeterminate` 时 |

因此脚本可以明确区分“定义失败（3）/ 无法判断（0，报告里为 indeterminate）/
确实不相容（4）”。

---

## 6. 报告结构（机器可读 JSON 摘要）

```json
{
  "warehouse": "...",
  "selected_sources": ["src_aaa", "src_bbb"],
  "networks": [{"role": "...", "totals": ["..."], "edge_count": 3}],
  "counts": {"consistent": 11, "inconsistent": 4, "indeterminate": 8},
  "checks": [{
    "verdict": "consistent",
    "role": "...",
    "total": "{ns}Total",
    "coordinate": {"entity": {...}, "period": {...}, "dimensions": [...], "unit": {...}},
    "edges": [{"role": "...", "total": "...", "item": "...", "weight": "1",
               "declared_at": ["file:line:column"]}],
    "total_candidates": [{"source_id": "...", "position": 3, "value": "100",
                          "decimals": "0", "nil": false, "line": 20, "column": 5}],
    "item_candidates": {"{ns}A": [{"source_id": "...", "position": 4, "...": "..."}]},
    "total_precision": [{"decimals": "0", "interval": {"lower": "99.5", "upper": "100.5"},
                         "candidate": {"source_id": "...", "position": 3}}],
    "total_interval": {"lower": "99.5", "upper": "100.5"},
    "weighted_items": [{"edge": {...}, "weight": "1", "candidate_count": 2,
                        "candidate_precision": [...],
                        "source_interval": {...}, "weighted_interval": {...}}],
    "sum_interval": {"lower": "100.40", "upper": "100.60"},
    "reasons": [],
    "explanation": "..."
  }],
  "unchecked": [{"role": "...", "total": "...", "reason": "no fact ...", "edges": [...]}],
  "precision_notice": "verdicts apply only to the explicit closed-interval ..."
}
```

每个候选都能追到已有的 `source_id` 与该来源内的事实序位 `position`（以及
行列号）。选中来源后，即使原始实例文件已删除，核查仍能完成——只读取库中
保存的值与坐标。

---

## 7. 代码结构

```
calc_check/
  errors.py     # 错误类型、退出码、命名空间常量
  intervals.py  # 精确 Decimal 闭区间：事实区间/交集/负权重缩放/区间和
  linkbase.py   # linkbase XML 流式解析 + locator 映射加载与校验
  network.py    # role 归并、priority/prohibition、消重、环检测、弧追溯
  store.py      # mode=ro 只读事实库（不执行 DDL，不产生 wal/journal）
  engine.py     # 坐标并集、候选判定、区间核查、三种结论与解释
  service.py    # CalculationChecker 公共门面
  cli.py        # networks / check 终端命令
demo/calc_fixtures/   # 手写 linkbase（含 7 个非法样本）、映射、3 个实例
demo/expected/calc_expectations.py  # 纯手写期望，不调用生产解析/区间函数
demo/run_calc_demo.py               # 端到端演示与逐项核对
tests/
  test_calc_intervals.py  # 区间数学与超默认精度
  test_calc_linkbase.py   # XML/映射/网络解析与各类定位拒绝
  test_calc_engine.py     # 端到端核查、只读保证、离线、CLI 退出码
```

---

## 8. 与接入/导出模块的关系

`calc_check` 是独立模块：它通过 `mode=ro` 打开 `xbrl_ingest` 的 SQLite 库，
只 `SELECT`，不导入也不修改接入写入逻辑，更不接触 `export_delivery` 的发送/
回执协议。核查前后事实库各表行数保持不变，也不会产生 `-wal`/`-shm`/
`-journal` 侧车文件。
