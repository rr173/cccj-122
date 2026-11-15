# 离线 XBRL Taxonomy Package 1.0 登记目录（`taxonomy_package/`）

把一个符合 [XBRL Taxonomy Package 1.0](https://www.xbrl.org/standard/historical/garbage/)
布局的 ZIP 包登记为**可复用的离线目录**：以原始 ZIP 的 SHA-256 作为包身份，
解压并逐成员校验，读取 `META-INF/taxonomyPackage.xml` 与旁边的
`META-INF/catalog.xml`，再从指定入口 schema 出发，解析它**实际依赖**的文件闭包。

本模块**不读取企业实例、不写事实仓、不执行计算关系核查、不访问网络**；它与
`xbrl_ingest`、`export_delivery`、`calc_check`、`warehouse_backup`、`wal_decode`、
`provenance_query` 完全独立，这些既有入口保持不变。

## 目录结构

```
taxonomy_package/
  errors.py       # 错误层级与退出码（0/2/3/4/5）
  models.py       # 命名空间常量与不可变值类型
  xmlutil.py      # 加固 XML 解析（拒绝 DTD/外部实体）、xml:base 折叠
  uriutil.py      # 逻辑包 URI、最长前缀 rewrite、百分号转义与越界检查
  zippkg.py       # 严格 ZIP 读取（绝对/盘符/.. /重复/符号链接全部拒绝）
  descriptor.py   # taxonomyPackage.xml 解析（按命名空间 URI 认元素）
  catalogdoc.py   # OASIS catalog.xml 解析（只支持 rewriteURI）
  resolver.py     # 迭代 DFS 依赖闭包（三色标记，允许环、不靠递归）
  catalogdb.py    # 每包一个 SQLite 目录（成员、规则、入口、解析批次、边）
  store.py        # 目录布局、flock、内容寻址 blob、原子发布
  service.py      # 领域服务：登记/查看/解析/查询/核验
  cli.py          # 命令行：init/register/list/show/resolve/verify/query
demo/taxonomy_package_demo/
  fixture_builder.py     # 小型演示包（重叠前缀、环、xml:base、linkbase…）
  independent_check.py   # 只用标准库独立推导期望 URI 与依赖边
  run_taxonomy_demo.py   # 41 项端到端验收（python3 -I 运行）
  run_taxonomy_cli_demo.sh
```

## 支持的格式与安全边界

### ZIP 成员（`zippkg.py`）

所有成员必须位于**一个共同顶层目录**之下，该目录内含
`META-INF/taxonomyPackage.xml`（本工具还要求 `META-INF/catalog.xml`）。
逐成员拒绝：

* 绝对路径（`/...`）与 Windows 盘符路径（任意路径段中的 `C:` 形式）；
* 反斜杠路径、NUL 字节、空段、`.`/`..` 段；
* 规范化后重复的成员路径；
* Unix 符号链接成员（按 ZIP external attribute 的 `S_IFLNK` 判定，目录项也查）；
* 解压失败/CRC 错误的成员。

任何成员都不可能被写到目标目录之外；登记过程先在 `packages/<digest>.staging-*`
暂存目录内完成，最后一次 `os.replace` 原子改名发布，失败即清理暂存目录，
已存在的包目录拒绝覆盖。

### 包描述符（`descriptor.py`）

只按**命名空间 URI + 本地名**匹配 `http://xbrl.org/2016/taxonomy-package`
内的元素，文档把前缀写成 `tp:` 还是 `zzz:` 都一样。读取四项标准信息：
`identifier`、`name`、`version`、`entryPointDocument`。

* 入口 `href` 按 Taxonomy Package 1.0 相对**包根**（含 `META-INF` 的目录）解析，
  并折叠该元素上生效的 `xml:base` 链；
* 解析结果必须落在包内逻辑空间，百分号解码后的 `%2e%2e` 越界同样拒绝；
* 外命名空间元素、缺失/重复的必备元素一律报错。

### OASIS 目录（`catalogdoc.py`）

只接受 OASIS XML Catalog 命名空间
（`urn:oasis:names:tc:entity:xmlns:xml:catalog`）下的 `catalog`、`group`、
`rewriteURI`。**任何其它规则（`uri`、`public`、`system`、`rewriteSystem`、
`nextCatalog`…）或外命名空间元素都是明确错误，绝不静默忽略。**

* `rewritePrefix` 按 catalog 所在位置（`META-INF/`）解析为包内逻辑 URI，
  相对前缀与生效 `xml:base` 都会折叠；
* 同一 `uriStartString` 出现两次即拒绝（目标不同报 `CatalogConflictError`，
  完全重复也拒绝，要求目录意图显式）；
* `uriStartString` 必须是绝对公开 URI，rewrite 目标必须保持在包内。

### XML 加固（`xmlutil.py`）

lxml 解析器固定 `resolve_entities=False, load_dtd=False, no_network=True`；
此外在解析前对**原始字节**扫描 `<!DOCTYPE` 与 `<!ENTITY`，因此任何带 DTD、
内部子集或外部实体的 XML（schema、linkbase、描述符、catalog 都算）在登记时
即被拒绝，不依赖某个 lxml 版本对 DTD 的宽容程度。

## URI 解析与依赖闭包（`resolver.py`）

1. 扫描 XSD 的 `import`/`include`/`redefine`（XSD 命名空间）与 XBRL
   `link:linkbaseRef`（`xlink:href`）。`import` **必须带 `schemaLocation`**，
   否则明确报告超出支持范围。
2. 相对引用先按该文档的逻辑 URI 与元素上生效的 `xml:base` 求**绝对 URI**，
   绝不先把它当本地文件名。
3. 公开 URI 再匹配 catalog 的 `rewriteURI`：取**最长 `uriStartString` 前缀**；
   同长前缀给出不同目标即报冲突。无匹配规则的公开 URI **不联网补齐**，直接拒绝。
4. rewrite 后的目标经 URI 百分号解码与路径规范化，必须仍在已登记包内；
   `%2e%2e%2f` 一类转义无法逃出包根。
5. 引用不存在、目标不是 XML、XML 非良构等错误都带**起始文件与行号**
   （lxml 提供 `sourceline`），并拒绝本次目录生成，不写入任何批次。

遍历使用显式栈的**迭代 DFS + 三色标记（白/灰/黑）**：

* 允许依赖环：回到灰色节点的边记为 `is_cycle` 环路边，不会无限递归，
  也不会因访问顺序漏掉文件；
* 每个逻辑 URI 在清单中只记录一次，每条去重边记录一次；
* 两个入口引用同一 URI 时共享同一个解析结果；不同 URI 即使字节内容相同，
  也保留各自的逻辑来源（成员 blob 仍按内容去重）。

## 登记目录的磁盘格式

```
<registry>/
  FORMAT                          # 固定魔数文本
  lock                            # flock 协调文件
  packages/<zip-sha256>/
    meta.json                     # 包元数据侧车（人读）
    catalog.sqlite                # 不可变 SQLite 目录
    files/<sha前2位>/<sha>.member # 内容寻址的成员 blob
```

SQLite 目录记录：包摘要与元数据、入口、catalog rewrite 规则、逐成员摘要/大小/
XML 标记、以及每次解析批次（`batches`）的最终解析表
（`resolution_files`：公开 URI → 包内路径）与依赖边（`resolution_edges`：
源 URI、目标 URI、引用种类、**原始 href**、是否环边）。公开 URI、原引用与
最终目标都可通过 `query` 检索。

* **原子发布**：登记在暂存目录内完成后整体改名；新解析批次先复制
  `catalog.sqlite` 到暂存文件、在一个事务内写入，再 `os.replace` 覆盖。
  发布中的崩溃不会留下可被当成有效目录的半成品，目录里也不会出现
  `-wal`/`-shm`/`-journal` 侧车文件。
* **内容寻址**：相同字节的成员只存一个 blob；相同 ZIP 摘要再次接入直接返回
  已有包身份，不再复制任何文件。
* **版本冲突**：内容不同但 `identifier`+`version` 与已登记包相同，按
  `VersionConflictError` 拒绝，绝不覆盖旧包；同 identifier 不同 version 可共存。

## 离线核验（只依赖登记后的本地文件）

`verify` 不访问网络、不需要原始 ZIP：

* 校验目录固定布局、拒绝符号链接替换；
* 逐 blob 重算 SHA-256 与字节数，与 SQLite 目录比对——文件缺失、字节被改都报
  **具体成员路径**；
* 检查 blob 与成员表互相引用一致（无孤儿 blob、无悬空引用），`meta.json`
  与 SQLite 元数据一致；
* 逐批次重放解析关系：每条边的两端必须仍在解析集合内，文件摘要必须与成员表
  一致，计数必须与表内容一致。

### 随仓库离线搬迁

整个 `<registry>` 目录是自包含的：成员字节在 `files/` 下，URI 关系在
`catalog.sqlite` 内，没有任何绝对路径或对原始 ZIP 的引用。把该目录
`cp -a`/`rsync` 到另一台（或无网络、无原 ZIP 的）机器后，直接
`verify` 与 `resolve` 即可，行为与原机一致。演示脚本第 6 阶段会删掉原始 ZIP、
复制目录到“离线位置”后重新核验与解析，再改写一份依赖文件，确认离线校验能
定位到具体变化成员。

## 命令行

```bash
python3 -m taxonomy_package.cli init     REGISTRY
python3 -m taxonomy_package.cli register REGISTRY PKG.zip [PKG.zip ...]
python3 -m taxonomy_package.cli list     REGISTRY [--json]
python3 -m taxonomy_package.cli show     REGISTRY --digest D [--json]
python3 -m taxonomy_package.cli resolve  REGISTRY --digest D [--entry URI ...]
                                              [--batch N] [--json]
python3 -m taxonomy_package.cli verify   REGISTRY [--json]
python3 -m taxonomy_package.cli query    REGISTRY --digest D --uri URI [--json]
```

退出码：`0` 成功；`2` 调用错误（未知包/入口、拒绝覆盖）；`3` 登记目录损坏
（缺文件、字节变化、解析关系不一致）；`4` 包格式或解析错误（危险 ZIP 成员、
坏描述符/catalog、DTD/外部实体、不支持的引用、缺失依赖、rewrite 冲突、
URI 多义）；`5` 原子发布/写盘失败。

## Python API

```python
from taxonomy_package import TaxonomyRegistry

registry = TaxonomyRegistry("registry/", create=True)
result = registry.register_zip("demo.zip")          # 幂等，按 ZIP SHA-256
batch = registry.resolve_entries(result.package_sha256)           # 全部入口
batch = registry.resolve_entries(result.package_sha256,
                                 ("xbrl-pkg://package/schemas/ep1.xsd",))
for f in batch.files:    # 公开/逻辑 URI -> 包内成员 + 成员摘要
    print(f.public_uri, f.member_path, f.member_sha256)
for e in batch.edges:    # 依赖边（含原始 href 与环标记）
    print(e.kind, e.source_uri, "->", e.target_uri, e.is_cycle)
report = registry.verify()                          # 离线核验
answer = registry.query_uri(result.package_sha256,
                            "xbrl-pkg://package/schemas/ep1.xsd")
```

## 依赖

仅依赖 Python 3 标准库与 **lxml**（`from lxml import etree`）。本环境中 lxml
安装在用户 site-packages（`python3 -m pip install --user lxml`）；以
`python3 -I` 运行演示脚本时，脚本会显式把受信任的用户 site 目录加回
`sys.path`，但数据/临时目录永远不会进入模块搜索路径。

## 验收与测试

```bash
python3 -m unittest tests.test_taxonomy_package_parsing tests.test_taxonomy_package_service \
                     tests.test_taxonomy_package_cli -v
python3 -I demo/taxonomy_package_demo/run_taxonomy_demo.py        # 41 项端到端
bash demo/taxonomy_package_demo/run_taxonomy_cli_demo.sh          # 终端退出码走查
```

演示包刻意覆盖：重叠 rewrite 前缀的最长匹配、schema `import` 到的类型文件、
带 `xml:base` 的相对 `include`、两个入口共享依赖、`linkbaseRef`、依赖环、
最终解析清单，以及大量与依赖无关的 ZIP 成员（确认只解析闭包中的 XML）。
期望 URI 与依赖边由 `independent_check.py` **独立**用标准库 `zipfile` +
`xml.etree.ElementTree` 重新推导，不调用正式解析器。负向夹具（路径越界、
同前缀不同目标、缺失依赖、同 identifier/version 内容冲突、外部实体、成员摘要
损坏）逐项确认非零退出且既有登记目录保持原样。

## 范围与限制
* 只支持 OASIS `rewriteURI` 一类目录规则；其它规则明确拒绝而非忽略。
* `xs:import` 必须显式给出 `schemaLocation`；无位置的纯命名空间导入不在
  支持范围内，会带行号报错。
* 逻辑 URI 使用私有 `xbrl-pkg` 方案，仅用于包内寻址，永远不会被当作可下载
  的公开地址。
* 本模块不校验 XSD 的 schema 合法性、不解析 XBRL 概念语义、不执行计算关系；
  这些分别属于既有的接入与核查入口。
