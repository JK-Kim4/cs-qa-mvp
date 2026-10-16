const $ = (sel) => document.querySelector(sel);
const chat = $("#chat");
const input = $("#input");
const form = $("#composer");
const sendBtn = $("#send");
const modal = $("#modal");
const modalBody = $("#modal-body");
const traces = new Map();

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

function attachResult(el, data) {
  const id = crypto.randomUUID();
  traces.set(id, data.trace);
  el.classList.add("clickable");
  el.dataset.trace = id;
  const bubble = el.querySelector(".bubble");
  bubble.classList.add("md");
  bubble.innerHTML = md(data.answer);
  const t = data.trace;
  const sources = t.selected_pages.map((p) => p.title).join(", ");
  el.insertAdjacentHTML(
    "beforeend",
    `<div class="meta"><span class="route ${t.answer_mode}">${ROUTE_LABEL[t.answer_mode] || t.answer_mode}</span>` +
      `<span>· ${(t.total_ms / 1000).toFixed(1)}초</span><span>· ${fmtCost(t.total_cost_usd)}</span>` +
      (sources ? `<span>· 근거: ${esc(sources)}</span>` : "") +
      (t.warnings.length ? `<span>· 경고 ${t.warnings.length}</span>` : "") +
      `<span>· 눌러서 상세 보기</span></div>`
  );
}

async function ask(question) {
  addMessage("user", question);
  const bot = addMessage("bot", "");
  bot.querySelector(".bubble").innerHTML = `<span class="typing">위키를 찾는 중…</span>`;
  sendBtn.disabled = true;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    attachResult(bot, await res.json());
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
  return esc([i !== undefined ? `입력 ${i.toLocaleString()}` : null, o !== undefined ? `출력 ${o.toLocaleString()}` : null].filter(Boolean).join(" / ") || JSON.stringify(u));
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
  const msg = e.target.closest(".msg.bot.clickable");
  if (msg && traces.has(msg.dataset.trace)) openTrace(traces.get(msg.dataset.trace));
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
