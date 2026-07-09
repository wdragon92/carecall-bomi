/* 돌봄콜 AI 프론트엔드 (frontend §11). 순수 JS, 프레임워크 없음. */
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  sessionId: null,
  ws: null,
  voiceOn: true,
  seenFindings: new Set(),
  audio: null,
  _audioDone: null, // 현재 재생의 완료 resolver (인터럽트 시 호출)
  recording: false,
  reconnect: 0,
};

/* ================= 초기화 ================= */
async function init() {
  wireUI();
  try {
    const r = await fetch("/api/sessions", { method: "POST" });
    const d = await r.json();
    state.sessionId = d.session_id;
    connectWS();
  } catch (e) {
    setBadge("연결 실패 — 새로고침 해주세요");
  }
}

// WS 재접속 백오프 상수 — handshake만 성공하고 서버가 곧장 close해도 무한 재접속에 빠지지 않게.
const RECONNECT_MAX = 6;       // 최대 재시도 횟수(초과 시 새로고침 안내)
const RECONNECT_BASE = 1500;   // 지수 백오프 기준(ms)
const RECONNECT_CAP = 15000;   // 백오프 상한(ms)
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${state.sessionId}`);
  state.ws = ws;
  // ⚠️ onopen(handshake 성공)만으로 카운터를 리셋하면, 서버가 handshake 직후 close할 때
  //    카운터가 계속 0으로 되돌아가 무한 재접속에 빠진다. 실제 메시지를 한 번이라도 받아
  //    '정상 통신'이 확인된 뒤에만(onmessage) 리셋한다.
  ws.onopen = () => { setBadge("보미가 곁에 있어요"); }; // session_ready가 곧 상세 배지로 갱신
  ws.onmessage = (ev) => {
    state.reconnect = 0; // 정상 통신 확인 — 이후 끊기면 백오프를 처음부터 다시
    try { handleWS(JSON.parse(ev.data)); } catch (e) {}
  };
  ws.onclose = () => {
    if (state.reconnect < RECONNECT_MAX) {
      const delay = Math.min(RECONNECT_CAP, Math.round(RECONNECT_BASE * Math.pow(1.5, state.reconnect)));
      state.reconnect++;
      setBadge("연결이 잠시 끊겼어요. 다시 연결 중…");
      setTimeout(connectWS, delay);
    } else {
      setBadge("연결이 불안정해요. 새로고침 해주세요.");
      toast("연결이 끊겼어요. 화면을 아래로 당겨 새로고침 해주세요.");
    }
  };
  ws.onerror = () => {};
}

/* ================= WS 메시지 처리 ================= */
function handleWS(m) {
  switch (m.type) {
    case "session_ready":
      showModes(m.providers);
      break;
    case "ai_typing":
      m.on ? showTyping() : hideTyping();
      break;
    case "ai_turn":
      revealTurn(m.bubbles || []);
      break;
    case "rag_status":
      ragStatus(m);
      break;
    case "findings_update":
      renderFindings(m.findings || []);
      break;
    case "welfare_update":
      renderWelfare(m.items);
      break;
    case "urgent_alert":
      showUrgent(m.message, m.level);
      break;
    case "ocr_status":
      ocrStatus(m);
      break;
    case "error":
      toast(m.message || "일시적인 오류가 있었어요.");
      break;
  }
}

/* ================= 채팅 렌더 ================= */
function chatLog() {
  return $("#chat-log");
}
function scrollDown() {
  const el = chatLog();
  el.scrollTop = el.scrollHeight;
}
function rowFor(side) {
  const row = document.createElement("div");
  row.className = "flex " + (side === "user" ? "justify-end" : "justify-start");
  return row;
}
function appendUserBubble(text) {
  const row = rowFor("user");
  row.innerHTML = `<div class="bubble bubble-user"></div>`;
  row.querySelector(".bubble").textContent = text;
  chatLog().appendChild(row);
  scrollDown();
  return row;
}
function appendImageBubble(dataUrl) {
  const row = rowFor("user");
  const div = document.createElement("div");
  div.className = "bubble bubble-user";
  div.innerHTML = `<img alt="첨부 사진" style="max-width:220px;border-radius:10px;display:block" />`;
  div.querySelector("img").src = dataUrl;
  row.appendChild(div);
  chatLog().appendChild(row);
  scrollDown();
}
function appendSystemNote(text) {
  const row = document.createElement("div");
  row.className = "text-center text-sm text-gray-400 my-1";
  row.textContent = text;
  chatLog().appendChild(row);
  scrollDown();
  return row;
}
function appendAiBubble(text, kind, card) {
  const row = rowFor("ai");
  const div = document.createElement("div");
  div.className = "bubble bubble-ai" + (kind === "card" ? " bubble-card" : "");
  if (kind === "card" && card && card.title) {
    div.appendChild(buildRagCard(card)); // 구조화 카드 (RAG 근거 가시화)
  } else if (kind === "card") {
    appendWithBokjiroLinks(div, text); // 텍스트 카드(신청 패키지 등) — 복지로 URL만 링크화
  } else {
    div.textContent = text;
  }
  row.appendChild(div);
  chatLog().appendChild(row);
  scrollDown();
}

/* 텍스트 카드(신청 패키지 등): 복지로 URL을 본문에서 걷어내고, 구조화 RAG 카드와
   똑같이 카드 '하단 버튼' 한 개로 붙인다 (링크 위치·모양 일관화 — 전 카드 공통).
   OCR 문서에서 읽힌 일반 URL(스미싱 링크 등)은 절대 링크화하지 않는다. */
function appendWithBokjiroLinks(el, text) {
  const re = /https?:\/\/(?:www\.)?bokjiro\.go\.kr[^\s]*/g;
  const urls = text.match(re) || [];
  let shown = text;
  for (const u of urls) {
    shown = shown
      .split("\n")
      .map((line) => {
        if (!line.includes(u)) return line;
        const rest = line.replace(u, "").trim();
        // "· 온라인:" / "· 복지로:"처럼 라벨만 남으면 줄 자체를 지운다
        return /^[·\-•\s]*(온라인|복지로)?\s*:?\s*$/.test(rest) ? null : rest;
      })
      .filter((l) => l !== null)
      .join("\n");
  }
  el.appendChild(document.createTextNode(shown));
  if (urls.length) {
    const a = document.createElement("a");
    a.className = "rag-link";
    a.href = urls[0];
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "복지로에서 자세히 보기 →";
    el.appendChild(document.createElement("br"));
    el.appendChild(a);
  }
}

/* RAG 정보 카드 DOM — 전부 textContent 기반 (주입 안전) */
function buildRagCard(c) {
  const box = document.createElement("div");
  const title = document.createElement("div");
  title.className = "rag-card-title";
  title.textContent = `📌 ${c.title}`;
  box.appendChild(title);

  const badge = document.createElement("div");
  badge.className = "rag-card-badge";
  badge.textContent = `📚 복지로 공식자료 · ${c["기준일"] || ""} 기준` + (c.live ? " · 방금 확인" : "");
  box.appendChild(badge);

  for (const key of ["지역", "대상", "지원", "신청", "문의"]) {
    const v = (c[key] || "").trim();
    if (!v) continue;
    const rowEl = document.createElement("div");
    rowEl.className = "rag-row";
    const b = document.createElement("b");
    b.textContent = key;
    const span = document.createElement("span");
    span.textContent = v;
    rowEl.appendChild(b);
    rowEl.appendChild(span);
    box.appendChild(rowEl);
  }
  if (c.url && /^https?:\/\//.test(c.url)) {
    const a = document.createElement("a");
    a.className = "rag-link";
    a.href = c.url;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = "복지로에서 자세히 보기 →";
    box.appendChild(a);
  }
  return box;
}

/* RAG 검색 상태 칩 — "찾는 중" → "N건 찾음" */
let _chipTimer = null;
function ragStatus(m) {
  const wrap = $("#rag-chip-wrap");
  const chip = $("#rag-chip");
  clearTimeout(_chipTimer);
  if (m.status === "searching") {
    chip.textContent = "📖 복지 자료를 찾아보는 중…";
    wrap.classList.remove("hidden");
  } else if (m.status === "found") {
    chip.textContent = `📚 공식 복지자료 ${Number(m.hits) || 0}건에서 근거를 찾았어요`;
    wrap.classList.remove("hidden");
    _chipTimer = setTimeout(() => wrap.classList.add("hidden"), 10000); // 어르신이 읽을 시간
  } else {
    wrap.classList.add("hidden");
  }
}

/* OCR 진행 상태 — 같은 칩 자리 재사용 (사진 턴엔 RAG 검색이 없어 충돌 없음) */
function ocrStatus(m) {
  const wrap = $("#rag-chip-wrap");
  const chip = $("#rag-chip");
  clearTimeout(_chipTimer);
  if (m.status === "processing") {
    chip.textContent = "📷 사진에서 글자를 읽는 중…";
    wrap.classList.remove("hidden");
  } else if (m.status === "done") {
    chip.textContent = "📷 사진을 다 읽었어요";
    wrap.classList.remove("hidden");
    _chipTimer = setTimeout(() => wrap.classList.add("hidden"), 2500);
  } else {
    wrap.classList.add("hidden");
    toast("사진에서 글자를 읽지 못했어요.");
  }
}
function showTyping() {
  if (document.getElementById("typing-row")) return;
  const row = rowFor("ai");
  row.id = "typing-row";
  row.innerHTML = `<div class="bubble bubble-ai"><span class="typing-dots"><i></i><i></i><i></i></span></div>`;
  chatLog().appendChild(row);
  scrollDown();
}
function hideTyping() {
  const r = document.getElementById("typing-row");
  if (r) r.remove();
}

/* ================= 특이사항 / 복지 패널 ================= */
const CAT_ICON = { 건강: "🩺", 정서: "💗", 인지: "🧩", 사기_노출: "⚠️", 복지_니즈: "🤝", 긴급: "🚨" };
const SEV_RANK = { 높음: 3, 보통: 2, 낮음: 1 };
// 표시 라벨 — 내부 값(낮음/보통/높음)은 그대로, 보호자에게 와닿는 말로만 변환
const SEV_LABEL = { 높음: "위험", 보통: "주의", 낮음: "참고" };
const sevLabel = (s) => SEV_LABEL[s] || s;
/* 특이사항: 항목별 팝업 대신 카테고리로 묶어 전체를 한눈에, 조용히 갱신 */
function renderFindings(findings) {
  const box = $("#tab-findings");
  box.innerHTML = "";
  if (!findings.length) {
    box.innerHTML = `<p class="text-gray-400 text-center my-6 text-sm">대화가 진행되면<br>특이사항이 여기에 정리됩니다.</p>`;
    return;
  }
  let newUrgent = false;
  const groups = new Map();
  for (const f of findings) {
    if (!state.seenFindings.has(f.id)) {
      state.seenFindings.add(f.id);
      if (f.severity === "높음" || f.category === "긴급") newUrgent = true;
    }
    if (!groups.has(f.category)) groups.set(f.category, []);
    groups.get(f.category).push(f);
  }
  const ordered = [...groups.entries()].sort((a, b) => {
    const w = (items) => Math.max(...items.map((f) => SEV_RANK[f.severity] || 0));
    return w(b[1]) - w(a[1]);
  });
  for (const [cat, items] of ordered) {
    const worst = items.reduce((a, b) => (SEV_RANK[b.severity] > SEV_RANK[a.severity] ? b : a));
    const anyHuman = items.some((f) => f.needs_human);
    const el = document.createElement("div");
    el.className = `card sev-${worst.severity}`;
    el.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="font-semibold">${CAT_ICON[cat] || "•"} ${escapeHtml(cat.replace("_", " "))} <span class="text-xs text-gray-400">${items.length}건</span></span>
        <span class="text-xs px-2 py-0.5 rounded-full ${sevChip(worst.severity)}">${escapeHtml(sevLabel(worst.severity))}</span>
      </div>
      <ul class="fcat-items"></ul>
      ${anyHuman ? '<div class="mt-1 text-xs font-semibold text-red-600">👤 보호자·담당자 연결 권고</div>' : ""}`;
    const ul = el.querySelector(".fcat-items");
    for (const f of items) {
      const li = document.createElement("li");
      li.textContent = f.content;
      ul.appendChild(li);
    }
    box.appendChild(el);
  }
  if (newUrgent) flashMobileBadge(); // 높음/긴급일 때만 알림 (조용한 갱신)
}
function sevChip(s) {
  return s === "높음" ? "bg-red-100 text-red-700" : s === "보통" ? "bg-amber-100 text-amber-700" : "bg-green-100 text-green-700";
}
function renderWelfare(items) {
  const box = $("#tab-welfare");
  box.innerHTML = "";
  if (!items || !items.length) {
    box.innerHTML = `<p class="text-gray-400 text-center mt-8 text-sm">자격이 맞는 복지가<br>여기에 안내됩니다.</p>`;
    return;
  }
  for (const it of items) {
    const el = document.createElement("div");
    el.className = "card newcard";
    const link = it.url && /^https?:\/\//.test(it.url)
      ? `<a class="rag-link" href="${escapeHtml(it.url)}" target="_blank" rel="noopener">복지로에서 자세히 보기 →</a>`
      : "";
    el.innerHTML = `<div class="font-semibold text-blue-700">🤝 ${escapeHtml(it["이름"])}</div>
      <div class="mt-1 text-sm text-gray-700">${escapeHtml(it["한줄"] || "")}</div>
      <div class="mt-1 text-xs text-gray-500">신청: ${escapeHtml(it["신청처"] || "복지로(129)·주민센터")}</div>
      ${it["기준일"] ? `<div class="mt-0.5 text-xs text-gray-400">정보 기준일 ${escapeHtml(it["기준일"])}</div>` : ""}${link}`;
    box.appendChild(el);
  }
  flashMobileBadge();
}
function showUrgent(msg, level) {
  const b = $("#urgent-banner");
  level = level === "warning" ? "warning" : "emergency";
  // 응급(빨강)이 이미 떠 있으면 경고(주황)로 낮추지 않음
  if (b.dataset.level === "emergency" && level !== "emergency") return;
  b.dataset.level = level;
  b.classList.remove("hidden", "bg-red-600", "bg-amber-500");
  b.classList.add(level === "warning" ? "bg-amber-500" : "bg-red-600");
  b.textContent = (level === "warning" ? "⚠️ " : "🚨 ") + msg;
}

/* ================= 입력: 텍스트 ================= */
function sendUserMessage(text, via = "text", opts = {}) {
  text = (text || "").trim();
  if (!text) return false;
  if (!state.ws || state.ws.readyState !== 1) {
    // 연결 안 된 상태에서 조용히 버리면 어르신은 보냈다고 착각 — 알리고 입력은 보존
    toast("아직 연결 중이에요. 잠시 후 다시 보내주세요.");
    return false;
  }
  _turn++;      // 진행 중이던 AI 말풍선 노출 중단
  stopAudio();  // 재생 중이던 음성 중지
  if (!opts.noBubble) appendUserBubble(text);
  state.ws.send(JSON.stringify({ type: "user_message", text, via }));
  return true;
}
function sendFromInput() {
  const inp = $("#text-input");
  const t = inp.value.trim();
  if (!t) return;
  if (sendUserMessage(t, "text")) {
    inp.value = "";
    inp.style.height = "auto";
  }
}

/* ================= 입력: 마이크 (클릭 토글 — 눌러 시작, 다시 눌러 전송) ================= */
let _stream, _ctx, _proc, _src, _mute, _chunks, _recTimer, _recog, _liveRow;

/* 실시간 인식 미리보기 (브라우저 Web Speech) — 표시용일 뿐, 최종 확정은 CLOVA CSR.
   미지원 브라우저(사파리 일부/파이어폭스)는 조용히 기존 방식으로 동작. */
function startLivePreview() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return;
  try {
    _recog = new SR();
    _recog.lang = "ko-KR";
    _recog.interimResults = true;
    _recog.continuous = true;
    _liveRow = appendUserBubble("🎤 듣고 있어요…");
    _liveRow.querySelector(".bubble").classList.add("bubble-live");
    _recog.onresult = (e) => {
      let txt = "";
      for (const res of e.results) txt += res[0].transcript;
      txt = txt.trim();
      if (txt && _liveRow) {
        _liveRow.querySelector(".bubble").textContent = "🎤 " + txt;
        scrollDown();
      }
    };
    _recog.onerror = () => {};
    _recog.start();
  } catch (e) {
    _recog = null;
  }
}
function stopLivePreview() {
  try { _recog && _recog.stop(); } catch (e) {}
  _recog = null;
}
async function toggleRec() {
  if (state.recPending || state.recStopping) return; // 권한 프롬프트/정지 꼬리 대기 중 중복 클릭 무시
  if (state.recording) await stopRec();
  else await startRec();
}
async function startRec() {
  if (state.recording) return;
  state.recPending = true;
  try {
    _stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch (e) {
    state.recPending = false;
    toast("마이크 사용 권한이 필요해요. 주소창 옆 🔒에서 마이크를 허용해 주세요.");
    return;
  }
  state.recPending = false;
  state.recording = true;
  stopAudio(); // 재생 중이던 TTS 중지(녹음에 안 섞이게 + 노출 페이싱 해제)
  $("#mic-hint").classList.remove("hidden");
  const btn = $("#btn-mic");
  btn.classList.add("btn-rec"); // !important 클래스 — Tailwind 순서 경합 없이 확실한 녹음 표시
  btn.setAttribute("aria-pressed", "true");
  // CSR 목표 샘플레이트(16k)로 컨텍스트를 직접 요청 — 브라우저가 고품질 리샘플링을 해준다.
  // (미지원 브라우저는 기본 레이트로 열리고, 아래 downsample 폴백이 처리)
  const AC = window.AudioContext || window.webkitAudioContext;
  try {
    _ctx = new AC({ sampleRate: 16000 });
  } catch (e) {
    _ctx = new AC();
  }
  try { await _ctx.resume(); } catch (e) {}
  try {
    _src = _ctx.createMediaStreamSource(_stream);
  } catch (e) {
    // 일부 브라우저는 16k 컨텍스트에 마이크 스트림 연결을 거부 → 기본 레이트로 재시도
    try { _ctx.close(); } catch (e2) {}
    _ctx = new AC();
    try { await _ctx.resume(); } catch (e2) {}
    _src = _ctx.createMediaStreamSource(_stream);
  }
  _proc = _ctx.createScriptProcessor(4096, 1, 1);
  _chunks = [];
  _proc.onaudioprocess = (e) => _chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  _src.connect(_proc);
  // ⚠️ destination에 직결하면 녹음 중 마이크가 스피커로 새어 하울링·에코가 녹음에 섞인다
  // (실시간 미리보기는 멀쩡한데 전송본만 달라지던 주범). 무음 게인으로 그래프만 구동.
  _mute = _ctx.createGain();
  _mute.gain.value = 0;
  _proc.connect(_mute);
  _mute.connect(_ctx.destination);
  startLivePreview(); // 말하는 동안 실시간 자막 (지원 브라우저만)
  _recTimer = setTimeout(() => { if (state.recording) stopRec(); }, 30000); // 최대 30초(CSR 60초 제한 대비)
}
async function stopRec() {
  if (!state.recording || state.recStopping) return;
  state.recording = false;
  state.recStopping = true; // 220ms 꼬리 대기 동안 새 녹음 시작을 막아 전역 오디오 그래프를 보호
  clearTimeout(_recTimer);
  $("#mic-hint").classList.add("hidden");
  const btn = $("#btn-mic");
  btn.classList.remove("btn-rec");
  btn.setAttribute("aria-pressed", "false");
  await _sleep(220); // 마지막 어절이 프로세서 버퍼에 남아 잘리지 않게 꼬리를 받는다
  try {
    _proc.disconnect();
    _src.disconnect();
    if (_mute) _mute.disconnect();
    _stream.getTracks().forEach((t) => t.stop());
  } catch (e) {}
  stopLivePreview();
  const sr = _ctx.sampleRate;
  const flat = flatten(_chunks);
  try { await _ctx.close(); } catch (e) {}
  state.recStopping = false; // 오디오 그래프 정리 완료 — 이제 새 녹음을 시작해도 안전(이후 fetch까지 동기 실행)
  // 실시간 미리보기 말풍선이 있으면 그대로 이어받아 확정 단계 표시
  const note = _liveRow || appendUserBubble("🎤 음성 인식 중…");
  _liveRow = null;
  const noteBubble = note.querySelector(".bubble");
  if (flat.length < sr * 0.3) {
    note.remove();
    toast("말씀이 너무 짧았어요. 🎤을 누르고 말씀하신 뒤 다시 눌러 주세요.");
    return;
  }
  if (noteBubble.classList.contains("bubble-live") && noteBubble.textContent === "🎤 듣고 있어요…") {
    noteBubble.textContent = "🎤 음성 인식 중…";
  }
  const wav = encodeWAV(normalizePeak(downsample(flat, sr, 16000)), 16000);
  const fd = new FormData();
  fd.append("file", new Blob([wav], { type: "audio/wav" }), "rec.wav");
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/audio`, { method: "POST", body: fd });
    const d = await r.json();
    const text = (d.text || "").trim();
    noteBubble.classList.remove("bubble-live");
    if (text) {
      noteBubble.textContent = text; // 최종 확정 = CLOVA CSR 결과
      sendUserMessage(text, "voice", { noBubble: true });
    } else {
      noteBubble.textContent = "(음성을 알아듣지 못했어요)";
    }
  } catch (e) {
    noteBubble.classList.remove("bubble-live");
    noteBubble.textContent = "(음성 전송에 실패했어요)";
  }
}
function flatten(chunks) {
  let len = 0;
  chunks.forEach((c) => (len += c.length));
  const out = new Float32Array(len);
  let o = 0;
  chunks.forEach((c) => { out.set(c, o); o += c.length; });
  return out;
}
function downsample(buf, from, to) {
  if (to >= from) return buf;
  const ratio = from / to;
  const outLen = Math.floor(buf.length / ratio);
  const out = new Float32Array(outLen);
  // 구간 평균(박스 필터) 후 추출 — 점추출은 앨리어싱으로 자음이 뭉개져 CSR 정확도를 깎는다
  for (let i = 0; i < outLen; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(buf.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
    let sum = 0;
    for (let j = start; j < end; j++) sum += buf[j];
    out[i] = sum / (end - start);
  }
  return out;
}
function normalizePeak(buf, target = 0.9) {
  let peak = 0;
  for (let i = 0; i < buf.length; i++) {
    const a = Math.abs(buf[i]);
    if (a > peak) peak = a;
  }
  if (peak < 0.02 || peak >= target) return buf; // 사실상 무음이거나 이미 충분한 레벨
  const g = target / peak;
  for (let i = 0; i < buf.length; i++) buf[i] *= g;
  return buf;
}
function encodeWAV(samples, rate) {
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); v.setUint32(4, 36 + samples.length * 2, true); w(8, "WAVE");
  w(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  w(36, "data"); v.setUint32(40, samples.length * 2, true);
  let o = 44;
  for (let i = 0; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buf;
}

/* ================= 입력: 사진 첨부 ================= */
async function handleFile(file) {
  if (!file) return;
  let dataUrl;
  try {
    dataUrl = await downscaleImage(file, 1600, 0.85);
  } catch (e) {
    toast("사진을 불러오지 못했어요.");
    return;
  }
  appendImageBubble(dataUrl);
  const note = appendSystemNote("사진을 확인하고 있어요…");
  try {
    const blob = await (await fetch(dataUrl)).blob();
    const fd = new FormData();
    fd.append("file", blob, file.name || "photo.jpg");
    const r = await fetch(`/api/sessions/${state.sessionId}/image`, { method: "POST", body: fd });
    if (!r.ok) throw new Error("upload");
    note.textContent = "사진을 확인했어요.";
  } catch (e) {
    note.textContent = "사진 전송에 실패했어요.";
  }
}
function downscaleImage(file, maxDim, quality) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      let { width, height } = img;
      const scale = Math.min(1, maxDim / Math.max(width, height));
      width = Math.round(width * scale);
      height = Math.round(height * scale);
      const c = document.createElement("canvas");
      c.width = width; c.height = height;
      c.getContext("2d").drawImage(img, 0, 0, width, height);
      resolve(c.toDataURL("image/jpeg", quality));
    };
    const url = URL.createObjectURL(file);
    const done = img.onload; // 위에서 정의한 리사이즈+resolve 핸들러
    img.onload = () => { try { URL.revokeObjectURL(url); } catch (e) {} done(); };
    img.onerror = () => { try { URL.revokeObjectURL(url); } catch (e) {} reject(new Error("image load")); };
    img.src = url;
  });
}

/* ============ 말풍선 노출 페이싱: TTS 재생이 끝나면 다음 말풍선을 띄운다 ============ */
let _turn = 0; // 현재 턴 번호(새 턴/사용자 입력 시 증가 → 진행 중 노출 중단)
const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function _readingDelay(text) {
  return Math.min(4500, 500 + text.length * 95); // 음성 off일 때 읽는 속도에 맞춘 간격
}
function stopAudio() {
  if (state.audio) { try { state.audio.pause(); } catch (e) {} }
  if (state._audioDone) { const done = state._audioDone; state._audioDone = null; done(); }
}
async function fetchTTS(id) {
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: id }),
    });
    return r.ok ? await r.blob() : null;
  } catch (e) {
    return null;
  }
}
function playBlob(blob) {
  // resolve(true)=정상 재생(또는 사용자 인터럽트), resolve(false)=재생 자체가 실패
  // (모바일 자동재생 차단 등) → 호출부가 읽기 딜레이로 폴백해 말풍선이 와르르 쏟아지지 않게.
  return new Promise((resolve) => {
    stopAudio();
    let url;
    try {
      url = URL.createObjectURL(blob);
      const a = new Audio(url);
      state.audio = a;
      state._audioDone = (played = true) => {
        state._audioDone = null;
        try { URL.revokeObjectURL(url); } catch (e) {}
        resolve(played);
      };
      a.onended = () => state._audioDone && state._audioDone(true);
      a.onerror = () => state._audioDone && state._audioDone(false);
      a.play().catch(() => state._audioDone && state._audioDone(false));
    } catch (e) {
      if (url) { try { URL.revokeObjectURL(url); } catch (e2) {} }
      resolve(false);
    }
  });
}
async function revealTurn(bubbles) {
  const myTurn = ++_turn;
  stopAudio();
  state._retryBlob = null; // 새 턴이 시작되면 이전 재생 실패분은 폐기
  hideTyping();
  const useVoice = state.voiceOn && !state.recording;
  // 음성이면 이번 턴 말풍선들의 TTS를 미리 병렬 요청(말풍선 사이 끊김 방지)
  const blobs = useVoice ? bubbles.map((b) => fetchTTS(b.id)) : [];
  for (let i = 0; i < bubbles.length; i++) {
    if (myTurn !== _turn) return; // 새 턴/입력으로 중단
    const b = bubbles[i] || {};
    const btext = typeof b.text === "string" ? b.text : ""; // text 없는 구조화 카드 방어(예외로 턴 무음 중단 방지)
    showTyping();
    await _sleep(Math.min(900, 280 + btext.length * 11)); // '입력 중' 짧은 뜸
    if (myTurn !== _turn) { hideTyping(); return; }
    hideTyping();
    appendAiBubble(btext, b.kind, b.card);
    if (useVoice && state.voiceOn && !state.recording) {
      const blob = await blobs[i];
      if (myTurn !== _turn) return;
      // 오디오가 끝나야 다음 말풍선 등장 → 음성 페이싱. 재생 실패(자동재생 차단)면
      // 읽기 딜레이로 폴백하되, 블롭을 보관해 첫 사용자 터치 때 다시 들려준다(첫 인사 무음 방지)
      const played = blob ? await playBlob(blob) : false;
      if (myTurn !== _turn) return;
      if (!played) {
        if (blob) state._retryBlob = blob;
        await _sleep(_readingDelay(btext));
      }
    } else {
      await _sleep(_readingDelay(btext));
    }
  }
}

/* ================= 종료 리포트 ================= */
async function endSession() {
  $("#btn-end").disabled = true;
  setBadge("오늘 이야기를 정리하고 있어요…");
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/end`, { method: "POST" });
    const d = await r.json();
    renderReport(d.report);
  } catch (e) {
    toast("리포트를 만들지 못했어요.");
  } finally {
    $("#btn-end").disabled = false;
    setBadge("보미가 곁에 있어요");
  }
}
function renderReport(rep) {
  const b = $("#report-body");
  const findings = (rep.findings || [])
    .map((f) => `<li class="mb-1"><b>${escapeHtml(f.category.replace("_", " "))}</b> (${sevLabel(f.severity)}) — ${escapeHtml(f.content)}${f.needs_human ? " 👤" : ""}</li>`)
    .join("");
  const recs = (rep.recommendations || []).map((r) => `<li class="mb-1">✅ ${escapeHtml(r)}</li>`).join("");
  const welfare = (rep.welfare || []).map((w) => `<li class="mb-1">🤝 ${escapeHtml(w["이름"])} — <span class="text-gray-500 text-sm">${escapeHtml(w["신청처"] || "")}${w["기준일"] ? " · " + escapeHtml(w["기준일"]) + " 기준" : ""}</span></li>`).join("");
  const pkgs = (rep.apply_packages || []).map((p) => {
    const docs = Array.isArray(p["필요서류"]) ? p["필요서류"].join(", ") : String(p["필요서류"] || "");
    return `<li class="mb-1">📝 ${escapeHtml(p["서비스명"])} — ${escapeHtml(docs)} <span class="text-gray-500 text-sm">(${escapeHtml(p["신청처"] || "")})</span></li>`;
  }).join("");
  b.innerHTML = `
    <div><h3 class="font-bold mb-1">요약</h3><p class="text-gray-700">${escapeHtml(rep.summary || "")}</p></div>
    <div><h3 class="font-bold mb-1">관찰된 특이사항</h3><ul class="list-none">${findings || '<li class="text-gray-400">없음</li>'}</ul></div>
    <div><h3 class="font-bold mb-1">후속 권고</h3><ul class="list-none">${recs || '<li class="text-gray-400">없음</li>'}</ul></div>
    ${welfare ? `<div><h3 class="font-bold mb-1">안내한 복지</h3><ul class="list-none">${welfare}</ul></div>` : ""}
    ${pkgs ? `<div><h3 class="font-bold mb-1">신청 준비물</h3><ul class="list-none">${pkgs}</ul></div>` : ""}
    <p class="text-xs text-gray-400">${escapeHtml(rep.disclaimer || "")}</p>`;
  $("#report-modal").classList.remove("hidden");
}

/* ================= UI 배선 ================= */
function wireUI() {
  $("#btn-send").addEventListener("click", sendFromInput);
  const inp = $("#text-input");
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFromInput(); }
  });
  inp.addEventListener("input", () => { inp.style.height = "auto"; inp.style.height = Math.min(120, inp.scrollHeight) + "px"; });

  $("#btn-mic").addEventListener("click", toggleRec);

  $("#btn-attach").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (e) => { handleFile(e.target.files[0]); e.target.value = ""; });

  $("#btn-voice").addEventListener("click", toggleVoice);
  $("#btn-voice").classList.toggle("btn-on", state.voiceOn); // 초기 '켜짐' 상태도 시각 표시
  $("#btn-end").addEventListener("click", endSession);
  const closeReport = () => $("#report-modal").classList.add("hidden");
  $("#btn-close-report").addEventListener("click", closeReport);
  $("#btn-close-report-x").addEventListener("click", closeReport);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeReport(); });

  $$(".mview-btn").forEach((btn) => btn.addEventListener("click", () => switchMobile(btn.dataset.mview)));

  // 모바일 가상 키보드: 입력 포커스·뷰포트 변화 시 마지막 말풍선이 가려지지 않게
  inp.addEventListener("focus", () => setTimeout(scrollDown, 150));
  if (window.visualViewport) window.visualViewport.addEventListener("resize", () => scrollDown());

  // 모바일 자동재생 언락: 사용자 제스처마다 확인. {once:true}면 인사 도착(=_retryBlob 설정) 전에
  // 화면을 한 번 탭하는 순간 리스너가 사라져 이후 인사 TTS가 영구 무음 → once 제거하고 계속 바인딩한다.
  let _audioUnlocked = false;
  document.addEventListener("pointerdown", () => {
    if (!_audioUnlocked) {
      try {
        const a = new Audio("data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQAAAAA=");
        a.play().then(() => { _audioUnlocked = true; }).catch(() => {});
      } catch (e) {}
    }
    if (state._retryBlob && state.voiceOn && !state.recording) {
      const b = state._retryBlob;
      state._retryBlob = null;
      playBlob(b); // 첫 제스처가 전송/마이크면 해당 핸들러의 stopAudio가 곧바로 정리
    }
  });
}
function toggleVoice() {
  state.voiceOn = !state.voiceOn;
  const btn = $("#btn-voice");
  btn.setAttribute("aria-pressed", String(state.voiceOn));
  btn.textContent = state.voiceOn ? "🔊 음성" : "🔇 음성";
  btn.classList.toggle("btn-on", state.voiceOn);
  if (!state.voiceOn) stopAudio();
}
function switchMobile(view) {
  $("#chat-pane").classList.toggle("hidden", view !== "chat");
  $("#panel-pane").classList.toggle("hidden", view !== "panel");
  $("#panel-pane").classList.toggle("flex", view === "panel");
  $$(".mview-btn").forEach((b) => {
    const on = b.dataset.mview === view;
    b.classList.toggle("text-blue-600", on);
    b.classList.toggle("text-gray-500", !on);
  });
  if (view === "panel") $("#mobile-badge").classList.add("hidden");
}
function flashMobileBadge() {
  if ($("#panel-pane").classList.contains("hidden")) $("#mobile-badge").classList.remove("hidden");
}

/* ================= 유틸 ================= */
function setBadge(t) { $("#mode-badge").textContent = t; }
function showModes(p) {
  if (!p) return setBadge("보미가 곁에 있어요");
  const allReal = Object.values(p).every((v) => v === "real");
  setBadge(allReal ? "보미가 듣고 있어요 (실시간 연동)" : "보미가 듣고 있어요 (일부 데모)");
  $("#mode-badge").title = `LLM:${p.llm} STT:${p.stt} TTS:${p.tts} OCR:${p.ocr} EMB:${p.embed || "-"}`;
}
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg) {
  const t = document.createElement("div");
  t.className = "fixed bottom-24 left-1/2 -translate-x-1/2 bg-gray-900 text-white px-4 py-2 rounded-full text-sm z-50";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

init();
