#!/usr/bin/env python3 -I
"""End-to-end demo of the corebench workbench (fully offline).

It starts the in-process mock drill-archive site, then walks the whole story:

1. import two declarative rule versions;
2. run a collection over HTTP, including a simulated mid-crawl failure and
   checkpoint resume;
3. show cross-page complementary measurements and the blocking conflict;
3b. manually adjudicate the conflict (pick a candidate + reason), producing an
    independent reviewed run and a review-backed baseline;
3c. replay the same frozen snapshot: an adjudication carries over only when
    candidate values and sources match; a vanished conflict still fails;
4. confirm a clean baseline, replay on the frozen snapshot and produce an
   empty semantic diff;
5. demonstrate a value-change regression and an unexplained-column failure;
6. export machine-readable JSON reports and JSONL output.

Run:  python3 -I demo.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from corebench import mock_site  # noqa: E402
from corebench.diff import compare_runs, confirm_baseline, regression_report  # noqa: E402
from corebench.merge import read_jsonl  # noqa: E402
from corebench.review import ReviewReuseError, carry_review, create_review  # noqa: E402
from corebench.runner import RunConfig, RunEngine  # noqa: E402
from corebench.rules import Rule  # noqa: E402
from corebench.sources import SnapshotStore  # noqa: E402
from corebench.storage import WorkbenchStore  # noqa: E402

RULE_V1 = os.path.join(ROOT, "tests", "fixtures", "rules", "rule_v1.dsl")
RULE_V2 = os.path.join(ROOT, "tests", "fixtures", "rules", "rule_v2.dsl")


def hr(title: str) -> None:
    print("\n" + "=" * 78)
    print(f"  {title}")
    print("=" * 78)


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="corebench-demo-")
    print(f"工作数据目录: {data_dir}")
    server, base_url = mock_site.start_mock_server()
    print(f"模拟站点已启动: {base_url} （不访问任何真实外站）")

    store = WorkbenchStore(data_dir)
    snapshots = SnapshotStore(os.path.join(data_dir, "snapshots"))
    engine = RunEngine(store, snapshots)
    rule_v1 = open(RULE_V1, encoding="utf-8").read()
    rule_v2 = open(RULE_V2, encoding="utf-8").read()
    Rule.from_text(rule_v1)  # validate before versioning
    v1 = store.save_rule_version("drill-archive", rule_v1, "v1 中文表头布局")
    v2 = store.save_rule_version("drill-archive", rule_v2, "v2 英文表头/等价单位")
    print(f"规则版本已保存: {v1['rule_id']}, {v2['rule_id']}")

    manifest = [
        {"page_id": page["page_id"], "url": f"{base_url}/site/{borehole}/{page['page_id']}"}
        for borehole, pages in mock_site.DATASET.items() for page in pages
    ]

    # --------------------------------------------------------------- 1. crawl
    hr("1. 采集：先模拟第 2 页中途失败，再从检查点续跑")
    engine.start(RunConfig("crawl", v1["rule_id"], v1["text"], manifest))
    mock_site.STATE.fail(["zk01-p2"])
    failed = engine.run("crawl")
    print(f"首次运行状态: {failed['status']}；失败页: "
          f"{[p['page_id'] for p in failed['pages'] if p['state'] == 'failed']}")
    mock_site.STATE.reset()
    run = engine.run("crawl")  # resume from checkpoint
    print(f"续跑后状态:   {run['status']}；快照完整: "
          f"{snapshots.load(run['snapshot_id']).is_complete}")

    docs = read_jsonl(run["output_path"])
    for doc in docs:
        print(f"  钻孔 {doc['borehole_id']}: 区间 {len(doc['intervals'])} · 样品 "
              f"{len(doc['samples'])} · 测量 {len(doc['measurements'])} · 冲突 "
              f"{len(doc['conflicts'])} · 来源页 {doc['source_pages']}")

    # --------------------------------------------------- 2. semantics showcase
    hr("2. 值语义：检出限 / 实测零 / 缺失 / 单位归一（以 S103、S102 为例）")
    zk01 = next(d for d in docs if d["borehole_id"] == "ZK01")
    for measurement in zk01["measurements"]:
        if measurement["sample_id"] in ("S103",) or (
            measurement["sample_id"] == "S102" and measurement["analyte"] in ("Cu", "Zn")
        ):
            value = measurement["value"]
            print(f"  {measurement['identity']:<48} kind={value['kind']:<9} "
                  f"value={str(value['value']):<8} unit={value['unit']:<7} "
                  f"raw={value['raw_text']!r} raw_unit={value.get('raw_unit')}")

    hr("3. 跨页互补与矛盾：冲突会被记录并阻止基线确认")
    for conflict in zk01["conflicts"]:
        print(f"  冲突 {conflict['identity']} 字段 {conflict['field']}: "
              f"{[v['value'] for v in conflict['values']]}；来源页 "
              f"{[s['page_id'] for s in conflict['sources']]}")
    try:
        confirm_baseline(store, run, snapshots.load(run["snapshot_id"]))
    except ValueError as exc:
        print(f"  基线确认被阻止 -> {exc}")

    # --------------------------------------------- 3b. manual adjudication --
    hr("3b. 人工裁决：审核员对照候选值与原页，选值并填写理由")
    conflict = zk01["conflicts"][0]
    decision = [{
        "borehole_id": "ZK01",
        "identity": conflict["identity"],
        "field": conflict["field"],
        "conflict_index": 0,
        "selected_index": 1,  # 0.012 from zk01-p2 (re-test result)
        "reason": "zk01-p2 为实验室复测，以复测值 0.012 为准",
    }]
    review, reviewed_run = create_review(store, "crawl", decision, reviewer="审核员A")
    print(f"  复核记录: {review['review_id']}（状态 {review['status']}），独立复核运行: "
          f"{reviewed_run['run_id']}")
    reviewed_rows = read_jsonl(reviewed_run["output_path"])
    reviewed_zk01 = next(d for d in reviewed_rows if d["borehole_id"] == "ZK01")
    chosen = next(m for m in reviewed_zk01["measurements"]
                  if m["identity"] == conflict["identity"])
    print(f"  复核结果采用候选值: {chosen['value']['value']} {chosen['value']['unit']}，"
          f"来源页 {chosen['provenance']['page_id']}；剩余冲突 "
          f"{len(reviewed_zk01['conflicts'])}")
    original_rows = read_jsonl(run["output_path"])
    print(f"  原运行保留且未改动: 仍有冲突 "
          f"{sum(len(d['conflicts']) for d in original_rows)} 个，输出文件未被覆盖")
    snapshot = snapshots.load(run["snapshot_id"])
    adjudicated_baseline = confirm_baseline(store, reviewed_run, snapshot, "人工裁决后的 ZK01 基线")
    print(f"  复核结果已确认基线: {adjudicated_baseline['baseline_id']} "
          f"(review_id={adjudicated_baseline['review_id']})")

    hr("3c. 同快照回放：候选值与来源一致才沿用裁决，否则回归不通过")
    replay = engine.replay("crawl", rule_text=v1["text"], new_run_id="crawl-replay")
    report = regression_report(store, adjudicated_baseline, replay)
    reuse = report["review_reuse"]
    print(f"  同规则回放: 可沿用 {reuse['reusable_count']} 项，需复核 "
          f"{reuse['needs_recheck_count']} 项；回归通过: {report['regression_passed']}")
    carried, carried_run = carry_review(store, review, replay)
    print(f"  裁决沿用 -> 新复核 {carried['review_id']}，复核运行 "
          f"{carried_run['run_id']}（无剩余冲突: {not carried_run['has_conflicts']}）")

    # Simulate a replay in which the conflict collapsed to only the selected
    # value: even that must force a re-review and fail the regression.
    vanished_rows = json.loads(json.dumps(read_jsonl(replay["output_path"])))
    for doc in vanished_rows:
        doc["conflicts"] = []
        for measurement in doc.get("measurements", []):
            if measurement["identity"] == conflict["identity"]:
                measurement["value"] = {"kind": "number", "value": 0.012,
                                        "unit": "mg/kg", "raw_text": "0.012"}
    vanished_path = store.run_output_path("crawl-replay-vanished")
    with open(vanished_path, "w", encoding="utf-8") as handle:
        for doc in vanished_rows:
            handle.write(json.dumps(doc, ensure_ascii=False, sort_keys=True) + "\n")
    vanished_run = dict(replay)
    vanished_run.update(run_id="crawl-replay-vanished", output_path=vanished_path,
                        has_conflicts=False)
    store.save_run(vanished_run)
    vanished_report = regression_report(store, adjudicated_baseline, vanished_run)
    print(f"  冲突消失（仅剩原选中值）: 回归通过 {vanished_report['regression_passed']}；"
          f"提示 -> {vanished_report['review_reuse']['items'][0]['detail']}")
    try:
        carry_review(store, review, vanished_run)
    except ReviewReuseError as exc:
        print(f"  沿用裁决被拒绝（HTTP API 会返回 409）: {exc}")

    # ------------------------------------------------- 4. clean baseline + diff
    hr("4. 干净钻孔 ZK02：确认基线，并在同一快照上回放（空差异）")
    zk02_manifest = [page for page in manifest if "zk02" in page["page_id"]]
    engine.start(RunConfig("clean", v1["rule_id"], v1["text"], zk02_manifest))
    clean_run = engine.run("clean")
    snapshot = snapshots.load(clean_run["snapshot_id"])
    baseline = confirm_baseline(store, clean_run, snapshot, "ZK02 成功回放基线")
    print(f"  已确认基线: {baseline['baseline_id']} (snapshot {baseline['snapshot_id']})")
    replay = engine.replay("clean", rule_text=v1["text"], new_run_id="clean-replay")
    report = regression_report(store, baseline, replay)
    print(f"  同规则回放差异: {report['summary']}；回归通过: {report['regression_passed']}")

    hr("5. 注入真实值变化与无法解释的字段，验证差异分类")
    old_rows = read_jsonl(clean_run["output_path"])
    new_rows = json.loads(json.dumps(old_rows))
    new_rows[0]["measurements"][0]["value"]["value"] = 9.99  # value change
    new_rows[0]["warnings"].append({
        "code": "UNEXPLAINED_COLUMN", "table": "assay_table", "page_id": "zk02-p1",
        "message": "rule cannot interpret header 新元素", "raw": "新元素"})
    changed = compare_runs(old_rows, new_rows)
    print(f"  差异统计: {changed['summary']}；回归通过: {changed['regression_passed']}")
    for change in changed["changes"]:
        detail = ", ".join(f"{fc['field']}:{fc['category']}" for fc in change["field_changes"])
        print(f"  - {change['record_type']:<11} {change['identity']:<46} "
              f"{change['category']} {detail}")
    report_path = store.save_report("demo-change-report", changed)
    print(f"  机器可读报告已导出: {report_path}")

    # ------------------------------------------------------------- 6. outputs
    hr("6. 产物清单")
    print(f"  JSON Lines 输出:   {run['output_path']}")
    print(f"  快照目录:          {snapshots.root}")
    print(f"  人工复核目录:      {store.reviews_dir}")
    print(f"  基线目录:          {store.baselines_dir}")
    print(f"  差异报告目录:      {store.reports_dir}")
    server.shutdown()
    print("\n演示完成。启动 Web 界面：")
    print(f"  python3 -I -m corebench.webapp --data-dir {data_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
