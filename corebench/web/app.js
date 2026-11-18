/* corebench workbench single-page UI (vanilla JS, no build step). */
const $ = (id) => document.getElementById(id);
const api = async (path, body) => {
  const opts = body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {};
  const resp = await fetch(path, opts);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
};
const toast = (msg) => {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  setTimeout(() => el.classList.remove("show"), 2600);
};
const esc = (v) => String(v ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ----------------------------------------------------------------- tabs
document.querySelectorAll("nav button").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $(`panel-${btn.dataset.tab}`).classList.add("active");
    refreshAll();
  });
});

// ----------------------------------------------------------------- rules
async function loadRuleVersions(selectId) {
  const { rules } = await api("/api/rules");
  const tbody = $("rule-versions").querySelector("tbody");
  tbody.innerHTML = rules.map((r) => `<tr>
    <td><code>${esc(r.rule_id)}</code></td><td>v${r.version_number}</td>
    <td>${esc(r.note)}</td><td>${esc(r.created_at)}</td>
    <td><button class="ghost" data-load="${esc(r.rule_id)}">载入编辑器</button></td></tr>`).join("");
  tbody.querySelectorAll("[data-load]").forEach((b) => b.addEventListener("click", async () => {
    const found = rules.find((r) => r.rule_id === b.dataset.load);
    $("rule-editor").value = found.text;
    $("rule-name").value = found.name;
  }));
  for (const id of ["run-rule"]) {
    const sel = $(id);
    if (!sel) continue;
    const current = sel.value;
    sel.innerHTML = rules.map((r) => `<option value="${esc(r.rule_id)}">${esc(r.rule_id)}</option>`).join("");
    if (current) sel.value = current;
  }
  return rules;
}

$("btn-save-rule").addEventListener("click", async () => {
  try {
    const saved = await api("/api/rules/save", {
      name: $("rule-name").value.trim(),
      text: $("rule-editor").value,
      note: $("rule-note").value.trim(),
    });
    toast(`已保存 ${saved.rule_id}`);
    await refreshAll();
  } catch (exc) {
    $("rule-validate").textContent = "保存失败：" + exc.message;
  }
});
$("btn-validate").addEventListener("click", () => {
  try {
    // Server-side validation happens on save; here we do a lightweight round trip
    // by attempting save into nothing: instead just check structure markers.
    const text = $("rule-editor").value;
    if (!text.includes("measurement:") || !text.includes("page:")) {
      throw new Error("缺少 page: 或 measurement: 段");
    }
    $("rule-validate").textContent = "结构检查通过（保存时服务端会做完整语法校验）";
  } catch (exc) {
    $("rule-validate").textContent = "校验失败：" + exc.message;
  }
});

// ------------------------------------------------------------------ runs
async function loadRuns() {
  const { runs } = await api("/api/runs");
  const tbody = $("runs-table").querySelector("tbody");
  tbody.innerHTML = runs.map((r) => `<tr>
    <td><code>${esc(r.run_id)}</code>${r.kind === "reviewed" ? '<span class="tag conflict">复核结果</span>' : ""}</td>
    <td>${esc(r.rule_id)}</td>
    <td><span class="tag ${esc(r.status)}">${esc(r.status)}</span></td>
    <td>${r.has_conflicts ? '<span class="tag conflict">有冲突</span>' : "无"}</td>
    <td>${r.has_warnings ? "有" : "无"}</td>
    <td><code>${esc(r.snapshot_id)}</code></td>
    <td>${esc(r.started_at)}</td>
    <td>
      <button class="ghost" data-resume="${esc(r.run_id)}">续跑/重放</button>
      <button class="ghost" data-output="${esc(r.run_id)}">查看结果</button>
      <button class="ghost" data-source="${esc(r.run_id)}">原页溯源</button>
    </td></tr>`).join("");
  tbody.querySelectorAll("[data-resume]").forEach((b) => b.addEventListener("click", async () => {
    try { await api("/api/runs/resume", { run_id: b.dataset.resume }); toast("运行已推进"); await refreshAll(); }
    catch (exc) { toast(exc.message); }
  }));
  tbody.querySelectorAll("[data-output]").forEach((b) => b.addEventListener("click", () => showOutput(b.dataset.output)));
  tbody.querySelectorAll("[data-source]").forEach((b) => b.addEventListener("click", () => showProvenance(b.dataset.source)));
  const sel = $("baseline-run");
  if (sel) {
    sel.innerHTML = runs.filter((r) => r.status === "completed").map((r) => `<option>${esc(r.run_id)}</option>`).join("");
  }
  const candidate = $("diff-candidate");
  if (candidate) {
    const previous = candidate.value;
    candidate.innerHTML = runs.filter((r) => r.status === "completed")
      .map((r) => `<option value="${esc(r.run_id)}">${esc(r.run_id)} (${esc(r.rule_id)})</option>`).join("");
    if (previous) candidate.value = previous;
  }
  return runs;
}

$("btn-start").addEventListener("click", async () => {
  try {
    const pages = JSON.parse($("run-pages").value);
    const runId = $("run-id").value.trim() || `run-${Date.now()}`;
    await api("/api/runs/start", { run_id: runId, rule_id: $("run-rule").value, pages });
    const run = await api("/api/runs/resume", { run_id: runId });
    toast(run.status === "completed" ? "采集完成" : `运行状态：${run.status}`);
    await refreshAll();
  } catch (exc) { toast("启动失败：" + exc.message); }
});
$("btn-refresh-runs").addEventListener("click", refreshAll);

async function showOutput(runId) {
  const [{ lines }, { reviews }] = await Promise.all([
    api(`/api/output?run_id=${encodeURIComponent(runId)}`),
    api("/api/reviews"),
  ]);
  const runReviews = reviews.filter((rv) => rv.source_run_id === runId);
  const reviewBanner = runReviews.length ? runReviews.map((rv) =>
    `<p class="diff-add">已有复核 <code>${esc(rv.review_id)}</code> · 状态 ${esc(rv.status)} · 未解决 ${rv.unresolved.length} 项</p>`).join("") : "";
  const html = lines.map((doc, docIndex) => {
    const conflicts = (doc.conflicts || []).map((c, conflictIndex) => renderConflictCard(runId, doc, c, conflictIndex)).join("");
    const adjudications = (doc.adjudications || []).map((a) =>
      `<li class="diff-add">已裁决 ${esc(a.identity)} · 采用候选 #${a.selected_index} · 理由：${esc(a.reason)}</li>`).join("");
    const meas = (doc.measurements || []).map((m) => `<tr>
      <td>${esc(m.sample_id)}</td><td>${esc(m.analyte)}</td><td>${esc(m.method)}</td>
      <td><span class="tag ${esc(m.value.kind)}">${esc(m.value.kind)}</span></td>
      <td>${esc(m.value.value)}</td><td>${esc(m.value.unit)}</td><td class="muted">${esc(m.value.raw_text)}</td>
      <td><button class="ghost" data-trace='${esc(JSON.stringify({ snapshot_run: runId, page_id: m.provenance.page_id }))}'>溯源</button>
      ${m.adjudication ? '<span class="tag conflict">人工裁决</span>' : ""}</td>
    </tr>`).join("");
    return `<details open><summary>钻孔 ${esc(doc.borehole_id)} · 区间 ${doc.intervals.length} · 样品 ${doc.samples.length} · 测量 ${doc.measurements.length} · 冲突 ${(doc.conflicts || []).length} · 页 ${esc((doc.source_pages || []).join(", "))}</summary>
      ${reviewBanner}
      ${adjudications ? `<ul>${adjudications}</ul>` : ""}
      ${conflicts}
      <table class="grid"><thead><tr><th>样品</th><th>项目</th><th>方法</th><th>类型</th><th>归一值</th><th>单位</th><th>原文</th><th></th></tr></thead><tbody>${meas}</tbody></table>
    </details>`;
  }).join("");
  $("run-output").innerHTML = html || "<p class='muted'>尚无输出（运行可能未完成）。</p>";
  $("run-output").querySelectorAll("[data-trace]").forEach((b) => b.addEventListener("click", () => {
    const payload = JSON.parse(b.dataset.trace);
    if (payload.page_id) showSourcePage(runId, payload.page_id);
  }));
  bindReviewForms(runId);
}

function renderConflictCard(runId, doc, conflict, conflictIndex) {
  const candidates = conflict.values.map((v, index) => {
    const src = conflict.sources[index] || {};
    return `<tr class="${index === 0 ? "" : ""}">
      <td><label><input type="radio" name="pick-${esc(doc.borehole_id)}-${conflictIndex}" value="${index}"> 候选 #${index}</label></td>
      <td><span class="tag ${esc(v.kind)}">${esc(v.kind)}</span></td>
      <td>${esc(v.value)}</td><td>${esc(v.unit)}</td><td class="muted">${esc(v.raw_text)}</td>
      <td class="muted">${esc(src.page_id || "")} ${esc(src.table || "")} sha1 <code>${esc((src.content_sha1 || "").slice(0, 10))}</code>
        <br>${esc((src.raw_row || []).join(" | "))}${src.raw_cell ? " | cell=" + esc(src.raw_cell) : ""}
        <br><button class="ghost" data-trace='${esc(JSON.stringify({ page_id: src.page_id }))}'>查看原页</button></td>
    </tr>`;
  }).join("");
  const formId = `review-form-${esc(doc.borehole_id)}-${conflictIndex}`;
  return `<div class="review-card">
    <h4>冲突 · ${esc(conflict.identity)} · 字段 ${esc(conflict.field)}
      <span class="tag conflict">待人工裁决</span></h4>
    <p class="muted">对照各候选值与原页来源，选择一项并填写理由；提交后生成独立复核结果，原运行保留不变。</p>
    <table class="grid"><thead><tr><th>选择</th><th>类型</th><th>归一值</th><th>单位</th><th>原文</th><th>来源（原页 / 原始内容）</th></tr></thead>
      <tbody>${candidates}</tbody></table>
    <form id="${formId}" class="review-form" data-run='${esc(JSON.stringify({
      run_id: runId, borehole_id: doc.borehole_id, identity: conflict.identity,
      field: conflict.field, conflict_index: conflictIndex,
    }))}'>
      <div class="row">
        <label>审核员 <input type="text" name="reviewer" placeholder="工号/姓名"></label>
        <label>裁决理由 <input type="text" name="reason" placeholder="必填，例如：以 p2 复测值为准" style="min-width:320px"></label>
        <button class="primary" type="submit">提交复核</button>
      </div>
    </form>
  </div>`;
}

function bindReviewForms(runId) {
  $("run-output").querySelectorAll(".review-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const meta = JSON.parse(form.dataset.run);
      const picked = form.querySelector(`input[name="pick-${esc(meta.borehole_id)}-${meta.conflict_index}"]:checked`);
      if (!picked) return toast("请先选择一个候选值");
      if (!form.elements.reason.value.trim()) return toast("请填写裁决理由");
      try {
        const result = await api("/api/reviews/create", {
          run_id: meta.run_id,
          reviewer: form.elements.reviewer.value.trim(),
          note: "",
          decisions: [{
            borehole_id: meta.borehole_id, identity: meta.identity, field: meta.field,
            conflict_index: meta.conflict_index, selected_index: Number(picked.value),
            reason: form.elements.reason.value.trim(),
          }],
        });
        toast(`复核已生成：${result.review.review_id}；复核运行 ${result.reviewed_run.run_id}`);
        await refreshAll();
        showOutput(meta.run_id);
      } catch (exc) { toast("复核提交失败：" + exc.message); }
    });
  });
}

async function showProvenance(runId) {
  const run = await api(`/api/run?id=${encodeURIComponent(runId)}`);
  const pages = run.pages.map((p) => `<li><button class="ghost" data-page="${esc(p.page_id)}">${esc(p.page_id)} <span class="tag ${esc(p.state)}">${esc(p.state)}</span></button>
    ${p.error ? `<span class="diff-del">${esc(p.error)}</span>` : ""}</li>`).join("");
  $("run-output").innerHTML = `<p>快照 <code>${esc(run.snapshot_id)}</code>，点击页面查看原始 HTML：</p><ul>${pages}</ul><div id="source-view"></div>`;
  $("run-output").querySelectorAll("[data-page]").forEach((b) => b.addEventListener("click", () => showSourcePage(runId, b.dataset.page)));
}

async function showSourcePage(runId, pageId) {
  const run = await api(`/api/run?id=${encodeURIComponent(runId)}`);
  const data = await api(`/api/snapshot-page?snapshot_id=${encodeURIComponent(run.snapshot_id)}&page_id=${encodeURIComponent(pageId)}`);
  $("source-view") ? null : null;
  const target = $("source-view") || $("run-output");
  target.innerHTML += `<details open><summary>原页 ${esc(pageId)} · ${esc(data.url)} · sha1 <code>${esc(data.content_sha1.slice(0, 12))}</code></summary>
    <pre class="raw">${esc(data.html)}</pre></details>`;
}

// ------------------------------------------------------------------ diff
async function loadBaselines(selectId) {
  const { baselines } = await api("/api/baselines");
  const tbody = $("baselines-table").querySelector("tbody");
  tbody.innerHTML = baselines.map((b) => `<tr>
    <td><code>${esc(b.baseline_id)}</code></td><td>${esc(b.run_id)}</td><td>${esc(b.rule_id)}</td>
    <td>${b.review_id ? `<code>${esc(b.review_id)}</code>` : "—"}</td>
    <td><code>${esc(b.snapshot_id)}</code></td><td>${esc(b.confirmed_at)}</td><td>${esc(b.note)}</td></tr>`).join("");
  const sel = $("diff-baseline");
  if (sel) sel.innerHTML = baselines.map((b) => `<option>${esc(b.baseline_id)}</option>`).join("");
}

$("btn-diff").addEventListener("click", async () => {
  try {
    const report = await api("/api/diff", {
      baseline_id: $("diff-baseline").value,
      candidate_run_id: $("diff-candidate").value,
    });
    renderDiff(report);
    window.__lastReport = report;
  } catch (exc) { toast("差异生成失败：" + exc.message); }
});

function renderDiff(report) {
  const passed = report.regression_passed;
  const reuse = report.review_reuse;
  const reuseBanner = reuse ? `<p>人工裁决沿用：可沿用 <b class="diff-add">${reuse.reusable_count}</b> 项 ·
    需复核 <b class="diff-del">${reuse.needs_recheck_count}</b> 项
    （候选值与来源完全一致才可沿用；冲突消失即使只剩原选中值也判未通过）</p>` : "";
  $("diff-summary").innerHTML = `回归结论：<b class="${passed ? "diff-add" : "diff-del"}">${passed ? "通过" : "未通过"}</b>
    · 统计 ${esc(JSON.stringify(report.summary))} · 快照完整：${report.snapshot_complete ? "是" : '<span class="diff-del">否</span>'}${reuseBanner}`;
  const rows = report.changes.map((ch) => {
    const fields = ch.field_changes.map((fc) => {
      const src = fc.new_source || fc.old_source || ch.new_source || ch.old_source || {};
      const tagClass = fc.category === "review_recheck_required" ? "conflict"
        : fc.category === "value_changed" ? "conflict" : "censored";
      return `<tr><td>${esc(fc.field)}</td><td><span class="tag ${tagClass}">${esc(fc.category)}</span></td>
        <td class="diff-del">${esc(JSON.stringify(fc.old))}</td><td class="diff-add">${esc(JSON.stringify(fc.new))}</td>
        <td>${esc(fc.detail || "")}</td>
        <td class="muted">${esc(src.page_id || "")} ${esc(src.table || "")} ${esc((src.raw_row || []).join(" | "))}${src.raw_cell ? " | cell=" + esc(src.raw_cell) : ""}</td></tr>`;
    }).join("");
    const carried = ch.adjudication_carried ? ' <span class="tag conflict">裁决可沿用</span>' : "";
    return `<details ${ch.category === "review_recheck_required" ? "open" : ""}><summary><span class="diff-chg">${esc(ch.category)}</span> · ${esc(ch.record_type)} · <code>${esc(ch.identity)}</code>${carried}</summary>
      <table class="grid"><thead><tr><th>字段</th><th>类别</th><th>旧值</th><th>新值</th><th>说明</th><th>对应原页 / 原始内容</th></tr></thead><tbody>${fields}</tbody></table></details>`;
  }).join("");
  $("diff-results").innerHTML = rows || "<p class='diff-add'>无业务数据变化（字段改名、列表换序、等价单位均已忽略）。</p>";
}

$("btn-export").addEventListener("click", () => {
  if (!window.__lastReport) return toast("请先生成差异报告");
  const blob = new Blob([JSON.stringify(window.__lastReport, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = `${window.__lastReport.report_id}.json`; a.click();
  URL.revokeObjectURL(url);
});

// -------------------------------------------------------------- baselines
$("btn-confirm-baseline").addEventListener("click", async () => {
  try {
    const b = await api("/api/baselines/confirm", {
      run_id: $("baseline-run").value,
      note: $("baseline-note").value.trim(),
    });
    toast(`基线已确认：${b.baseline_id}`);
    await refreshAll();
  } catch (exc) { toast("基线确认被阻止：" + exc.message); }
});

// ----------------------------------------------------------------- boot
async function refreshAll() {
  const rules = await loadRuleVersions();
  if (!$("rule-editor").value && rules.length) $("rule-editor").value = rules[rules.length - 1].text;
  await loadRuns();
  await loadBaselines();
}
refreshAll();
