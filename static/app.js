const $ = (sel) => document.querySelector(sel);
const chat = $("#chat");
const input = $("#input");
const form = $("#composer");
const sendBtn = $("#send");
const endBtn = $("#end-chat");
const modal = $("#modal");
const modalBody = $("#modal-body");
const traces = new Map();
const compareToggle = $("#compare");
try { compareToggle.checked = localStorage.getItem("compare") === "1"; } catch {}
// 상담 종료 시 노션 티켓으로 보낼 대화. 티켓을 만들면 비우고 새 상담을 시작한다.
// 질문할 때도 최근 대화를 함께 보내 이어지는 질문("발송해 주세요")을 이해하게 한다.
let conversation = [];
const HISTORY_SEND = 6; // 서버가 쓰는 최근 메시지 수(app/pipeline.py HISTORY_MAX_MESSAGES)와 같게 둔다

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
    $("#guide-btn-label").textContent = `데이터 가이드 · ${s.pages}주제`;
    $("#status").innerHTML = [
      `<span class="chip ${s.decision.enabled ? "on" : "off"}">${s.decision.enabled ? `${esc(s.decision.label)} 연결됨` : "판단 모델 미설정"}</span>`,
      `<span class="chip ${s.llm.enabled ? "on" : "off"}">LLM ${s.llm.enabled ? esc(s.llm.model) : "미설정"}</span>`,
      `<span class="chip ${s.notion.enabled ? "on" : "off"}">노션 ${s.notion.enabled ? "연결됨" : "미설정"}</span>`,
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

// 답변을 한 번이라도 받은 뒤, 요청 중이 아닐 때만 상담을 끝낼 수 있다 (같은 대화로 티켓이 두 번 생기지 않게).
function syncEndBtn() {
  endBtn.disabled = sendBtn.disabled || !conversation.some((m) => m.role === "bot");
}

async function ask(question) {
  let escalated = false;
  addMessage("user", question);
  const history = conversation.slice(-HISTORY_SEND).map(({ role, text }) => ({ role, text }));
  conversation.push({ role: "user", text: question });
  const bot = addMessage("bot", "");
  bot.querySelector(".bubble").innerHTML = `<span class="typing">위키를 찾는 중…</span>`;
  sendBtn.disabled = true;
  syncEndBtn();
  try {
    const compare = compareToggle.checked;
    if (compare) bot.querySelector(".bubble").innerHTML = `<span class="typing">두 경로로 답을 만드는 중…</span>`;
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, compare, history }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
    const data = await res.json();
    if (compare) attachCompare(bot, data);
    else attachResult(bot, data);
    // 비교 모드에서는 실제 운영 경로인 판단 모델 경로(A)의 답변을 기록한다.
    const sent = compare ? data.with_decision : data;
    conversation.push({ role: "bot", text: sent.answer, route: sent.route });
    escalated = sent.route === "escalate";
  } catch (e) {
    bot.querySelector(".bubble").textContent = `요청에 실패했습니다. ${e.message}`;
  } finally {
    sendBtn.disabled = false;
    syncEndBtn();
    chat.scrollTop = chat.scrollHeight;
    input.focus();
  }
  // 상담원 연결로 분류되면 고객이 상담 종료를 누르지 않아도 담당자에게 넘어가도록 바로 티켓을 만든다.
  if (escalated) await createTicket("escalate");
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
  if (s.key === "rewrite") body = kv([["변환된 검색어", esc(d.query)], ["반영한 이전 질문", d.previous_questions?.length ? d.previous_questions.map(esc).join("<br>") : null], ["모델", esc(d.model)], ["토큰", usageText(d.usage)], ["비용", d.cost_usd !== undefined ? fmtCost(d.cost_usd) : null]]);
  if (s.key === "search") body = renderSearch(d, picked);
  if (s.key === "decide") body = renderDecide(d, picked);
  if (s.key === "answer")
    body = kv([
      ["방식", esc(d.mode)],
      ["모델", esc(d.model)],
      ["effort", esc(d.effort)],
      ["넘긴 이전 대화", d.mode === "generate" && trace.history_messages ? `최근 ${trace.history_messages}개 메시지` : null],
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
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!tour.hidden) endTour();
  else if (!guide.hidden) closeGuide();
  else if (!modal.hidden) closeModal();
});

// 티켓을 만드는 두 경우: 고객이 상담 종료를 눌렀을 때(end), 챗봇이 상담원 연결로 분류했을 때(escalate).
const TICKET_TEXT = {
  end: { pending: "상담을 종료하고 노션 티켓을 만드는 중…", done: "상담을 종료했어요.", fail: "티켓을 만들지 못했어요." },
  escalate: {
    pending: "담당자에게 전달하는 중…",
    done: "담당자에게 전달했어요. 확인 후 안내해 드릴게요.",
    fail: "담당자에게 전달하지 못했어요. 상담 종료를 눌러 다시 시도할 수 있어요.",
  },
};

async function createTicket(trigger) {
  const text = TICKET_TEXT[trigger];
  sendBtn.disabled = true;
  syncEndBtn();
  const note = addMessage("system", text.pending);
  const bubble = note.querySelector(".bubble");
  try {
    const res = await fetch("/api/tickets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: conversation }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${res.status}`);
    bubble.innerHTML =
      `${esc(text.done)} <a href="${esc(data.url)}" target="_blank" rel="noopener">${esc(data.cs_id)} 티켓 열기</a>` +
      `<br>${esc(data.summary)} · ${esc(data.category)} · ${esc(data.priority)}` +
      data.warnings.map((w) => `<br><span class="warn">${esc(w)}</span>`).join("");
    conversation = [];
  } catch (e) {
    bubble.textContent = `${text.fail} ${e.message}`;
  } finally {
    sendBtn.disabled = false;
    syncEndBtn();
    chat.scrollTop = chat.scrollHeight;
  }
}
endBtn.addEventListener("click", () => { if (!endBtn.disabled) createTicket("end"); });

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

// ---- 데이터 가이드 ----

const guide = $("#guide");
const guideState = { data: null, tab: "all", topic: null, q: "" };
const topicIndex = new Map();

async function openGuide() {
  guide.hidden = false;
  document.body.style.overflow = "hidden";
  if (!guideState.data) {
    try {
      guideState.data = await (await fetch("/api/guide")).json();
      for (const ds of guideState.data.datasets)
        for (const g of ds.groups) for (const t of g.topics) topicIndex.set(t.id, { t, ds, g });
    } catch {
      $("#guide-sub").textContent = "가이드를 불러오지 못했습니다";
      return;
    }
  }
  renderGuide();
  $("#guide-q").focus();
}

function closeGuide() {
  guide.hidden = true;
  document.body.style.overflow = "";
}

function topicMatches(t, ds, g, q) {
  if (!q) return true;
  const hay = [t.title, g.name, g.label, ds.label, ds.id, ...t.examples].join(" ").toLowerCase();
  return q.split(/\s+/).every((w) => hay.includes(w));
}

function renderGuide() {
  const { data, tab } = guideState;
  const q = guideState.q.trim().toLowerCase();
  const n = (v) => v.toLocaleString();
  $("#guide-sub").textContent =
    `공개 CS 데이터셋 ${data.total.datasets}개를 주제별로 정리한 위키 ${data.total.pages}페이지(원본 기록 ${n(data.total.records)}건)를 검색합니다. ` +
    "위키가 영어라서 한국어 질문은 영어 검색어로 바꿔 찾고, 맞는 주제가 없으면 상담원 연결로 안내합니다.";

  const tabs = [{ id: "all", label: "전체", meta: `${data.total.pages}주제 · ${n(data.total.records)}건`, desc: `데이터셋 ${data.total.datasets}개` }]
    .concat(data.datasets.map((d) => ({ id: d.id, label: `${d.id} · ${d.label}`, meta: `${d.pages}주제 · ${n(d.records)}건`, desc: `${d.desc} · ${d.license}` })));
  $("#guide-tabs").innerHTML = tabs
    .map((t) => `<button type="button" role="tab" class="guide-tab" data-tab="${esc(t.id)}" aria-selected="${t.id === tab}">
        <strong>${esc(t.label)}</strong><span>${esc(t.meta)}</span><span>${esc(t.desc)}</span></button>`)
    .join("");

  // 검색어가 선택한 데이터셋에 없으면 전체에서 찾아 보여 준다.
  const inTab = (d) => tab === "all" || d.id === tab;
  const hits = (d) => d.groups.some((g) => g.topics.some((t) => topicMatches(t, d, g, q)));
  const widened = q && tab !== "all" && !data.datasets.some((d) => inTab(d) && hits(d)) && data.datasets.some(hits);
  const sets = data.datasets.filter((d) => widened || inTab(d));
  const showAll = tab === "all" || widened;
  const visible = [];
  const html = sets
    .flatMap((ds) =>
      ds.groups.map((g) => {
        const topics = g.topics.filter((t) => topicMatches(t, ds, g, q));
        if (!topics.length) return "";
        visible.push(...topics.map((t) => t.id));
        const head = showAll ? `${ds.id} · ${g.label}` : `${g.label} · ${g.name}`;
        return `<div class="guide-group"><div class="guide-group-head">${esc(head)} <span>· ${topics.length}</span></div>
          <div class="guide-chips">${topics
            .map((t) => `<button type="button" class="guide-chip" data-topic="${esc(t.id)}" aria-pressed="${t.id === guideState.topic}">${esc(t.title)}</button>`)
            .join("")}</div></div>`;
      })
    )
    .join("");
  const note = widened ? `<p class="guide-empty">선택한 데이터셋에는 "${esc(guideState.q)}" 주제가 없어 전체에서 찾았어요.</p>` : "";
  $("#guide-groups").innerHTML = html
    ? note + html
    : `<p class="guide-empty">"${esc(guideState.q)}"에 맞는 주제가 없습니다. 이런 질문은 상담원 연결로 안내됩니다.</p>`;

  if (!visible.includes(guideState.topic)) guideState.topic = visible[0] || null;
  $("#guide-groups").querySelectorAll(".guide-chip").forEach((b) => b.setAttribute("aria-pressed", b.dataset.topic === guideState.topic));
  renderGuideDetail();
}

function renderGuideDetail() {
  const box = $("#guide-detail");
  const hit = topicIndex.get(guideState.topic);
  if (!hit) { box.innerHTML = `<p class="guide-empty">주제를 고르면 예시 질문과 대표 답변을 볼 수 있어요.</p>`; return; }
  const { t, ds, g } = hit;
  box.innerHTML = `
    <div class="guide-detail-head">
      <span class="muted">${esc(ds.id)} · ${esc(g.label)} (${esc(g.name)})</span>
      <strong>${esc(t.title)}</strong>
      <span class="muted">근거 기록 ${t.records.toLocaleString()}건 · <span class="path">${esc(t.path)}</span></span>
    </div>
    <div class="guide-detail-sec">
      <span class="label">이런 질문에 답할 수 있어요 (눌러서 바로 질문)</span>
      ${t.examples.map((e) => `<button type="button" class="guide-ask" data-ask="${esc(e)}">${esc(e)}</button>`).join("")}
    </div>
    <div class="guide-detail-sec">
      <span class="label">대표 답변 (앞부분)</span>
      <p class="guide-answer">${esc(t.answer) || "대표 답변 없음"}</p>
    </div>
    <details class="guide-body" data-body="${esc(t.id)}"><summary>위키 본문 전체 보기</summary><div class="md"></div></details>`;
}

$("#guide-btn").addEventListener("click", openGuide);
guide.addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) return closeGuide();
  const tab = e.target.closest("[data-tab]");
  if (tab) { guideState.tab = tab.dataset.tab; guideState.topic = null; return renderGuide(); }
  const chip = e.target.closest("[data-topic]");
  if (chip) { guideState.topic = chip.dataset.topic; return renderGuide(); }
  const askBtn = e.target.closest("[data-ask]");
  if (askBtn && !sendBtn.disabled) { closeGuide(); ask(askBtn.dataset.ask.replace(/…$/, "")); }
});
guide.addEventListener("toggle", async (e) => {
  const d = e.target.closest?.("details[data-body]");
  if (!d || !d.open || d.dataset.loaded) return;
  const box = d.querySelector(".md");
  box.textContent = "불러오는 중…";
  try {
    box.innerHTML = md((await (await fetch(`/api/wiki/${d.dataset.body}`)).json()).body);
    d.dataset.loaded = "1";
  } catch {
    box.textContent = "본문을 불러오지 못했습니다";
  }
}, true);
$("#guide-q").addEventListener("input", (e) => { guideState.q = e.target.value; renderGuide(); });

// ---- 첫 방문 사용법 안내 (브라우저당 한 번, 쿠키로 기억) ----

const tour = $("#tour");
const TOUR_COOKIE = "cs_qa_tour";
const TOUR_STEPS = [
  { sel: "#input", title: "질문 입력", text: "고객 문의를 입력하고 Enter로 보냅니다. 줄바꿈은 Shift+Enter예요." },
  { sel: "#guide-btn", title: "데이터 가이드", text: "어떤 데이터셋이 들어 있고 어떤 주제로 질문할 수 있는지 봅니다. 예시 질문을 누르면 바로 물어볼 수 있어요." },
  { sel: ".toggle", title: "비교 모드", text: "켜면 같은 질문을 판단 모델 경로와 바이패스 경로로 동시에 처리해 답변·비용·시간을 나란히 비교합니다." },
  { sel: "#status", title: "연결 상태", text: "판단 모델과 LLM이 어떤 구현체로 연결됐는지 보여 줍니다. 회색 점은 키가 설정되지 않은 상태예요." },
  { sel: "#chat", title: "답변 상세 보기", text: "답변 말풍선을 누르면 어떤 위키를 찾아 왜 골랐고 어떻게 답을 만들었는지, 단계별 비용과 시간을 볼 수 있어요." },
  { sel: "#tour-btn", title: "안내 다시 보기", text: "이 안내는 처음 접속할 때 한 번만 나와요. ? 버튼을 누르면 언제든 다시 볼 수 있습니다." },
];
let tourIdx = 0;

const tourSeen = () => document.cookie.split("; ").some((c) => c === `${TOUR_COOKIE}=done`);

function startTour() {
  tourIdx = 0;
  tour.hidden = false;
  showTourStep();
  $("#tour-next").focus();
}

function endTour() {
  tour.hidden = true;
  document.cookie = `${TOUR_COOKIE}=done; max-age=${60 * 60 * 24 * 365}; path=/; SameSite=Lax`;
  input.focus();
}

function showTourStep() {
  const step = TOUR_STEPS[tourIdx];
  const target = $(step.sel);
  if (!target) return endTour();
  const r = target.getBoundingClientRect();
  const pad = 6;
  Object.assign($("#tour-spot").style, {
    left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px`,
  });
  $("#tour-step").textContent = `${tourIdx + 1} / ${TOUR_STEPS.length}`;
  $("#tour-title").textContent = step.title;
  $("#tour-text").textContent = step.text;
  $("#tour-prev").hidden = tourIdx === 0;
  $("#tour-next").textContent = tourIdx === TOUR_STEPS.length - 1 ? "시작하기" : "다음";

  // 카드는 대상 아래에, 공간이 없으면 위에 둔다. 대상이 화면 대부분이면 가운데.
  const card = $("#tour-card");
  const cw = card.offsetWidth, ch = card.offsetHeight, gap = 12, vw = innerWidth, vh = innerHeight;
  let top = r.bottom + pad + gap;
  if (top + ch > vh - 8) top = r.top - pad - gap - ch;
  if (top < 8) top = (vh - ch) / 2;
  const left = Math.min(Math.max(8, r.left + r.width / 2 - cw / 2), vw - cw - 8);
  Object.assign(card.style, { top: `${top}px`, left: `${left}px` });
}

$("#tour-next").addEventListener("click", () => (tourIdx < TOUR_STEPS.length - 1 ? (tourIdx++, showTourStep()) : endTour()));
$("#tour-prev").addEventListener("click", () => { if (tourIdx > 0) { tourIdx--; showTourStep(); } });
$("#tour-skip").addEventListener("click", endTour);
$("#tour-btn").addEventListener("click", startTour);
addEventListener("resize", () => { if (!tour.hidden) showTourStep(); });

loadStatus();
input.focus();
if (!tourSeen()) setTimeout(startTour, 400);
