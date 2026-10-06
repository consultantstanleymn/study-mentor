/* Study Mentor front end: state, conversation socket, audio playback queue, mic capture (push-to-talk and hands-free), whiteboard. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const el = {
    trackSeg: $("trackSeg"), phase: $("phase"), dayNum: $("dayNum"), dayOf: $("dayOf"), dayTitle: $("dayTitle"),
    svcChips: $("svcChips"), bar: $("bar"), barFill: $("barFill"), barLabel: $("barLabel"),
    prevDay: $("prevDay"), nextDay: $("nextDay"), jumpBtn: $("jumpBtn"), jumpSheet: $("jumpSheet"), jumpClose: $("jumpClose"), dayList: $("dayList"),
    modes: $("modes"), weakList: $("weakList"), stats: $("stats"),
    avatar: $("avatar"), status: $("status"), capLine: $("capLine"), transcript: $("transcript"),
    typeForm: $("typeForm"), typeIn: $("typeIn"), stopBtn: $("stopBtn"), micBtn: $("micBtn"), micLabel: $("micLabel"), handsFree: $("handsFree"),
    boardBody: $("boardBody"), boardEmpty: $("boardEmpty"), boardTabs: $("boardTabs"),
    voiceBtn: $("voiceBtn"), voicePop: $("voicePop"), voiceSel: $("voiceSel"), speedRng: $("speedRng"), speedOut: $("speedOut"), voiceTest: $("voiceTest"),
    toast: $("toast"), stage: $("stage"), layout: document.querySelector(".layout"), board: document.querySelector(".board"), splitter: $("splitter"), autoBtn: $("autoBtn"), stepPill: $("stepPill"), suggest: $("suggest"), hint: $("hint"),
  };

  const S = {
    tracks: [], track: null, mode: "teach", sessionActive: false,
    ws: null, wsReady: false, turn: 0, genDone: true,
    queue: [], playing: false, audio: null, currentUrl: null,
    boards: [], boardIdx: -1,
    voice: localStorage.getItem("mentor.voice") || "", speed: parseFloat(localStorage.getItem("mentor.speed") || "1"),
    mic: null, recording: false, handsFree: false, awaitingStt: false,
    boardAuto: localStorage.getItem("mentor.boardAuto") !== "0", boardMode: "normal", steps: 0,
  };

  /* ---------------- helpers ---------------- */
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function toast(msg, ms = 3800) {
    el.toast.textContent = msg; el.toast.hidden = false;
    clearTimeout(toast.t); toast.t = setTimeout(() => (el.toast.hidden = true), ms);
  }
  function setState(state, text) {
    el.avatar.dataset.state = state;
    el.stage.dataset.state = state;
    if (text) el.status.textContent = text;
    el.stopBtn.disabled = !(state === "speaking" || state === "thinking");
  }
  const track = () => S.tracks.find((t) => t.id === S.track);
  const api = async (path, opts) => { const r = await fetch(path, opts); if (!r.ok) throw new Error(await r.text()); return r.json(); };
  const jpost = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  /* ---------------- state + sidebar ---------------- */
  async function loadState(keepTrack = true) {
    const st = await api("/api/state");
    S.tracks = st.tracks;
    if (!S.voice || !st.voices[S.voice]) S.voice = st.voice;
    el.voiceSel.innerHTML = Object.entries(st.voices).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
    el.voiceSel.value = S.voice;
    el.speedRng.value = S.speed; el.speedOut.textContent = S.speed.toFixed(2).replace(/0$/, "") + "x";
    if (!keepTrack || !S.track || !S.tracks.find((t) => t.id === S.track)) S.track = localStorage.getItem("mentor.track") || (S.tracks[0] && S.tracks[0].id);
    if (!S.tracks.find((t) => t.id === S.track) && S.tracks[0]) S.track = S.tracks[0].id;
    renderTracks(); renderSide();
  }
  function renderTracks() {
    el.trackSeg.innerHTML = S.tracks.map((t) => `<button role="tab" aria-selected="${t.id === S.track}" data-track="${t.id}">${esc(t.name)}</button>`).join("");
  }
  function renderSide() {
    const t = track(); if (!t) return;
    el.phase.textContent = t.phase.replace(/^Phase \d+:\s*/, (m) => m.trim() + " ") || "Study plan";
    el.dayNum.textContent = `${t.unit} ${t.day}`; el.dayOf.textContent = `of ${t.days}`;
    el.dayTitle.textContent = t.title || "";
    const pct = Math.round((t.done.length / t.days) * 100);
    el.barFill.style.width = pct + "%"; el.bar.setAttribute("aria-valuenow", pct);
    el.barLabel.textContent = `${t.done.length} of ${t.days} ${t.unit.toLowerCase()}s complete` + (t.unit === "Day" && t.week ? ` · week ${t.week}` : "");
    el.weakList.innerHTML = t.weak.length
      ? t.weak.map((w) => `<li><span>${esc(w.topic)}</span><em>${w.misses} missed</em></li>`).join("")
      : `<li class="empty">Nothing yet. Your misses show up here, and I bring them back until they stick.</li>`;
    el.stats.innerHTML = `<div class="stat"><b>${t.stats.streak}</b><span>day streak</span></div><div class="stat"><b>${t.stats.minutes}</b><span>minutes studied</span></div>`;
    api(`/api/day/${t.id}/${t.day}`).then((d) => {
      el.svcChips.innerHTML = (d.services || []).slice(0, 5).map((s) => `<span class="chip">${esc(s)}</span>`).join("");
    }).catch(() => (el.svcChips.innerHTML = ""));
  }
  async function changeDay(day) {
    const t = track(); if (!t || day < 1 || day > t.days) return;
    await jpost("/api/day", { track: t.id, day });
    await loadState(); refreshResume();
    if (S.sessionActive) endSession(true);
  }
  el.trackSeg.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-track]"); if (!b) return;
    S.track = b.dataset.track; localStorage.setItem("mentor.track", S.track);
    if (S.sessionActive) endSession(true);
    renderTracks(); renderSide();
  });
  el.prevDay.addEventListener("click", () => changeDay(track().day - 1));
  el.nextDay.addEventListener("click", () => changeDay(track().day + 1));
  el.modes.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]"); if (!b) return;
    S.mode = b.dataset.mode;
    el.modes.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", x === b));
    if (S.sessionActive) { toast("Mode changes next session. Press Start to apply."); }
  });
  el.jumpBtn.addEventListener("click", async () => {
    const t = track(); el.jumpBtn.dataset.unit = t.unit; const out = await api(`/api/outline/${t.id}`);
    el.dayList.innerHTML = out.map((d) => `<li><button data-day="${d.day}" class="${d.day === t.day ? "cur" : ""}"><span class="dn">${t.unit} ${d.day}</span><span>${esc(d.title)}</span><span class="ok">${t.done.includes(d.day) ? "done" : ""}</span><span class="ph">${esc((d.phase || "").replace(/^Phase \d+:\s*/, ""))}</span></button></li>`).join("");
    el.jumpSheet.hidden = false;
    const cur = el.dayList.querySelector(".cur"); if (cur) cur.scrollIntoView({ block: "center" });
    el.jumpClose.focus();
  });
  el.dayList.addEventListener("click", (e) => { const b = e.target.closest("button[data-day]"); if (b) { el.jumpSheet.hidden = true; changeDay(+b.dataset.day); } });
  el.jumpClose.addEventListener("click", () => (el.jumpSheet.hidden = true));
  el.jumpSheet.addEventListener("click", (e) => { if (e.target === el.jumpSheet) el.jumpSheet.hidden = true; });

  /* ---------------- voice settings ---------------- */
  el.voiceBtn.addEventListener("click", () => {
    const open = el.voicePop.hidden; el.voicePop.hidden = !open; el.voiceBtn.setAttribute("aria-expanded", open);
  });
  document.addEventListener("click", (e) => { if (!el.voicePop.hidden && !e.target.closest(".top-actions")) { el.voicePop.hidden = true; el.voiceBtn.setAttribute("aria-expanded", false); } });
  el.voiceSel.addEventListener("change", () => { S.voice = el.voiceSel.value; localStorage.setItem("mentor.voice", S.voice); wsSend({ type: "set", voice: S.voice, speed: S.speed }); });
  el.speedRng.addEventListener("input", () => { S.speed = parseFloat(el.speedRng.value); el.speedOut.textContent = S.speed.toFixed(2).replace(/0$/, "") + "x"; localStorage.setItem("mentor.speed", S.speed); wsSend({ type: "set", voice: S.voice, speed: S.speed }); });
  el.voiceTest.addEventListener("click", async () => {
    stopPlayback();
    try {
      const r = await fetch(`/api/tts?text=${encodeURIComponent("Right, Stanley. This is how I will sound. Shall we begin?")}&voice_id=${S.voice}&speed=${S.speed}`);
      const url = URL.createObjectURL(await r.blob()); const a = new Audio(url); a.onended = () => URL.revokeObjectURL(url); a.play();
    } catch (e) { toast("Could not play the sample."); }
  });

  /* ---------------- socket ---------------- */
  function connect() {
    const ws = new WebSocket(`ws://${location.host}/ws`);
    S.ws = ws;
    ws.onopen = () => { S.wsReady = true; };
    ws.onclose = () => { S.wsReady = false; setTimeout(connect, 1500); };
    ws.onmessage = (m) => onEvent(JSON.parse(m.data));
  }
  function wsSend(o) { if (S.ws && S.wsReady) S.ws.send(JSON.stringify(o)); }

  function onEvent(ev) {
    if (ev.turn !== undefined && ev.turn < S.turn) return; // stale (interrupted) turn
    if (ev.turn !== undefined) S.turn = ev.turn;
    switch (ev.type) {
      case "status": if (ev.state === "thinking") { S.genDone = false; setState("thinking", "Thinking"); } else if (ev.state === "idle") setState("idle", "Ready when you are"); break;
      case "sentence": if (window.glossScan) glossScan(ev.text); S.queue.push({ kind: "sentence", text: ev.text, audio: ev.audio }); pump(); break;
      case "board": S.queue.push({ ...ev, kind: "board", style: ev.kind }); pump(); break;
      case "focus": S.queue.push({ kind: "focus", nodes: ev.nodes }); pump(); break;
      case "layout": S.queue.push({ kind: "layout", board: ev.board }); pump(); break;
      case "log": refreshSoon(); break;
      case "day_done": S.queue.push({ kind: "daydone", day: ev.day }); pump(); break;
      case "error": addTurn("err", "Mentor", ev.message); setState("idle", "Something went wrong"); toast(ev.message, 6000); break;
      case "done": S.genDone = true; pump(); break;
      case "resumed":
        el.transcript.innerHTML = "";
        for (const m of ev.transcript) addTurn(m.role === "user" ? "you" : "mentor", m.role === "user" ? "You" : "Mentor", m.text);
        if (ev.board) S.queue.push({ ...ev.board, kind: "board", style: ev.board.kind });
        curTurnEl = null; pump(); break;
      case "ended": break;
    }
  }
  let refreshT; function refreshSoon() { clearTimeout(refreshT); refreshT = setTimeout(() => loadState().catch(() => {}), 600); }

  /* ---------------- playback queue ---------------- */
  function b64ToBlobUrl(b64) {
    const bin = atob(b64); const buf = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
    return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
  }
  let curTurnEl = null;
  function pump() {
    if (S.playing) return;
    const item = S.queue.shift();
    if (!item) {
      if (S.genDone && S.sessionActive) {
        setState("idle", S.handsFree ? "Listening, just talk" : "Your turn");
        if (S.handsFree) startHandsFree();
      }
      return;
    }
    if (item.kind === "board") { addBoard(item); return pump(); }
    if (item.kind === "focus") { applyFocus(item.nodes); return pump(); }
    if (item.kind === "layout") { setBoardMode(item.board, "ai"); return pump(); }
    if (item.kind === "daydone") { toast(`${track().unit} ${item.day} complete. Nicely done.`, 5000); loadState().catch(() => {}); return pump(); }
    S.playing = true; setState("speaking", "Speaking");
    showCaption(item.text);
    appendMentor(item.text);
    const url = b64ToBlobUrl(item.audio); S.currentUrl = url;
    const a = new Audio(url); S.audio = a; a.playbackRate = 1;
    const done = () => { URL.revokeObjectURL(url); if (S.audio === a) { S.audio = null; S.currentUrl = null; } S.playing = false; if (!S.queue.length) endLive(); pump(); };
    a.onended = done; a.onerror = done;
    a.play().catch(() => done());
  }
  function stopPlayback() {
    S.queue = []; endLive();
    if (S.audio) { S.audio.onended = null; S.audio.onerror = null; try { S.audio.pause(); } catch (e) {} if (S.currentUrl) URL.revokeObjectURL(S.currentUrl); S.audio = null; }
    S.playing = false;
  }
  function interrupt() {
    stopPlayback(); S.genDone = true; curTurnEl = null;
    wsSend({ type: "interrupt" });
    setState("idle", "Interrupted. Go ahead.");
  }
  el.stopBtn.addEventListener("click", interrupt);

  /* ---------------- captions + transcript ---------------- */
  let capT;
  function showCaption(text) {
    el.capLine.classList.add("fade");
    clearTimeout(capT);
    capT = setTimeout(() => { el.capLine.textContent = text; el.capLine.classList.remove("fade", "dim"); }, 120);
  }
  function addTurn(kind, who, text) {
    const li = document.createElement("li"); li.className = `turn ${kind}`;
    li.innerHTML = `<span class="who">${esc(who)}</span><span class="what"></span>`;
    li.querySelector(".what").textContent = text;
    el.transcript.appendChild(li); el.transcript.scrollTop = el.transcript.scrollHeight;
    return li;
  }
  let liveSpan = null;
  function appendMentor(text) {
    if (!curTurnEl || !curTurnEl.isConnected) curTurnEl = addTurn("mentor", "Mentor", "");
    const w = curTurnEl.querySelector(".what");
    if (liveSpan) { liveSpan.classList.remove("live"); liveSpan.classList.add("heard"); }
    const sp = document.createElement("span"); sp.className = "s live"; sp.textContent = text;
    if (w.childNodes.length) w.appendChild(document.createTextNode(" "));
    w.appendChild(sp); liveSpan = sp;
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }
  function endLive() { if (liveSpan) { liveSpan.classList.remove("live"); liveSpan.classList.add("heard"); liveSpan = null; } }

  /* ---------------- whiteboard ---------------- */
  if (window.mermaid) mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict", fontFamily: "Inter, system-ui, sans-serif", flowchart: { useMaxWidth: true, htmlLabels: true, nodeSpacing: 28, rankSpacing: 38, padding: 10 },
    themeVariables: { fontSize: "17px", background: "#112124", primaryColor: "#172C30", primaryBorderColor: "#2DD4BF", primaryTextColor: "#E8F5F2", lineColor: "#5EEAD4", secondaryColor: "#0C1719", tertiaryColor: "#0C1719" } });
  let mid = 0;
  async function addBoard(b) {
    el.boardEmpty && (el.boardEmpty.hidden = true);
    const card = document.createElement("div"); card.className = "bcard";
    card.innerHTML = `<h2>${esc(b.title || "Board")}</h2><div class="md"></div>`;
    const md = card.querySelector(".md");
    if (b.style === "diagram" && window.mermaid) {
      try {
        const { svg } = await mermaid.render("mm" + ++mid, b.body.replace(/^```mermaid\s*|```$/g, "").trim());
        md.innerHTML = `<div class="diagram">${svg}</div>`;
      } catch (e) { md.innerHTML = DOMPurify.sanitize(marked.parse("```\n" + b.body + "\n```")); }
    } else {
      md.innerHTML = DOMPurify.sanitize(marked.parse(b.body.replace(/\\n/g, "\n")));
    }
    S.steps = 0; el.stepPill.hidden = true;
    S.boards.push({ title: b.title || "Board", node: card });
    if (b.style === "diagram") setBoardMode("wide", "auto"); else if (S.boardMode === "rail") setBoardMode("normal", "auto");
    showBoard(S.boards.length - 1);
    flashBoard();
  }
  // Highlight diagram nodes while the mentor explains them (stage-by-stage walkthrough).
  function applyFocus(nodes) {
    const svg = el.boardBody.querySelector(".bcard .diagram svg"); if (!svg) return;
    const want = (nodes || "").split(",").map((s) => s.trim()).filter(Boolean);
    const all = [...svg.querySelectorAll("g.node, g.cluster")];
    svg.classList.toggle("has-focus", want.length > 0);
    let first = null;
    all.forEach((g) => {
      const m = /^(?:flowchart|state|class|er)-(.+?)-\d+$/.exec(g.id || "");
      const on = !!m && want.includes(m[1]);
      g.classList.toggle("focus", on);
      if (on && !first) first = g;
    });
    document.querySelector(".board").classList.toggle("active", want.length > 0);
    if (want.length) { S.steps++; el.stepPill.hidden = false; el.stepPill.textContent = "Step " + S.steps; }
    else { el.stepPill.hidden = true; }
    if (first) first.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
  }
  window.__mentor = { addBoard, applyFocus }; // used by the automated UI test
  let freshT;
  function flashBoard() {
    const bd = document.querySelector(".board"); bd.classList.add("fresh");
    clearTimeout(freshT); freshT = setTimeout(() => bd.classList.remove("fresh"), 4200);
  }
  document.addEventListener("click", (e) => {
    const d = e.target.closest(".bcard .diagram"); if (!d) return;
    const z = document.createElement("div"); z.className = "zoom"; z.innerHTML = `<div>${d.innerHTML}</div>`;
    z.addEventListener("click", () => z.remove()); document.body.appendChild(z);
  });
  function showBoard(i) {
    S.boardIdx = i;
    [...el.boardBody.querySelectorAll(".bcard")].forEach((n) => n.remove());
    el.boardBody.appendChild(S.boards[i].node);
    el.boardTabs.innerHTML = S.boards.slice(-8).map((b, k) => { const idx = S.boards.length - Math.min(8, S.boards.length) + k; return `<button role="tab" aria-selected="${idx === i}" data-i="${idx}">${esc(b.title)}</button>`; }).join("");
  }
  el.boardTabs.addEventListener("click", (e) => { const b = e.target.closest("button[data-i]"); if (b) showBoard(+b.dataset.i); });

  /* ---------------- session ---------------- */
  function startSession() {
    const t = track(); if (!t) return;
    if (!S.wsReady) { toast("Connecting to the mentor. One moment."); return; }
    stopPlayback(); S.sessionActive = true; S.genDone = false;
    el.transcript.innerHTML = ""; S.boards = []; el.boardBody.querySelectorAll(".bcard").forEach((n) => n.remove()); el.boardTabs.innerHTML = ""; el.boardEmpty.hidden = false; curTurnEl = null;
    wsSend({ type: "start", track: t.id, day: t.day, mode: S.mode, voice: S.voice, speed: S.speed });
    setState("thinking", "Thinking");
    showCaption("One moment. Pulling today's lesson together.");
    syncCta();
  }
  function resumeSession() {
    const t = track(); if (!t || !S.wsReady) { toast("Connecting to the mentor. One moment."); return; }
    stopPlayback(); S.sessionActive = true; S.genDone = false;
    S.boards = []; el.boardBody.querySelectorAll(".bcard").forEach((n) => n.remove()); el.boardTabs.innerHTML = ""; el.boardEmpty.hidden = false; curTurnEl = null;
    wsSend({ type: "resume", track: t.id, day: t.day, voice: S.voice, speed: S.speed });
    setState("thinking", "Thinking"); showCaption("Welcome back. Picking up where we left off."); syncCta();
  }
  async function refreshResume() {
    const t = track(); if (!t) return;
    try { const r = await (await fetch(`/api/resume/${t.id}/${t.day}`)).json(); resumeBtn.hidden = !(r.available && !S.sessionActive); resumeBtn.textContent = `Resume where we left off (${r.turns || 0} turns)`; }
    catch (e) { resumeBtn.hidden = true; }
  }
  function syncCta() {
    const idle = !S.sessionActive;
    el.micBtn.classList.toggle("cta", idle);
    el.micLabel.hidden = !idle;
    if (idle) el.micLabel.textContent = "Start session";
    el.micBtn.setAttribute("aria-label", idle ? "Start session" : "Hold to talk (or hold Space)");
    startBtn.textContent = idle ? "Start session" : "End session"; startBtn.hidden = idle;
    el.suggest.classList.toggle("gone", !idle);
    if (idle) refreshResume(); else resumeBtn.hidden = true;
    $("sessbar").hidden = idle; S.paused = false; $("pauseBtn").textContent = "Pause";
    el.hint.innerHTML = idle ? "Press Start to begin &middot; then hold <kbd>Space</kbd> to talk, <kbd>Esc</kbd> to interrupt" : "Hold <kbd>Space</kbd> to talk &middot; <kbd>Esc</kbd> to interrupt";
  }
  function endSession(silent) {
    wsSend({ type: "end" }); stopPlayback(); stopHandsFree(); S.sessionActive = false; S.genDone = true;
    setState("idle", "Session ended"); syncCta(); if (window.glossClear) glossClear();
    if (!silent) showCaption("Good work. See you next session.");
    loadState().catch(() => {});
  }
  $("pauseBtn").addEventListener("click", () => {
    if (!S.paused) { interrupt(); S.paused = true; $("pauseBtn").textContent = "Continue"; setState("idle", "Paused. Press Continue when ready."); }
    else { S.paused = false; $("pauseBtn").textContent = "Pause"; sendUser("Please continue from exactly where we stopped."); }
  });
  $("endBtn").addEventListener("click", () => endSession());
  // The primary action lives in the Today card
  const resumeBtn = document.createElement("button");
  resumeBtn.className = "btn ghost resume-btn"; resumeBtn.hidden = true; resumeBtn.id = "resumeBtn";
  el.suggest.parentNode.insertBefore(resumeBtn, el.suggest);
  resumeBtn.addEventListener("click", resumeSession);
  const startBtn = document.createElement("button");
  startBtn.className = "btn ghost"; startBtn.id = "startBtn"; startBtn.textContent = "Start session"; startBtn.style.cssText = "width:100%;margin-top:14px";
  document.querySelector(".today").appendChild(startBtn);
  startBtn.addEventListener("click", () => (S.sessionActive ? endSession() : startSession()));
  const doneBtn = document.createElement("button");
  doneBtn.className = "btn ghost small"; doneBtn.textContent = "Mark complete"; doneBtn.style.cssText = "width:100%;margin-top:8px";
  document.querySelector(".today").appendChild(doneBtn);
  doneBtn.addEventListener("click", async () => { const t = track(); await jpost("/api/mark", { track: t.id, day: t.day }); await loadState(); toast(`${t.unit} ${t.day} marked complete.`); });

  function sendUser(text) {
    text = (text || "").trim(); if (!text) return;
    if (!S.sessionActive) { toast("Press Start session first."); return; }
    stopPlayback(); curTurnEl = null; S.genDone = false;
    addTurn("you", "You", text);
    wsSend({ type: "user", text });
    setState("thinking", "Thinking");
  }
  el.typeForm.addEventListener("submit", (e) => { e.preventDefault(); const v = el.typeIn.value; el.typeIn.value = ""; sendUser(v); });

  /* ---------------- microphone: PCM capture, push-to-talk + hands-free ---------------- */
  const TARGET_RATE = 16000;
  const mic = { ctx: null, node: null, stream: null, ring: [], ringMax: 0, rec: null, rate: 48000 };
  async function ensureMic() {
    if (mic.ctx) { if (mic.ctx.state === "suspended") await mic.ctx.resume(); return true; }
    try {
      mic.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    } catch (e) { toast("Microphone blocked. Allow it in the browser and try again.", 6000); return false; }
    mic.ctx = new (window.AudioContext || window.webkitAudioContext)();
    mic.rate = mic.ctx.sampleRate;
    const src = mic.ctx.createMediaStreamSource(mic.stream);
    const code = `class P extends AudioWorkletProcessor{process(i){const c=i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor('pcm',P)`;
    await mic.ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([code], { type: "application/javascript" })));
    mic.node = new AudioWorkletNode(mic.ctx, "pcm");
    mic.node.port.onmessage = (e) => onPcm(e.data);
    src.connect(mic.node);
    mic.ringMax = Math.ceil(mic.rate * 0.5); // 0.5 s pre-roll
    return true;
  }
  let ringLen = 0;
  const vad = { floor: 0.006, speech: 0, silence: 0, active: false, started: 0 };
  function rms(buf) { let s = 0; for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i]; return Math.sqrt(s / buf.length); }

  function onPcm(chunk) {
    // pre-roll ring
    mic.ring.push(chunk); ringLen += chunk.length;
    while (ringLen - mic.ring[0].length > mic.ringMax) { ringLen -= mic.ring[0].length; mic.ring.shift(); }
    if (mic.rec) mic.rec.push(chunk);
    if (!S.handsFree || S.recordingPtt) return;
    const level = rms(chunk), dur = chunk.length / mic.rate;
    const speaking = S.playing || S.audio; // mentor is talking: need a louder voice to barge in
    const thresh = Math.max(0.02, vad.floor * 4) * (speaking ? 2.2 : 1);
    if (!vad.active) {
      if (level > thresh) { vad.speech += dur; } else { vad.speech = Math.max(0, vad.speech - dur); vad.floor = vad.floor * 0.97 + level * 0.03; }
      if (vad.speech > (speaking ? 0.25 : 0.12)) {
        vad.active = true; vad.silence = 0; vad.started = performance.now();
        mic.rec = mic.ring.slice(); // include pre-roll
        if (speaking || S.genDone === false) interrupt();
        setState("listening", "Listening"); setMicUi(true);
      }
    } else {
      if (level > thresh * 0.6) vad.silence = 0; else vad.silence += dur;
      if (vad.silence > 0.95 || performance.now() - vad.started > 45000) finishRecording();
    }
  }
  function startRecording() { mic.rec = mic.ring.slice(); }
  async function finishRecording() {
    const chunks = mic.rec; mic.rec = null; vad.active = false; vad.speech = 0; vad.silence = 0; setMicUi(false);
    if (!chunks || !chunks.length) return;
    const wav = encodeWav(chunks, mic.rate, TARGET_RATE);
    if (wav.size < 12000) { setState("idle", S.handsFree ? "Listening, just talk" : "Your turn"); return; } // under ~0.35 s: ignore
    setState("thinking", "Transcribing"); S.awaitingStt = true;
    try {
      const fd = new FormData(); fd.append("audio", wav, "speech.wav");
      const r = await fetch("/api/stt", { method: "POST", body: fd }); const j = await r.json();
      if (j.error) throw new Error(j.error);
      if (!j.text) { setState("idle", S.handsFree ? "Listening, just talk" : "Did not catch that. Try again."); return; }
      sendUser(j.text);
    } catch (e) { toast("Speech recognition failed: " + e.message, 6000); setState("idle", "Your turn"); }
    finally { S.awaitingStt = false; }
  }
  function encodeWav(chunks, inRate, outRate) {
    let n = 0; chunks.forEach((c) => (n += c.length));
    const all = new Float32Array(n); let o = 0; chunks.forEach((c) => { all.set(c, o); o += c.length; });
    const ratio = inRate / outRate, len = Math.floor(all.length / ratio), pcm = new Int16Array(len);
    for (let i = 0; i < len; i++) { // box-filter downsample
      const a = Math.floor(i * ratio), b = Math.min(all.length, Math.floor((i + 1) * ratio)); let s = 0;
      for (let k = a; k < b; k++) s += all[k]; const v = Math.max(-1, Math.min(1, s / Math.max(1, b - a))); pcm[i] = v * 32767;
    }
    const buf = new ArrayBuffer(44 + pcm.length * 2), dv = new DataView(buf);
    const w = (p, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(p + i, s.charCodeAt(i)); };
    w(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVE"); w(12, "fmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, outRate, true); dv.setUint32(28, outRate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); w(36, "data"); dv.setUint32(40, pcm.length * 2, true);
    new Int16Array(buf, 44).set(pcm);
    return new Blob([buf], { type: "audio/wav" });
  }
  function setMicUi(on) { el.micBtn.setAttribute("aria-pressed", on); if (S.sessionActive) { el.micLabel.hidden = true; } }

  // push-to-talk
  S.recordingPtt = false;
  async function pttDown() {
    if (!S.sessionActive) { startSession(); return; }
    if (S.recordingPtt || !(await ensureMic())) return;
    S.recordingPtt = true;
    if (S.playing || S.audio || !S.genDone) interrupt();
    startRecording(); setState("listening", "Listening"); setMicUi(true);
  }
  function pttUp() { if (!S.recordingPtt) return; S.recordingPtt = false; setTimeout(finishRecording, 180); }
  el.micBtn.addEventListener("pointerdown", (e) => { e.preventDefault(); pttDown(); });
  el.micBtn.addEventListener("pointerup", pttUp); el.micBtn.addEventListener("pointerleave", pttUp);
  el.micBtn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); if (!S.recordingPtt) pttDown(); } });
  el.micBtn.addEventListener("keyup", (e) => { if (e.key === "Enter") pttUp(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { if (!el.jumpSheet.hidden) el.jumpSheet.hidden = true; else interrupt(); return; }
    if (e.code === "Space" && !e.repeat && !/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(document.activeElement.tagName)) { e.preventDefault(); pttDown(); }
  });
  document.addEventListener("keyup", (e) => { if (e.code === "Space") pttUp(); });

  // hands-free
  async function startHandsFree() { if (!S.handsFree) return; if (!(await ensureMic())) { el.handsFree.checked = false; S.handsFree = false; return; } setMicUi(false); }
  function stopHandsFree() { vad.active = false; vad.speech = 0; if (!S.recordingPtt) mic.rec = null; setMicUi(false); }
  el.handsFree.addEventListener("change", async () => {
    S.handsFree = el.handsFree.checked;
    if (S.handsFree) { await startHandsFree(); if (S.handsFree) toast("Hands-free on. Use headphones so I do not hear myself.", 4500); } else stopHandsFree();
    });

  /* ---------------- adaptive whiteboard size ---------------- */
  function setBoardMode(mode, source) {
    if (!["rail", "normal", "wide"].includes(mode)) return;
    if (source !== "user" && !S.boardAuto) return;          // user pinned a size: the mentor must not fight them
    if (source === "auto" && S.boardMode === "wide" && mode === "normal") return;
    el.layout.style.removeProperty("--board-w");
    el.layout.dataset.board = mode; S.boardMode = mode;
  }
  function setAuto(on) {
    S.boardAuto = on; localStorage.setItem("mentor.boardAuto", on ? "1" : "0");
    el.autoBtn.setAttribute("aria-pressed", on);
    if (on) setBoardMode(S.boards.length ? "normal" : "normal", "user");
  }
  el.autoBtn.addEventListener("click", () => setAuto(!S.boardAuto));
  let dragging = false;
  function widthFromPointer(x) { const pad = 20; return Math.max(320, Math.min(window.innerWidth - x - pad, window.innerWidth - 300 - 380 - 72)); }
  el.splitter.addEventListener("pointerdown", (e) => { dragging = true; el.splitter.setPointerCapture(e.pointerId); el.layout.classList.add("dragging"); e.preventDefault(); });
  el.splitter.addEventListener("pointermove", (e) => { if (!dragging) return; el.layout.dataset.board = "custom"; el.layout.style.setProperty("--board-w", widthFromPointer(e.clientX) + "px"); });
  const endDrag = () => { if (!dragging) return; dragging = false; el.layout.classList.remove("dragging"); setAutoQuiet(false); };
  function setAutoQuiet(on) { S.boardAuto = on; localStorage.setItem("mentor.boardAuto", on ? "1" : "0"); el.autoBtn.setAttribute("aria-pressed", on); }
  el.splitter.addEventListener("pointerup", endDrag); el.splitter.addEventListener("pointercancel", endDrag);
  el.splitter.addEventListener("dblclick", () => { setAutoQuiet(false); const cur = el.layout.dataset.board; el.layout.style.removeProperty("--board-w"); el.layout.dataset.board = cur === "wide" ? "normal" : "wide"; S.boardMode = el.layout.dataset.board; });
  el.splitter.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return; e.preventDefault();
    const cur = el.board.getBoundingClientRect().width; setAutoQuiet(false);
    el.layout.dataset.board = "custom"; el.layout.style.setProperty("--board-w", Math.max(320, Math.min(900, cur + (e.key === "ArrowLeft" ? 48 : -48))) + "px");
  });
  el.board.addEventListener("click", (e) => { if (S.boardMode === "rail" && !e.target.closest(".splitter")) { setAutoQuiet(false); setBoardMode("normal", "user"); } });
  setBoardMode("normal", "user"); el.autoBtn.setAttribute("aria-pressed", S.boardAuto);

  el.suggest.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]"); if (!b) return;
    S.mode = b.dataset.mode;
    el.modes.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", x.dataset.mode === S.mode));
    startSession();
  });
  syncCta();

  /* ---------------- boot ---------------- */
  loadState(false).then(() => { refreshResume();
    const t = track(); if (t) showCaption(t.title);
  }).catch((e) => toast("Cannot reach the mentor server: " + e.message, 8000));
  connect();
})();
