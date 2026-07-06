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

function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${state.sessionId}`);
  state.ws = ws;
  ws.onopen = () => (state.reconnect = 0);
  ws.onmessage = (ev) => { try { handleWS(JSON.parse(ev.data)); } catch (e) {} };
  ws.onclose = () => {
    if (state.reconnect < 3) {
      state.reconnect++;
      setBadge("연결이 잠시 끊겼어요. 다시 연결 중…");
      setTimeout(connectWS, 1500);
    } else {
      setBadge("연결이 불안정해요. 새로고침 해주세요.");
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
      renderFindings(m.findings);
      break;
    case "welfare_update":
      renderWelfare(m.items);
      break;
    case "urgent_alert":
      showUrgent(m.message, m.level);
      break;
    case "ocr_status":
      if (m.status === "error") toast("사진에서 글자를 읽지 못했어요.");
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
  } else {
    div.textContent = text;
  }
  row.appendChild(div);
  chatLog().appendChild(row);
  scrollDown();
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
  if (c.url && /^https:\/\//.test(c.url)) {
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
    chip.textContent = `📚 공식 복지자료 ${m.hits}건에서 근거를 찾았어요`;
    wrap.classList.remove("hidden");
    _chipTimer = setTimeout(() => wrap.classList.add("hidden"), 4000);
  } else {
    wrap.classList.add("hidden");
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
        <span class="font-semibold">${CAT_ICON[cat] || "•"} ${cat.replace("_", " ")} <span class="text-xs text-gray-400">${items.length}건</span></span>
        <span class="text-xs px-2 py-0.5 rounded-full ${sevChip(worst.severity)}">${sevLabel(worst.severity)}</span>
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
    el.innerHTML = `<div class="font-semibold text-blue-700">🤝 ${escapeHtml(it["이름"])}</div>
      <div class="mt-1 text-sm text-gray-700">${escapeHtml(it["한줄"] || "")}</div>
      <div class="mt-1 text-xs text-gray-500">신청: ${escapeHtml(it["신청처"] || "복지로(129)·주민센터")}</div>
      ${it["기준일"] ? `<div class="mt-0.5 text-xs text-gray-400">정보 기준일 ${escapeHtml(it["기준일"])}</div>` : ""}`;
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
  if (!text || !state.ws || state.ws.readyState !== 1) return;
  _turn++;      // 진행 중이던 AI 말풍선 노출 중단
  stopAudio();  // 재생 중이던 음성 중지
  if (!opts.noBubble) appendUserBubble(text);
  state.ws.send(JSON.stringify({ type: "user_message", text, via }));
}
function sendFromInput() {
  const inp = $("#text-input");
  const t = inp.value.trim();
  if (!t) return;
  sendUserMessage(t, "text");
  inp.value = "";
  inp.style.height = "auto";
}

/* ================= 입력: 마이크 (클릭 토글 — 눌러 시작, 다시 눌러 전송) ================= */
let _stream, _ctx, _proc, _src, _chunks, _recTimer, _recog, _liveRow;

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
  if (state.recPending) return; // 권한 프롬프트 대기 중 중복 클릭 무시
  if (state.recording) await stopRec();
  else await startRec();
}
async function startRec() {
  if (state.recording) return;
  state.recPending = true;
  try {
    _stream = await navigator.mediaDevices.getUserMedia({ audio: true });
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
  btn.classList.add("bg-red-200");
  btn.setAttribute("aria-pressed", "true");
  _ctx = new (window.AudioContext || window.webkitAudioContext)();
  try { await _ctx.resume(); } catch (e) {}
  _src = _ctx.createMediaStreamSource(_stream);
  _proc = _ctx.createScriptProcessor(4096, 1, 1);
  _chunks = [];
  _proc.onaudioprocess = (e) => _chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
  _src.connect(_proc);
  _proc.connect(_ctx.destination);
  startLivePreview(); // 말하는 동안 실시간 자막 (지원 브라우저만)
  _recTimer = setTimeout(() => { if (state.recording) stopRec(); }, 30000); // 최대 30초(CSR 60초 제한 대비)
}
async function stopRec() {
  if (!state.recording) return;
  state.recording = false;
  clearTimeout(_recTimer);
  $("#mic-hint").classList.add("hidden");
  const btn = $("#btn-mic");
  btn.classList.remove("bg-red-200");
  btn.setAttribute("aria-pressed", "false");
  try {
    _proc.disconnect();
    _src.disconnect();
    _stream.getTracks().forEach((t) => t.stop());
  } catch (e) {}
  stopLivePreview();
  const sr = _ctx.sampleRate;
  const flat = flatten(_chunks);
  try { await _ctx.close(); } catch (e) {}
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
  const wav = encodeWAV(downsample(flat, sr, 16000), 16000);
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
  for (let i = 0; i < outLen; i++) out[i] = buf[Math.floor(i * ratio)];
  return out;
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
    img.onerror = reject;
    img.src = URL.createObjectURL(file);
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
  return new Promise((resolve) => {
    stopAudio();
    let url;
    try {
      url = URL.createObjectURL(blob);
      const a = new Audio(url);
      state.audio = a;
      state._audioDone = () => {
        state._audioDone = null;
        try { URL.revokeObjectURL(url); } catch (e) {}
        resolve();
      };
      a.onended = () => state._audioDone && state._audioDone();
      a.onerror = () => state._audioDone && state._audioDone();
      a.play().catch(() => state._audioDone && state._audioDone());
    } catch (e) {
      if (url) { try { URL.revokeObjectURL(url); } catch (e2) {} }
      resolve();
    }
  });
}
async function revealTurn(bubbles) {
  const myTurn = ++_turn;
  stopAudio();
  hideTyping();
  const useVoice = state.voiceOn && !state.recording;
  // 음성이면 이번 턴 말풍선들의 TTS를 미리 병렬 요청(말풍선 사이 끊김 방지)
  const blobs = useVoice ? bubbles.map((b) => fetchTTS(b.id)) : [];
  for (let i = 0; i < bubbles.length; i++) {
    if (myTurn !== _turn) return; // 새 턴/입력으로 중단
    showTyping();
    await _sleep(Math.min(900, 280 + bubbles[i].text.length * 11)); // '입력 중' 짧은 뜸
    if (myTurn !== _turn) { hideTyping(); return; }
    hideTyping();
    appendAiBubble(bubbles[i].text, bubbles[i].kind, bubbles[i].card);
    if (useVoice && state.voiceOn && !state.recording) {
      const blob = await blobs[i];
      if (myTurn !== _turn) return;
      if (blob) await playBlob(blob); // 오디오가 끝나야 다음 말풍선 등장 → 음성에 맞춘 페이싱
      else await _sleep(_readingDelay(bubbles[i].text));
    } else {
      await _sleep(_readingDelay(bubbles[i].text));
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
  const pkgs = (rep.apply_packages || []).map((p) => `<li class="mb-1">📝 ${escapeHtml(p["서비스명"])} — ${escapeHtml((p["필요서류"] || []).join(", "))} <span class="text-gray-500 text-sm">(${escapeHtml(p["신청처"] || "")})</span></li>`).join("");
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
  $("#btn-end").addEventListener("click", endSession);
  $("#btn-close-report").addEventListener("click", () => $("#report-modal").classList.add("hidden"));

  $$(".mview-btn").forEach((btn) => btn.addEventListener("click", () => switchMobile(btn.dataset.mview)));
}
function toggleVoice() {
  state.voiceOn = !state.voiceOn;
  const btn = $("#btn-voice");
  btn.setAttribute("aria-pressed", String(state.voiceOn));
  btn.textContent = state.voiceOn ? "🔊 음성" : "🔇 음성";
  btn.classList.toggle("bg-blue-100", state.voiceOn);
  if (!state.voiceOn) stopAudio();
  state.ws?.send(JSON.stringify({ type: "set_voice", on: state.voiceOn }));
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
