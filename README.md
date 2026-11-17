# corebench · 钻孔档案网页抽取工作台

把按钻孔分页、区间嵌套样品、样品嵌套化验表的档案页面，抽取为本地 **JSON Lines**，
并提供声明式规则、版本管理、采集回放、语义差异与基线确认的完整工作台。

* **纯 Python 标准库**实现，无第三方依赖、无浏览器、无独立数据库服务；
* 验证通过 **HTTP（内置模拟站点）** 与 **本地文件** 两种来源完成，不访问真实外站；
* 容错解析缺失闭合标签、`rowspan`/`colspan` 合并单元格与多层表头；
* 深度统一为米、浓度统一为 mg/kg，保留检出限（`<0.05`）、实测零（`0`）与缺失（空/`N/A`）三种不同含义；
* 规则为声明式文本，页面结构变化只改规则文本，不需要写 Python。

## 快速开始（一条命令，约 2 秒）

```bash
python3 -I demo.py
```

端到端演示：启动模拟站点 → 模拟采集中途失败并从检查点续跑 → 跨页互补合并与冲突拦截
→ 干净钻孔确认基线 → 同一快照回放得到空差异 → 注入值变化/无法解释字段并导出机器可读报告。

## 运行自动验证（约 2 秒，远低于 80 秒）

```bash
python3 -I run_tests.py
```

47 个用例覆盖：嵌套归属、脏表格、跨页互补与冲突、单位等价、检出限/零/缺失语义、
分页中途失败与续跑、规则版本固定、基线拦截、不完整快照不得回归通过、Web API 与界面资源。

## 启动 Web 界面

```bash
python3 -I serve.py --data-dir ./workbench-data --port 8077
# 浏览器打开 http://127.0.0.1:8077
```

界面四个页签：

1. **规则编辑与版本**：在线编辑 DSL，保存前服务端做语法校验，每次保存生成不可变版本；
2. **采集与回放**：粘贴页面清单（`page_id` + `url`）启动/续跑，查看 JSONL 结果与原页溯源；
3. **差异查看**：在同一完整快照上比较基线与候选运行，定位到业务记录与字段，可导出 JSON 报告；
4. **基线确认**：仅完整、无冲突的成功运行可确认，冲突/失败/不完整快照都会被阻止。

## 目录结构

```
corebench/
  dsl.py          声明式规则文本格式（缩进式 YAML 子集，纯 stdlib 解析/序列化）
  html_dom.py     容错 HTML 解析（隐式闭合、script/style 原样保留）
  tables.py       表格网格重建（合并单元格展开、多层表头拼接）
  values.py       值/单位语义（检出限、零、缺失；m/cm、mg/kg 与 ug/kg 归一）
  rules.py        规则模型：层级、字段含义、记录身份、表头/分析项/方法别名
  extractor.py    单页抽取（宽表与长表、行级溯源、无法解释列告警）
  sources.py      HTTP/文件来源、内容哈希与快照存储
  merge.py        跨页身份合并（顺序无关、互补测量、矛盾冲突）、JSONL 读写
  runner.py       运行引擎（检查点/续跑、规则版本固定、快照回放）
  storage.py      规则版本/运行/基线/报告的文件存储（原子写入）
  diff.py         语义差异与基线、回归报告
  mock_site.py    模拟钻孔档案站点（脏 HTML、双布局、失败开关）
  webapp.py       stdlib HTTP 服务 + JSON API
  web/            单页界面（原生 HTML/CSS/JS，无构建步骤）
tests/
  fixtures/rules/rule_v1.dsl, rule_v2.dsl     两版声明式规则
  fixtures/expected/expected.jsonl            人工编写的期望结果
  test_*.py                                    47 个自动验证用例
demo.py / serve.py / run_tests.py             演示、启动、测试入口
```

## 声明式规则要点

```yaml
measurement:
  table: assay_table
  long_format:                      # 元素/方法在数据行的长表
    analyte_column: [元素, Element]
    method_column: [方法, Method]
    value_column: [结果, Result]
analytes:
  - code: Au
    labels: [Au, Gold]              # 表头改名只改别名，输出字段保持稳定
methods:
  - code: FA
    labels: [火试金 FA, Fire Assay FA]
```

规则可表达：表格选择器与表头层数、字段业务类型（文本/深度/浓度）与默认单位、
记录身份字段、分析项与方法别名。两版规则（中文布局 A、英文布局 B）输出语义完全一致。

## 关键行为约定

* **跨页合并**：区间/样品按身份字段、测量按 `(样品, 项目, 方法)` 去重；同身份等价测量折叠，
  矛盾值生成 `conflicts` 并阻止基线确认；合并结果与页面顺序无关。
* **差异分类**：`added` / `removed` / `value_changed` / `type_or_meaning_changed` /
  `extraction_failed` / `unexplained_field`。字段改名、列表换序、等价单位表达不计为数据变化；
  规则无法解释的原有字段产生 `unexplained_field`，不会被当作正常删除。
* **安全保障**：运行开始即固定规则文本，运行期间保存新规则不影响在途运行；失败/未完成运行
  不能替换已确认基线；不完整快照的回归结论一律不通过。
* **可追溯**：每条记录与错误都带 `page_id`、`url`、页面 `content_sha1`、表选择器、原始行/单元格文本
  与规则版本，界面可直接查看对应原页 HTML。
