const $ = (sel) => document.querySelector(sel);
const chat = $("#chat");
const input = $("#input");
const form = $("#composer");
const sendBtn = $("#send");
const modal = $("#modal");
const modalBody = $("#modal-body");
const traces = new Map();
const compareToggle = $("#compare");
try { compareToggle.checked = localStorage.getItem("compare") === "1"; } catch {}

const SAMPLES = [
  "주문을 취소하고 싶어요",
  "I was charged twice for my subscription",
  "배송이 4일째 안 오고 있어요",
  "How can I reset my account password?",
  "오늘 날씨 어때요?",
];

// 실제로 답을 만든 방식 기준 (trace.answer_mode)
const ROUTE_LABEL = { generate: "LLM 생성", direct: "위키 답변 그대로", canned: "위키 원문 (LLM 미사용)", escalate: "상담원 연결" };
const STATUS_LABEL = { ok: "완료", skipped: "건너뜀", error: "오류" };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtCost(v) {
  if (v === null || v === undefined) return "-";
  if (v === 0) return "$0";
  return v < 0.01 ? `$${v.toFixed(5)}` : `$${v.toFixed(4)}`;
}

// 위키 본문 표시용 최소 마크다운 렌더러 (제목, 목록, 인용, 굵게, 인라인 코드)
function md(text) {
  const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const out = [];
  let list = null;
  let para = [];
  const flushPara = () => { if (para.length) { out.push(`<p>${para.map(inline).join("\n")}</p>`); para = []; } };
  const flushList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of text.split("\n")) {
    let m;
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) {
      flushPara(); flushList();
      out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/)) || (m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      flushPara();
      const tag = /^\s*\d+\./.test(line) ? "ol" : "ul";
      if (list !== tag) { flushList(); out.push(`<${tag}>`); list = tag; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^>\s?(.*)$/))) {
      flushPara(); flushList();
      out.push(`<blockquote>${inline(m[1])}</blockquote>`);
    } else if (!line.trim()) {
      flushPara(); flushList();
    } else {
      flushList();
      para.push(line);
    }
  }
  flushPara(); flushList();
  return out.join("");
}

async function loadStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    $("#status").innerHTML = [
      `<span class="chip">위키 ${s.pages}페이지</span>`,
      `<span class="chip ${s.decision.enabled ? "on" : "off"}">${s.decision.enabled ? `${esc(s.decision.label)} 연결됨` : "판단 모델 미설정"}</span>`,
      `<span class="chip ${s.llm.enabled ? "on" : "off"}">LLM ${s.llm.enabled ? esc(s.llm.model) : "미설정"}</span>`,
    ].join("");
  } catch {
    $("#status").textContent = "서버 상태를 불러오지 못했습니다";
  }
}

function addMessage(role, text) {
  $("#empty")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = `<div class="bubble"></div>`;
  el.querySelector(".bubble").textContent = text;
  chat.appendChild(el);
  chat.scrollTop = chat.scrollHeight;
  return el;
}

function metaHtml(t) {
  const sources = t.selected_pages.map((p) => p.title).join(", ");
  return (
    `<div class="meta"><span class="route ${t.answer_mode}">${ROUTE_LABEL[t.answer_mode] || t.answer_mode}</span>` +
    `<span>· ${(t.total_ms / 1000).toFixed(1)}초</span><span>· ${fmtCost(t.total_cost_usd)}</span>` +
    (sources ? `<span>· 근거: ${esc(sources)}</span>` : "") +
    (t.warnings.length ? `<span>· 경고 ${t.warnings.length}</span>` : "") +
    `<span>· 눌러서 상세 보기</span></div>`
  );
}

function keepTrace(trace) {
  const id = crypto.randomUUID();
  traces.set(id, trace);
  return id;
}

function attachResult(el, data) {
  el.classList.add("clickable");
  el.dataset.trace = keepTrace(data.trace);
  const bubble = el.querySelector(".bubble");
  bubble.classList.add("md");
  bubble.innerHTML = md(data.answer);
  el.insertAdjacentHTML("beforeend", metaHtml(data.trace));
}

// 비교 모드 결과: 두 경로를 나란히 보여 주고, 차이를 한 줄로 요약한다.
function attachCompare(el, data) {
  const a = data.with_decision, b = data.bypass;
  const card = (label, sub, r) =>
    `<div class="cmp-card" data-trace="${keepTrace(r.trace)}">
      <div class="cmp-head"><strong>${esc(label)}</strong><span>${esc(sub)}</span></div>
      <div class="bubble md">${md(r.answer)}</div>
      ${metaHtml(r.trace)}
    </div>`;
  const ta = a.trace, tb = b.trace;
  const pagesA = ta.selected_pages.map((p) => p.id).join(","), pagesB = tb.selected_pages.map((p) => p.id).join(",");
  const dCost = ta.total_cost_usd - tb.total_cost_usd, dMs = ta.total_ms - tb.total_ms;
  const sign = (v) => (v > 0 ? "+" : v < 0 ? "−" : "±");
  const diff = [
    ta.answer_mode === tb.answer_mode ? "답변 방식 같음" : `답변 방식 다름 (${ROUTE_LABEL[ta.answer_mode]} vs ${ROUTE_LABEL[tb.answer_mode]})`,
    pagesA === pagesB ? "근거 페이지 같음" : "근거 페이지 다름",
    `비용 A−B ${sign(dCost)}${fmtCost(Math.abs(dCost))}`,
    `시간 A−B ${sign(dMs)}${(Math.abs(dMs) / 1000).toFixed(1)}초`,
  ];
  el.classList.add("compare");
  el.querySelector(".bubble").remove();
  el.insertAdjacentHTML(
    "beforeend",
    `<div class="cmp-grid">${card("A · 판단 모델 경로", ta.steps.find((s) => s.key === "decide")?.title.replace(/^페이지 선택 /, "") || "", a)}` +
      `${card("B · 바이패스", "판단 모델 없이 BM25 1위 → LLM", b)}</div>` +
      `<div class="cmp-diff">${diff.map(esc).join(" · ")}</div>`
  );
}

async function ask(question) {
  addMessage("user", question);
  const bot = addMessage("bot", "");
  bot.querySelector(".bubble").innerHTML = `<span class="typing">위키를 찾는 중…</span>`;
  sendBtn.disabled = true;
  try {
    const compare = compareToggle.checked;
    if (compare) bot.querySelector(".bubble").innerHTML = `<span class="typing">두 경로로 답을 만드는 중…</span>`;
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, compare }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    if (compare) attachCompare(bot, data);
    else attachResult(bot, data);
  } catch (e) {
    bot.querySelector(".bubble").textContent = `요청에 실패했습니다. ${e.message}`;
  } finally {
    sendBtn.disabled = false;
    chat.scrollTop = chat.scrollHeight;
    input.focus();
  }
}

// ---- modal ----

function kv(rows) {
  const body = rows
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${v}</div>`)
    .join("");
  return body ? `<div class="kv">${body}</div>` : "";
}

function usageText(u) {
  if (!u) return null;
  const i = u.input_tokens ?? u.prompt_tokens;
  const o = u.output_tokens ?? u.completion_tokens;
  const c = u.cached_input_tokens;
  return esc([i !== undefined ? `입력 ${i.toLocaleString()}` : null, c ? `(캐시 ${c.toLocaleString()})` : null, o !== undefined ? `출력 ${o.toLocaleString()}` : null].filter(Boolean).join(" / ") || JSON.stringify(u));
}

function chosenIds(trace) {
  return new Set(trace.selected_pages.map((p) => p.id));
}

function renderSearch(d, picked) {
  if (!d.candidates?.length) return kv([["검색어", esc(d.query)], ["토큰", esc(d.tokens.join(", "))]]);
  const rows = d.candidates
    .map(
      (c) => `<tr class="${picked.has(c.id) ? "picked" : ""}">
        <td class="num">${c.rank}</td>
        <td><span class="link" data-page="${esc(c.id)}">${esc(c.title)}</span><div class="path">${esc(c.path)}</div></td>
        <td class="num">${c.bm25}</td></tr>
        <tr class="preview" data-preview="${esc(c.id)}" hidden><td></td><td colspan="2"><div class="md"></div></td></tr>`
    )
    .join("");
  return (
    kv([["검색어", esc(d.query)], ["검색 토큰", esc(d.tokens.join(", "))]]) +
    `<div class="table-wrap"><table><thead><tr><th>#</th><th>후보 페이지 (눌러서 본문 보기)</th><th>BM25</th></tr></thead><tbody>${rows}</tbody></table></div>`
  );
}

function renderDecide(d, picked) {
  if (!d.options) {
    return d.chosen ? kv([["선택", `${esc(d.chosen.path)} (BM25 1위)`]]) : "";
  }
  const rows = d.options
    .map(
      (o) => `<tr class="${o.id && picked.has(o.id) ? "picked" : ""}">
        <td>${esc(o.key)}</td><td>${esc(o.title)}</td>
        <td class="num">${(o.probability * 100).toFixed(1)}%</td>
        <td><div class="bar"><span style="width:${Math.round(o.probability * 100)}%"></span></div></td></tr>`
    )
    .join("");
  return (
    kv([["선택지", esc(d.choice)], ["토큰", usageText(d.usage)], ["비용", fmtCost(d.cost_usd)]]) +
    `<div class="table-wrap"><table><thead><tr><th>키</th><th>선택지</th><th>확률</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
  );
}

function renderStep(s, trace) {
  const d = s.detail || {};
  const picked = chosenIds(trace);
  let body = "";
  if (s.key === "rewrite") body = kv([["변환된 검색어", esc(d.query)], ["모델", esc(d.model)], ["토큰", usageText(d.usage)], ["비용", d.cost_usd !== undefined ? fmtCost(d.cost_usd) : null]]);
  if (s.key === "search") body = renderSearch(d, picked);
  if (s.key === "decide") body = renderDecide(d, picked);
  if (s.key === "answer")
    body = kv([
      ["방식", esc(d.mode)],
      ["모델", esc(d.model)],
      ["effort", esc(d.effort)],
      ["근거 파일", d.sources ? d.sources.map((p) => `<span class="path">${esc(p)}</span>`).join("<br>") : d.source ? `<span class="path">${esc(d.source)}</span>` : null],
      ["프롬프트 길이", d.prompt_chars ? `${d.prompt_chars.toLocaleString()}자` : null],
      ["토큰", usageText(d.usage)],
      ["비용", d.cost_usd !== undefined ? fmtCost(d.cost_usd) : null],
    ]);
  return `<li class="step">
    <div class="step-head"><span class="step-title">${esc(s.title)}</span>
      <span class="step-meta"><span class="badge ${s.status}">${STATUS_LABEL[s.status] || s.status}</span>${s.duration_ms}ms</span></div>
    ${s.reason ? `<div class="step-reason">${esc(s.reason)}</div>` : ""}
    ${body}
  </li>`;
}

function openTrace(trace) {
  const pages = trace.selected_pages
    .map((p, i) => `<details class="page" ${i === 0 ? "open" : ""}><summary>${esc(p.title)}<span class="path">${esc(p.path)}</span></summary><div class="md">${md(p.body)}</div></details>`)
    .join("");
  modalBody.innerHTML = `
    <div class="summary">
      ${trace.path ? `<div class="stat"><div class="k">경로</div><div class="v">${trace.path === "bypass" ? "B · 바이패스" : "A · 판단 모델"}</div></div>` : ""}
      <div class="stat"><div class="k">답변 방식</div><div class="v route ${trace.answer_mode}">${ROUTE_LABEL[trace.answer_mode] || esc(trace.answer_mode)}</div></div>
      <div class="stat"><div class="k">총 소요</div><div class="v">${(trace.total_ms / 1000).toFixed(2)}초</div></div>
      <div class="stat"><div class="k">추정 비용</div><div class="v">${fmtCost(trace.total_cost_usd)}</div></div>
      <div class="stat"><div class="k">근거 페이지</div><div class="v">${trace.selected_pages.length}개</div></div>
    </div>
    <div class="reason"><strong>판단 근거:</strong> ${esc(trace.route_reason)}</div>
    ${trace.warnings.map((w) => `<div class="warning">⚠ ${esc(w)}</div>`).join("")}
    <div class="section-title">질문</div>
    <div class="reason">${esc(trace.question)}</div>
    <div class="section-title">처리 경로</div>
    <ol class="steps">${trace.steps.map((s) => renderStep(s, trace)).join("")}</ol>
    <div class="section-title">답변에 사용한 위키 파일</div>
    ${pages || `<div class="step-reason">사용한 파일 없음</div>`}
    <details class="raw"><summary>원본 trace JSON</summary><pre class="json">${esc(JSON.stringify(trace, null, 2))}</pre></details>`;
  modal.hidden = false;
  document.body.style.overflow = "hidden";
}

function closeModal() {
  modal.hidden = true;
  document.body.style.overflow = "";
}

async function togglePreview(pageId) {
  const row = modalBody.querySelector(`tr[data-preview="${CSS.escape(pageId)}"]`);
  if (!row) return;
  if (!row.hidden) { row.hidden = true; return; }
  const box = row.querySelector(".md");
  if (!box.dataset.loaded) {
    box.textContent = "불러오는 중…";
    try {
      const p = await (await fetch(`/api/wiki/${pageId}`)).json();
      box.innerHTML = md(p.body);
      box.dataset.loaded = "1";
    } catch {
      box.textContent = "페이지를 불러오지 못했습니다";
    }
  }
  row.hidden = false;
}

// ---- events ----

chat.addEventListener("click", (e) => {
  const el = e.target.closest("[data-trace]");
  if (el && traces.has(el.dataset.trace)) openTrace(traces.get(el.dataset.trace));
});
compareToggle.addEventListener("change", () => {
  try { localStorage.setItem("compare", compareToggle.checked ? "1" : "0"); } catch {}
});
modal.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) closeModal();
  const link = e.target.closest("[data-page]");
  if (link) togglePreview(link.dataset.page);
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modal.hidden) closeModal(); });

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = input.value.trim();
  if (!q || sendBtn.disabled) return;
  input.value = "";
  input.style.height = "";
  ask(q);
});
input.addEventListener("keydown", (e) => {
  // 한글 조합 중 Enter 는 조합 확정이라 무시한다.
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
});
input.addEventListener("input", () => { input.style.height = ""; input.style.height = `${input.scrollHeight}px`; });

$("#samples").innerHTML = SAMPLES.map((s) => `<button type="button">${esc(s)}</button>`).join("");
$("#samples").addEventListener("click", (e) => { if (e.target.tagName === "BUTTON") ask(e.target.textContent); });

loadStatus();
input.focus();
