import { icon } from "./icons.js";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const secs = (t) => (t == null ? "" : t < 60 ? `${+t.toFixed(t < 10 ? 2 : 1)}s` : `${Math.floor(t / 60)}m ${Math.round(t % 60)}s`);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const COMPONENT = {
  pipeline: "CI pipeline", build: "Image build", registry: "Registry", orchestrator: "Scheduler", container: "Container",
  config: "Config and secrets", app: "Application", database: "Database", "load-balancer": "Load balancer",
};
const SEVERITY = { blocked: "Deployment blocked", degraded: "Service degraded", warning: "Warnings only" };
const RISK = { "read-only": "Read-only", reversible: "Reversible", destructive: "Destructive", review: "Needs review" };
const MARK = { root_cause: "Probable root cause", first_anomaly: "First anomaly", final_failure: "Final failure" };
const CONF = { high: "High", medium: "Medium", low: "Low" };
const MAX_FULL_LOG = 3000;

const state = { health: null, report: null, map: null, mapView: null, timer: null };

// ---------- startup ----------
init();

async function init() {
  bindForm();
  $("#case").addEventListener("click", onLineLink);
  try {
    const [health, samples] = await Promise.all([api("/api/health"), api("/api/samples")]);
    state.health = health;
    renderEngine(health);
    renderSamples(samples);
  } catch {
    $("#engine").textContent = "Server not reachable. Start it with uvicorn app.main:app";
    $("#engine").dataset.state = "off";
  }
}

async function api(url, opts) {
  const res = await fetch(url, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Request failed with status ${res.status}.`);
  return body;
}

function renderEngine(h) {
  const el = $("#engine"), note = $("#llm-note"), box = $("#use-llm");
  if (h.llm === "groq") {
    el.textContent = `AI agent on, ${h.model} via Groq`;
    el.dataset.state = "on";
    note.textContent = `(${h.model})`;
  } else if (h.llm === "error") {
    el.textContent = `AI agent unavailable, ${h.llm_error}. Reports use the rule engine`;
    el.dataset.state = "error";
    note.textContent = `(${h.model}, currently failing)`;
  } else {
    el.textContent = "AI agent off, rule engine only";
    el.dataset.state = "off";
    note.textContent = "(off: no GROQ_API_KEY on the server)";
    box.checked = false;
    box.disabled = true;
  }
}

function renderSamples(samples) {
  const ul = $("#sample-list");
  ul.innerHTML = samples.map((s) => `<li><button type="button" data-id="${esc(s.id)}"><span>${esc(s.title)}</span>${s.has_success_log ? '<span class="muted">with comparison</span>' : ""}</button></li>`).join("");
  ul.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-id]");
    if (!btn) return;
    try {
      const s = await api(`/api/samples/${encodeURIComponent(btn.dataset.id)}`);
      $("#failed-log").value = s.failed_log;
      $("#success-log").value = s.success_log || "";
      $("#success-block").open = !!s.success_log;
      clearFiles();
      $("#failed-note").textContent = `Sample loaded: ${s.title}`;
      runAnalysis();
    } catch (e) { showFormError(e.message); }
  });
}

// ---------- form ----------
function bindForm() {
  const form = $("#analyze-form");
  form.addEventListener("submit", (ev) => { ev.preventDefault(); runAnalysis(); });
  $("#failed-log").addEventListener("input", () => { showFormError(""); $("#failed-note").textContent = ""; });
  for (const [fileId, areaId] of [["#failed-file", "#failed-log"], ["#success-file", "#success-log"]]) {
    const input = $(fileId), area = $(areaId);
    input.addEventListener("change", () => loadFile(input.files[0], area, fileId === "#failed-file"));
    area.addEventListener("dragover", (ev) => { ev.preventDefault(); area.classList.add("is-drop"); });
    area.addEventListener("dragleave", () => area.classList.remove("is-drop"));
    area.addEventListener("drop", (ev) => {
      ev.preventDefault(); area.classList.remove("is-drop");
      if (ev.dataTransfer.files[0]) loadFile(ev.dataTransfer.files[0], area, fileId === "#failed-file");
    });
  }
}

function clearFiles() { $("#failed-file").value = ""; $("#success-file").value = ""; }

function loadFile(file, area, isFailed) {
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) { showFormError(`${file.name} is larger than 5 MB. Trim it to the failing run.`); return; }
  const reader = new FileReader();
  reader.onload = () => {
    area.value = reader.result;
    if (isFailed) $("#failed-note").textContent = `${file.name}, ${reader.result.split("\n").length} lines`;
    showFormError("");
  };
  reader.readAsText(file);
}

function showFormError(msg) { $("#form-error").textContent = msg; }

async function runAnalysis() {
  const failed = $("#failed-log").value;
  if (!failed.trim()) {
    showFormError("Add the log from the failed deployment first.");
    $("#failed-log").focus();
    return;
  }
  const btn = $("#analyze-btn");
  btn.disabled = true;
  btn.textContent = "Analyzing";
  showFormError("");
  teardownMap();
  $("#case").innerHTML = `<p class="loading">Reading the log and running the troubleshooting agent</p>`;
  const body = new FormData();
  body.append("failed_log", failed);
  body.append("success_log", $("#success-log").value);
  body.append("use_llm", $("#use-llm").checked ? "true" : "false");
  try {
    const report = await api("/api/analyze", { method: "POST", body });
    state.report = report;
    renderReport(report);
    if (state.health?.llm !== "off") api("/api/health").then((h) => { state.health = h; renderEngine(h); }).catch(() => {});
  } catch (e) {
    $("#case").innerHTML = `<div class="empty"><h2>Analysis failed</h2><p>${esc(e.message)}</p></div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Analyze log";
  }
}

// ---------- report ----------
function renderReport(r) {
  const node = $("#report-tpl").content.cloneNode(true);
  const q = (k) => node.querySelector(`[data-js="${k}"]`);
  const rc = r.root_cause;

  if (r.status === "no_errors") {
    $("#case").innerHTML = `<div class="empty"><h2>No errors found</h2><p>${esc(r.summary)}</p></div>`;
    return;
  }

  const sev = r.severity || "blocked";
  node.querySelector(".verdict").dataset.severity = sev;
  q("sev-lamp").dataset.state = sev === "blocked" ? "failed" : sev === "degraded" ? "degraded" : "ok";
  q("severity").textContent = SEVERITY[sev] || sev;
  q("root-title").textContent = rc.title;
  q("root-expl").textContent = rc.explanation || "";
  const c = r.confidence;
  q("confidence").innerHTML = `<span class="conf" data-level="${esc(c.level)}"><span class="conf-bars" aria-hidden="true"><i></i><i></i><i></i></span>${esc(CONF[c.level])}</span>`;
  q("component").textContent = COMPONENT[rc.component] || rc.component;
  q("first-seen").innerHTML = rc.first_line
    ? `<button type="button" class="line-link" data-line="${rc.first_line}">Line ${rc.first_line}</button> <span class="muted">at ${esc(secs(rc.first_t))}</span>`
    : "Unknown";
  q("mode").textContent = r.mode === "llm" ? `AI agent, ${r.llm_model}` : "Rule engine";
  q("reasons").innerHTML = c.reasons.map((x) => `<li>${esc(x)}</li>`).join("");
  q("summary").textContent = r.summary;
  if (r.llm_error) {
    q("notice").hidden = false;
    q("notice").textContent = `The AI agent failed (${r.llm_error}), so this diagnosis comes from the rule engine.`;
  } else if (r.stats.truncated) {
    q("notice").hidden = false;
    q("notice").textContent = "This log was longer than 20,000 lines; only the first 20,000 were analysed.";
  }

  renderEvidence(q("evidence"), r, new Set(rc.evidence_ids));
  q("full-log").addEventListener("click", (ev) => toggleFullLog(ev.currentTarget, $(".logview"), r));
  renderFixes(q("fixes"), r.fixes);
  q("fix-count").textContent = `${r.fixes.length} step${r.fixes.length === 1 ? "" : "s"}, safest first`;
  q("prevention").textContent = r.prevention ? `Prevent it next time: ${r.prevention}` : "";
  renderTimeline(q("timeline"), q("links"), r);
  q("tl-source").textContent = `Ordered by ${r.timeline.time_source}`;
  if (r.diff) { q("diff-panel").hidden = false; renderDiff(q("diff"), r.diff); }
  renderTrace(q("trace"), q("other-findings"), r);
  q("elapsed").textContent = r.elapsed_ms < 1000 ? `${r.elapsed_ms} ms` : `${(r.elapsed_ms / 1000).toFixed(1)}s`;
  bindFeedback(node, r);

  const caseEl = $("#case");
  caseEl.innerHTML = "";
  caseEl.appendChild(node);
  setupMap(r);
  caseEl.querySelector(".verdict-title").setAttribute("tabindex", "-1");
  if (window.innerWidth < 1100) caseEl.scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth" });
}

function logLine(ev, cls = "") {
  return `<div class="logline ${cls}" data-line="${ev.id}" data-level="${esc(ev.level)}"><span class="ln">${ev.id}</span><span class="msg">${esc(ev.raw)}</span></div>`;
}

function renderEvidence(el, r, evidence) {
  const byId = new Map(r.events.map((e) => [e.id, e]));
  const ids = [...evidence].sort((a, b) => a - b);
  if (!ids.length) { el.innerHTML = `<p class="gap">No log line could be confirmed as evidence.</p>`; return; }
  const show = new Set();
  for (const id of ids) for (let k = id - 2; k <= id + 2; k++) if (byId.has(k)) show.add(k);
  const sorted = [...show].sort((a, b) => a - b);
  let html = "", prev = null;
  for (const id of sorted) {
    if (prev !== null && id > prev + 1) html += `<div class="gap">${id - prev - 1} lines hidden</div>`;
    html += logLine(byId.get(id), evidence.has(id) ? "is-evidence" : "");
    prev = id;
  }
  el.innerHTML = html;
  el.dataset.mode = "evidence";
}

function toggleFullLog(btn, el, r) {
  const evidence = new Set(r.root_cause.evidence_ids);
  if (el.dataset.mode === "full") {
    renderEvidence(el, r, evidence);
    btn.textContent = "Show full log";
    return;
  }
  const events = r.events.slice(0, MAX_FULL_LOG);
  el.innerHTML = events.map((e) => logLine(e, evidence.has(e.id) ? "is-evidence" : "")).join("") +
    (r.events.length > MAX_FULL_LOG ? `<div class="gap">Showing the first ${MAX_FULL_LOG} of ${r.events.length} lines</div>` : "");
  el.dataset.mode = "full";
  btn.textContent = "Show evidence only";
}

function onLineLink(ev) {
  const b = ev.target.closest(".line-link");
  if (!b) return;
  const id = +b.dataset.line, view = $(".logview");
  let row = view.querySelector(`.logline[data-line="${id}"]`);
  if (!row) { toggleFullLog($('[data-js="full-log"]'), view, state.report); row = view.querySelector(`.logline[data-line="${id}"]`); }
  if (!row) return;
  view.querySelectorAll(".is-target").forEach((x) => x.classList.remove("is-target"));
  row.classList.add("is-target");
  view.scrollTop = row.offsetTop - view.offsetTop - 40;
  view.scrollIntoView({ block: "nearest", behavior: reducedMotion ? "auto" : "smooth" });
}

function renderFixes(el, fixes) {
  if (!fixes.length) { el.innerHTML = `<li class="muted">No fix could be proposed for this error.</li>`; return; }
  el.innerHTML = fixes.map((f, i) => `
    <li class="fix">
      <div class="fix-head"><span class="fix-title">${esc(f.title)}</span><span class="risk" data-risk="${esc(f.risk)}" title="${esc(f.risk_reason)}">${esc(RISK[f.risk])}</span></div>
      <div class="cmd"><pre><code>${esc(f.command)}</code></pre><button type="button" class="copy" data-copy="${i}" aria-label="Copy command">${icon("copy", 15)}</button></div>
      ${f.dry_run ? `<div><p class="cmd-label">Preview without applying</p><div class="cmd"><pre><code>${esc(f.dry_run)}</code></pre><button type="button" class="copy" data-copy-dry="${i}" aria-label="Copy preview command">${icon("copy", 15)}</button></div></div>` : ""}
      ${f.verify ? `<p class="fix-meta"><b>Confirm it worked:</b> ${esc(f.verify)}</p>` : ""}
      ${f.why ? `<p class="fix-meta">${esc(f.why)}</p>` : ""}
      <p class="fix-meta">${esc(f.risk_reason)}</p>
      <ul class="checks">${f.checks.map((c) => `<li data-status="${esc(c.status)}">${icon(c.status === "pass" ? "check" : "alert", 14)}<span><b>${esc(c.name)}:</b> ${esc(c.detail)}</span></li>`).join("")}</ul>
      <p class="source-tag">${f.source === "ai" ? "Suggested by the AI agent" : f.source === "fallback" ? "General diagnostic step" : "From the troubleshooting knowledge base"}</p>
    </li>`).join("");
  el.addEventListener("click", async (ev) => {
    const b = ev.target.closest(".copy");
    if (!b) return;
    const f = fixes[+(b.dataset.copy ?? b.dataset.copyDry)];
    const text = b.dataset.copy !== undefined ? f.command : f.dry_run;
    try {
      await navigator.clipboard.writeText(text);
      b.innerHTML = icon("check", 15);
      b.setAttribute("aria-label", "Copied");
      setTimeout(() => { b.innerHTML = icon("copy", 15); b.setAttribute("aria-label", "Copy command"); }, 1400);
    } catch { b.setAttribute("aria-label", "Copy failed. Select the text instead."); }
  });
}

function renderTimeline(el, linksEl, r) {
  const tl = r.timeline;
  const titles = Object.fromEntries(r.findings.map((f) => [f.kb_id, f.title]));
  linksEl.innerHTML = tl.links.map((l) => `<li>${esc(l.explanation)}<span class="by">${l.explained_by === "ai" ? "explained by AI" : "from rule order"}</span></li>`).join("");
  el.innerHTML = tl.items.map((it) => `
    <li data-level="${esc(it.level)}" class="${it.marks.includes("root_cause") ? "is-root" : ""}">
      <time>${esc(secs(it.t))}</time>
      <span class="tl-msg"><button type="button" class="line-link" data-line="${it.line}">L${it.line}</button> ${esc(it.message)}</span>
      ${it.marks.length || it.finding ? `<span class="marks">${it.marks.map((m) => `<span class="mark" data-mark="${m}">${MARK[m]}</span>`).join("")}${it.finding && !it.marks.includes("root_cause") ? `<span class="mark">${esc(titles[it.finding] || it.finding)}${it.symptom ? ", symptom" : ""}</span>` : ""}</span>` : ""}
    </li>`).join("");
}

function renderDiff(el, d) {
  const versions = d.version_changes.length
    ? `<table><thead><tr><th>Name</th><th>Last success</th><th>Failed run</th></tr></thead><tbody>${d.version_changes.map((v) => `<tr><td>${esc(v.name)}</td><td class="mono">${esc(v.before)}</td><td class="mono">${esc(v.after)}</td></tr>`).join("")}</tbody></table>`
    : `<p class="muted">No version numbers changed between the two runs.</p>`;
  const newLines = d.new_in_failed.slice(0, 12).map((x) => `<div class="logline" data-level="${esc(x.level)}"><span class="ln">${x.line}</span><span class="msg">${esc(x.message)}</span></div>`).join("");
  const missing = d.missing_from_failed.slice(0, 10).map((x) => `<div class="logline"><span class="ln">${x.line}</span><span class="msg">${esc(x.message)}</span></div>`).join("");
  el.innerHTML = `<div class="diff-grid">
    <div><h4>Versions that changed</h4>${versions}</div>
    <div><h4>Only in the failed run (${d.counts.new})</h4><div class="logview">${newLines || '<p class="gap">None</p>'}</div></div>
    <div><h4>In the successful run but missing from the failed run (${d.counts.missing})</h4><div class="logview">${missing || '<p class="gap">None</p>'}</div></div>
  </div>`;
}

function renderTrace(el, otherEl, r) {
  const STEP = { log_analyzer: "Parsed and normalized the log", docker_plugin: "Docker plugin", database_plugin: "Database plugin",
    general_analysis: "General analysis", search_knowledge_base: "Searched the knowledge base", get_log_lines: "Read more log lines",
    final_answer: "Wrote the diagnosis", llm: "AI reasoning" };
  el.innerHTML = r.trace.map((t) => `<li><code>${esc(STEP[t.step] || t.step)}</code> ${esc(t.detail)}${t.by ? ` <span class="by">(${t.by === "llm" ? "chosen by the AI agent" : "rule engine"})</span>` : ""}</li>`).join("");
  const others = r.findings.filter((f) => f.kb_id !== r.root_cause.kb_id);
  if (others.length) {
    otherEl.innerHTML = `<div class="other"><h4>Other findings</h4><ul>${others.map((f) => `<li>${esc(f.title)}${f.symptom ? " (symptom)" : ""}, first at <button type="button" class="line-link" data-line="${f.first_line}">line ${f.first_line}</button></li>`).join("")}</ul></div>`;
  }
}

function bindFeedback(node, r) {
  const out = node.querySelector('[data-js="fb-result"]');
  const yes = node.querySelector('[data-js="fb-yes"]'), no = node.querySelector('[data-js="fb-no"]');
  const send = async (useful) => {
    yes.setAttribute("aria-pressed", String(useful));
    no.setAttribute("aria-pressed", String(!useful));
    try {
      const s = await api("/api/feedback", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ analysis_id: r.id, useful, root_cause: r.root_cause.kb_id || r.root_cause.title }) });
      out.textContent = `Rating saved. ${s.useful} of ${s.rated} rated analyses were useful.`;
    } catch (e) { out.textContent = e.message; }
  };
  yes.addEventListener("click", () => send(true));
  no.addEventListener("click", () => send(false));
}

// ---------- failure map ----------
function teardownMap() {
  clearInterval(state.timer);
  if (state.map) { state.map.dispose(); state.map = null; }
}

async function setupMap(r) {
  const stage = $('[data-js="map-stage"]');
  const play = $('[data-js="play"]'), scrub = $('[data-js="scrub"]'), clock = $('[data-js="clock"]');
  const segBtns = [...document.querySelectorAll(".seg-btn")];
  const mod = await import("./failure-map.js").catch(() => null);
  const map = r.timeline.map;
  const changes = map.nodes.flatMap((n) => n.changes.map((c) => c.t));
  const maxT = Math.max(r.timeline.duration || 0, ...changes, 0.001);
  let t = 0, playing = false;
  const canWebGL = mod && mod.webglAvailable();
  let view = canWebGL && window.innerWidth >= 640 ? "3d" : "2d";

  const select = (id) => showNode(id, r);
  const mount = () => {
    if (state.map) state.map.dispose();
    stage.innerHTML = "";
    if (!mod) { stage.innerHTML = `<p class="gap">The map could not be loaded.</p>`; return; }
    try {
      state.map = view === "3d" ? new mod.FailureMap3D(stage, map, { onSelect: select }) : new mod.FailureMap2D(stage, map, { onSelect: select });
    } catch {
      view = "2d";
      state.map = new mod.FailureMap2D(stage, map, { onSelect: select });
    }
    state.map.setTime(t, false);
    segBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
    const root = map.nodes.find((n) => n.root_cause);
    if (root) { state.map.select(root.id); showNode(root.id, r); }
  };
  const setT = (v, animate) => {
    t = Math.max(0, Math.min(maxT, v));
    state.map && state.map.setTime(t, animate);
    scrub.value = String(Math.round((t / maxT) * 1000));
    clock.textContent = `${secs(t)} / ${secs(maxT)}`;
  };
  const stop = () => { playing = false; play.innerHTML = icon("play"); play.setAttribute("aria-label", "Replay the failure"); };
  const start = () => {
    if (t >= maxT) setT(0, false);
    playing = true;
    play.innerHTML = icon("pause");
    play.setAttribute("aria-label", "Pause replay");
  };
  clearInterval(state.timer);
  let last = performance.now();
  state.timer = setInterval(() => {
    const now = performance.now(), dt = (now - last) / 1000;
    last = now;
    if (!playing) return;
    setT(t + dt * (maxT / 8), true);
    if (t >= maxT) stop();
  }, 33);

  play.addEventListener("click", () => (playing ? stop() : start()));
  scrub.addEventListener("input", () => { stop(); setT((+scrub.value / 1000) * maxT, false); });
  segBtns.forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.view === "3d" && !canWebGL) return;
    view = b.dataset.view; mount();
  }));
  if (!canWebGL) segBtns.find((b) => b.dataset.view === "3d").disabled = true;

  mount();
  setT(0, false);
  if (reducedMotion) setT(maxT, false);
  else start();
}

function showNode(id, r) {
  const el = $('[data-js="node-detail"]');
  if (!el) return;
  const node = r.timeline.map.nodes.find((n) => n.id === id);
  const lines = r.events.filter((e) => e.component === id && e.level !== "INFO" && e.level !== "DEBUG").slice(0, 6);
  const fallback = lines.length ? lines : r.events.filter((e) => e.component === id).slice(0, 4);
  const changes = node.changes.map((c) => `${c.state === "failed" ? "failed" : "degraded"} at ${secs(c.t)} (line ${c.line})`).join(", then ");
  el.innerHTML = `<h4></h4>
    <p class="muted">${node.changes.length ? `Became ${esc(changes)}.` : "No warnings or errors from this component."} ${node.line_count} log line${node.line_count === 1 ? "" : "s"}.</p>
    ${fallback.length ? `<div class="logview" style="margin-top:8px">${fallback.map((e) => logLine(e, r.root_cause.evidence_ids.includes(e.id) ? "is-evidence" : "")).join("")}</div>` : ""}`;
  el.querySelector("h4").textContent = node.label + (node.root_cause ? " (probable root cause)" : "");
}
