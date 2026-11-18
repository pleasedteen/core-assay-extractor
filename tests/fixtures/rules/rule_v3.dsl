# 钻孔档案抽取规则 v3 —— 同快照输出字段改名（含身份字段）
# 页面布局与 v2 相同，但业务输出字段名按新档案规范调整。
# renamed_from 声明改名前后是同一业务字段：新版 JSONL 写新名称，
# 对旧基线做语义差异时按同一字段对齐，而不是删除旧字段再加回新字段；
# 身份字段改名后，化验记录仍归到原样品。
name: drill-archive
rule_version: "3"
page:
  borehole:
    selector: h2.hole-heading
    labels:
      - Hole ID
  tables:
    interval_table:
      selector: table.interval-grid
      header_layers: 1
      identity:
        - interval_code
      fields:
        interval_code:
          labels: [Interval]
          type: text
          required: true
          renamed_from: [interval_id]
        depth_start:
          labels: [From]
          type: depth
          unit: m
          renamed_from: [depth_from]
        depth_end:
          labels: [To]
          type: depth
          unit: m
          renamed_from: [depth_to]
        lithology_name:
          labels: [Rock type]
          type: text
          renamed_from: [rock_type]
    sample_table:
      selector: table.sample-grid
      header_layers: 1
      identity:
        - sample_code
      fields:
        sample_code:
          labels: [Sample]
          type: text
          required: true
          renamed_from: [sample_id]
        parent_interval:
          labels: [Interval]
          type: text
          renamed_from: [interval_ref]
        sampling_depth:
          labels: [Depth]
          type: depth
          unit: cm
          renamed_from: [depth]
        lithology_detail:
          labels: [Lithology]
          type: text
          renamed_from: [lithology]
    assay_table:
      selector: table.assay-grid
      header_layers: 2
      identity:
        - sample_code
      fields:
        sample_code:
          labels: [Sample]
          type: text
          required: true
          renamed_from: [sample_id]
measurement:
  table: assay_table
  long_format:
    analyte_column: [元素, Element]
    method_column: [方法, Method]
    value_column: [结果, Result]
analytes:
  - code: Au
    labels: [Au, Gold]
  - code: Cu
    labels: [Cu, Copper]
  - code: Zn
    labels: [Zn, Zinc]
  - code: Pb
    labels: [Pb, Lead]
  - code: Ag
    labels: [Ag, Silver]
  - code: Mo
    labels: [Mo, Molybdenum]
methods:
  - code: FA
    labels: [火试金 FA, Fire Assay FA]
  - code: ICP-AES
    labels: [ICP-AES]
  - code: ICP-MS
    labels: [ICP-MS]
  - code: GFAA
    labels: [石墨炉 GFAA, Graphite Furnace GFAA]
