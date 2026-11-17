# 钻孔档案抽取规则 v1 —— 适配模拟站点布局 A（中文表头）
# 纯声明式文本：调整页面结构只需修改本文件，无需改动 Python。
name: drill-archive
rule_version: "1"
page:
  borehole:
    selector: h1
    labels:
      - 钻孔编号
  tables:
    interval_table:
      selector: table.interval-table
      header_layers: 1
      identity:
        - interval_id
      fields:
        interval_id:
          labels: [区间编号]
          type: text
          required: true
        depth_from:
          labels: [孔深起]
          type: depth
          unit: m
        depth_to:
          labels: [孔深止]
          type: depth
          unit: m
        rock_type:
          labels: [岩性]
          type: text
    sample_table:
      selector: table.sample-table
      header_layers: 1
      identity:
        - sample_id
      fields:
        sample_id:
          labels: [样品编号]
          type: text
          required: true
        interval_ref:
          labels: [所属区间]
          type: text
        depth:
          labels: [采样深度]
          type: depth
          unit: m
        lithology:
          labels: [岩性描述]
          type: text
    assay_table:
      selector: table.assay-table
      header_layers: 2
      identity:
        - sample_id
      fields:
        sample_id:
          labels: [样品编号]
          type: text
          required: true
measurement:
  table: assay_table
  # 长表：分析项/方法出现在数据行，结果列内嵌单位
  long_format:
    analyte_column: [元素]
    method_column: [方法]
    value_column: [结果]
# 元素表头别名：宽表表头路径形如 ["Au", "火试金 FA (mg/kg)"]，
# 长表表头路径形如 ["分析结果", "元素"]，元素名称出现在数据行中。
analytes:
  - code: Au
    labels: [Au]
  - code: Cu
    labels: [Cu]
  - code: Zn
    labels: [Zn]
  - code: Pb
    labels: [Pb]
  - code: Ag
    labels: [Ag]
  - code: Mo
    labels: [Mo]
# 方法别名归一：不同表头写法映射到稳定方法代码
methods:
  - code: FA
    labels: [火试金 FA]
  - code: ICP-AES
    labels: [ICP-AES]
  - code: ICP-MS
    labels: [ICP-MS]
  - code: GFAA
    labels: [石墨炉 GFAA]
