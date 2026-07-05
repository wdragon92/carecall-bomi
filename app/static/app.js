/* 돌봄콜 AI 프론트엔드 (frontend §11). 순수 JS, 프레임워크 없음. */
"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

const state = {
  sessionId: null,
  ws: null,
  voiceOn: true,
  streams: {}, // messageId -> bubble element
  seenFindings: new Set(),
  audio: null,
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
  ws.onmessage = (ev) => handleWS(JSON.parse(ev.data));
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
    case "ai_message_start":
      hideTyping();
      startAiBubble(m.id);
      break;
    case "ai_message_delta":
      appendDelta(m.id, m.text);
      break;
    case "ai_message_end":
      endAiBubble(m.id, m.full_text);
      if (state.voiceOn && m.full_text) enqueueTTS(m.id);
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
function startAiBubble(id) {
  const row = rowFor("ai");
  const div = document.createElement("div");
  div.className = "bubble bubble-ai";
  div.innerHTML = `<span class="txt"></span><span class="blink">▍</span>`;
  row.appendChild(div);
  chatLog().appendChild(row);
  state.streams[id] = div;
  scrollDown();
}
function appendDelta(id, text) {
  const div = state.streams[id];
  if (!div) return;
  div.querySelector(".txt").textContent += text;
  scrollDown();
}
function endAiBubble(id, full) {
  const div = state.streams[id];
  if (!div) return;
  div.querySelector(".txt").textContent = full || div.querySelector(".txt").textContent;
  const cur = div.querySelector(".blink");
  if (cur) cur.remove();
  delete state.streams[id];
  scrollDown();
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
function renderFindings(findings) {
  const box = $("#tab-findings");
  box.innerHTML = "";
  if (!findings.length) {
    box.innerHTML = `<p class="text-gray-400 text-center mt-8 text-sm">대화가 진행되면<br>특이사항이 여기에 정리됩니다.</p>`;
    return;
  }
  let hasNew = false;
  for (const f of findings) {
    const isNew = !state.seenFindings.has(f.id);
    if (isNew) {
      state.seenFindings.add(f.id);
      hasNew = true;
    }
    const el = document.createElement("div");
    el.className = `card sev-${f.severity} ${isNew ? "newcard" : ""}`;
    el.innerHTML = `
      <div class="flex items-center justify-between">
        <span class="font-semibold">${CAT_ICON[f.category] || "•"} ${f.category.replace("_", " ")}</span>
        <span class="text-xs px-2 py-0.5 rounded-full ${sevChip(f.severity)}">${f.severity}</span>
      </div>
      <div class="mt-1 text-sm text-gray-700"></div>
      ${f.needs_human ? '<div class="mt-1 text-xs font-semibold text-red-600">👤 보호자·담당자 연결 권고</div>' : ""}`;
    el.querySelector(".text-gray-700").textContent = f.content;
    box.appendChild(el);
  }
  if (hasNew) flashMobileBadge();
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
      <div class="mt-1 text-xs text-gray-500">신청: ${escapeHtml(it["신청처"] || "복지로(129)·주민센터")}</div>`;
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
  if (!opts.noBubble) appendUserBubble(text);
  state.ws.send(JSON.stringify({ type: "user_message", text, via }));
}
function sendFromInput() {
  const inp = $("#text-input");
  const t = inp.value.trim();
  if (!t) return;
  if (state.audio) state.audio.pause();
  sendUserMessage(t, "text");
  inp.value = "";
  inp.style.height = "auto";
}

/* ================= 입력: 마이크 (클릭 토글 — 눌러 시작, 다시 눌러 전송) ================= */
let _stream, _ctx, _proc, _src, _chunks, _recTimer;
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
  if (state.audio) state.audio.pause(); // 스피커(TTS) 소리가 녹음되지 않게
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
  const sr = _ctx.sampleRate;
  const flat = flatten(_chunks);
  try { await _ctx.close(); } catch (e) {}
  if (flat.length < sr * 0.3) { toast("말씀이 너무 짧았어요. 🎤을 누르고 말씀하신 뒤 다시 눌러 주세요."); return; }
  const wav = encodeWAV(downsample(flat, sr, 16000), 16000);
  const fd = new FormData();
  fd.append("file", new Blob([wav], { type: "audio/wav" }), "rec.wav");
  const note = appendUserBubble("🎤 음성 인식 중…");
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/audio`, { method: "POST", body: fd });
    const d = await r.json();
    const text = (d.text || "").trim();
    if (text) {
      note.querySelector(".bubble").textContent = text;
      sendUserMessage(text, "voice", { noBubble: true });
    } else {
      note.querySelector(".bubble").textContent = "(음성을 알아듣지 못했어요)";
    }
  } catch (e) {
    note.querySelector(".bubble").textContent = "(음성 전송에 실패했어요)";
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

/* ================= TTS 재생 (말풍선 순서대로 순차 재생 큐) ================= */
function enqueueTTS(id) {
  if (!state.voiceOn) return;
  state.ttsChain = (state.ttsChain || Promise.resolve()).then(() => playTTSOnce(id)).catch(() => {});
}
async function playTTSOnce(id) {
  if (!state.voiceOn || state.recording) return; // 녹음 중엔 재생하지 않음

  let blob;
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/tts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message_id: id }),
    });
    if (!r.ok) return;
    blob = await r.blob();
  } catch (e) {
    return;
  }
  if (!state.voiceOn) return;
  await new Promise((resolve) => {
    try {
      if (state.audio) state.audio.pause();
      const a = new Audio(URL.createObjectURL(blob));
      state.audio = a;
      a.onended = resolve;
      a.onerror = resolve;
      a.play().catch(() => resolve());
    } catch (e) {
      resolve();
    }
  });
}

/* ================= 종료 리포트 ================= */
async function endSession() {
  $("#btn-end").disabled = true;
  setBadge("상담을 정리하고 있어요…");
  try {
    const r = await fetch(`/api/sessions/${state.sessionId}/end`, { method: "POST" });
    const d = await r.json();
    renderReport(d.report);
  } catch (e) {
    toast("리포트를 만들지 못했어요.");
  } finally {
    $("#btn-end").disabled = false;
    setBadge("상담 진행 중");
  }
}
function renderReport(rep) {
  const b = $("#report-body");
  const findings = (rep.findings || [])
    .map((f) => `<li class="mb-1"><b>${escapeHtml(f.category.replace("_", " "))}</b> (${f.severity}) — ${escapeHtml(f.content)}${f.needs_human ? " 👤" : ""}</li>`)
    .join("");
  const recs = (rep.recommendations || []).map((r) => `<li class="mb-1">✅ ${escapeHtml(r)}</li>`).join("");
  const welfare = (rep.welfare || []).map((w) => `<li class="mb-1">🤝 ${escapeHtml(w["이름"])} — <span class="text-gray-500 text-sm">${escapeHtml(w["신청처"] || "")}</span></li>`).join("");
  b.innerHTML = `
    <div><h3 class="font-bold mb-1">요약</h3><p class="text-gray-700">${escapeHtml(rep.summary || "")}</p></div>
    <div><h3 class="font-bold mb-1">관찰된 특이사항</h3><ul class="list-none">${findings || '<li class="text-gray-400">없음</li>'}</ul></div>
    <div><h3 class="font-bold mb-1">후속 권고</h3><ul class="list-none">${recs || '<li class="text-gray-400">없음</li>'}</ul></div>
    ${welfare ? `<div><h3 class="font-bold mb-1">안내한 복지</h3><ul class="list-none">${welfare}</ul></div>` : ""}
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

  $$(".tab-btn").forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));
  $$(".mview-btn").forEach((btn) => btn.addEventListener("click", () => switchMobile(btn.dataset.mview)));
}
function toggleVoice() {
  state.voiceOn = !state.voiceOn;
  const btn = $("#btn-voice");
  btn.setAttribute("aria-pressed", String(state.voiceOn));
  btn.textContent = state.voiceOn ? "🔊 음성" : "🔇 음성";
  btn.classList.toggle("bg-blue-100", state.voiceOn);
  if (!state.voiceOn && state.audio) state.audio.pause();
  state.ws?.send(JSON.stringify({ type: "set_voice", on: state.voiceOn }));
}
function switchTab(tab) {
  $$(".tab-btn").forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle("border-blue-600", on);
    b.classList.toggle("text-blue-600", on);
    b.classList.toggle("border-transparent", !on);
    b.classList.toggle("text-gray-500", !on);
  });
  $("#tab-findings").classList.toggle("hidden", tab !== "findings");
  $("#tab-welfare").classList.toggle("hidden", tab !== "welfare");
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
  if (!p) return setBadge("상담 진행 중");
  const allReal = Object.values(p).every((v) => v === "real");
  setBadge(allReal ? "실시간 연동 상담 중" : "상담 진행 중 (일부 데모)");
  $("#mode-badge").title = `LLM:${p.llm} STT:${p.stt} TTS:${p.tts} OCR:${p.ocr}`;
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
