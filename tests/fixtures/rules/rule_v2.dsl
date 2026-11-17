# 钻孔档案抽取规则 v2 —— 适配站点改版后的布局 B
# 仅靠声明式别名完成结构迁移：英文表头、新 table class、cm 深度、
# mg kg-1 / μg kg-1 等等价单位拼写。输出字段名与 v1 保持一致，
# 因此字段改名不会在差异中被报成数据变化。
name: drill-archive
rule_version: "2"
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
        - interval_id
      fields:
        interval_id:
          labels: [Interval]
          type: text
          required: true
        depth_from:
          labels: [From]
          type: depth
          unit: m
        depth_to:
          labels: [To]
          type: depth
          unit: m
        rock_type:
          labels: [Rock type]
          type: text
    sample_table:
      selector: table.sample-grid
      header_layers: 1
      identity:
        - sample_id
      fields:
        sample_id:
          labels: [Sample]
          type: text
          required: true
        interval_ref:
          labels: [Interval]
          type: text
        depth:
          labels: [Depth]
          type: depth
          unit: cm
        lithology:
          labels: [Lithology]
          type: text
    assay_table:
      selector: table.assay-grid
      header_layers: 2
      identity:
        - sample_id
      fields:
        sample_id:
          labels: [Sample]
          type: text
          required: true
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
