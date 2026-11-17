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
    <td><code>${esc(r.run_id)}</code></td><td>${esc(r.rule_id)}</td>
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
  const { lines } = await api(`/api/output?run_id=${encodeURIComponent(runId)}`);
  const html = lines.map((doc) => {
    const conflicts = (doc.conflicts || []).map((c) => `<li class="diff-chg">冲突 ${esc(c.identity)} · ${esc(c.field)}: ${esc(JSON.stringify(c.values))}</li>`).join("");
    const meas = (doc.measurements || []).map((m) => `<tr>
      <td>${esc(m.sample_id)}</td><td>${esc(m.analyte)}</td><td>${esc(m.method)}</td>
      <td><span class="tag ${esc(m.value.kind)}">${esc(m.value.kind)}</span></td>
      <td>${esc(m.value.value)}</td><td>${esc(m.value.unit)}</td><td class="muted">${esc(m.value.raw_text)}</td>
      <td><button class="ghost" data-trace='${esc(JSON.stringify({ snapshot_run: runId, page_id: m.provenance.page_id }))}'>溯源</button></td>
    </tr>`).join("");
    return `<details open><summary>钻孔 ${esc(doc.borehole_id)} · 区间 ${doc.intervals.length} · 样品 ${doc.samples.length} · 测量 ${doc.measurements.length} · 页 ${esc((doc.source_pages || []).join(", "))}</summary>
      ${conflicts ? `<ul>${conflicts}</ul>` : ""}
      <table class="grid"><thead><tr><th>样品</th><th>项目</th><th>方法</th><th>类型</th><th>归一值</th><th>单位</th><th>原文</th><th></th></tr></thead><tbody>${meas}</tbody></table>
    </details>`;
  }).join("");
  $("run-output").innerHTML = html || "<p class='muted'>尚无输出（运行可能未完成）。</p>";
  $("run-output").querySelectorAll("[data-trace]").forEach((b) => b.addEventListener("click", () => showSourcePage(runId, JSON.parse(b.dataset.trace).page_id)));
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
  $("diff-summary").innerHTML = `回归结论：<b class="${passed ? "diff-add" : "diff-del"}">${passed ? "通过" : "未通过"}</b>
    · 统计 ${esc(JSON.stringify(report.summary))} · 快照完整：${report.snapshot_complete ? "是" : '<span class="diff-del">否</span>'}`;
  const rows = report.changes.map((ch) => {
    const fields = ch.field_changes.map((fc) => {
      const src = fc.new_source || fc.old_source || ch.new_source || ch.old_source || {};
      return `<tr><td>${esc(fc.field)}</td><td><span class="tag ${fc.category === "value_changed" ? "conflict" : "censored"}">${esc(fc.category)}</span></td>
        <td class="diff-del">${esc(JSON.stringify(fc.old))}</td><td class="diff-add">${esc(JSON.stringify(fc.new))}</td>
        <td>${esc(fc.detail || "")}</td>
        <td class="muted">${esc(src.page_id || "")} ${esc(src.table || "")} ${esc((src.raw_row || []).join(" | "))}${src.raw_cell ? " | cell=" + esc(src.raw_cell) : ""}</td></tr>`;
    }).join("");
    return `<details><summary><span class="diff-chg">${esc(ch.category)}</span> · ${esc(ch.record_type)} · <code>${esc(ch.identity)}</code></summary>
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
