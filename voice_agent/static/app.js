// Browser client for the bidi-streaming voice agent.
// Uses only textContent / createElement for rendering (no innerHTML).
(() => {
  const $ = (id) => document.getElementById(id);
  const micBtn = $("micBtn");
  const statusText = $("statusText");
  const statusDot = $("statusDot");
  const agentPill = $("agentPill");
  const transcriptEl = $("transcript");
  const activityEl = $("activity");
  const textForm = $("textForm");
  const textInput = $("textInput");
  const sendBtn = $("sendBtn");
  const callerSelect = $("callerSelect");
  const callerHint = $("callerHint");
  const modePill = $("modePill");
  const failBtn = $("failBtn");
  const handoverEl = $("handover");
  const handoverTitle = $("handoverTitle");
  const handoverBody = $("handoverBody");
  let callers = [];
  let endingCall = false; // the agent ended the call: let the goodbye finish before closing audio

  let ws = null;
  let micStream = null;
  let recorderCtx = null;
  let playerCtx = null;
  let playerNode = null;
  const openBubble = { user: null, agent: null };

  // ---------- UI helpers ----------
  function setStatus(text, state) {
    statusText.textContent = text;
    statusDot.className = "dot " + (state || "");
  }

  function setConnected(connected) {
    micBtn.textContent = connected ? "Stop" : "Start talking";
    micBtn.classList.toggle("danger", connected);
    textInput.disabled = !connected;
    sendBtn.disabled = !connected;
    callerSelect.disabled = connected;
    failBtn.disabled = !connected || !failBtn.dataset.enabled;
    if (!connected) agentPill.textContent = "offline";
  }

  function closeBubble(role) {
    if (openBubble[role]) openBubble[role].classList.remove("live");
    openBubble[role] = null;
  }

  function newBubble(role, label) {
    const wrap = document.createElement("div");
    wrap.className = "msg " + role + " live";
    const who = document.createElement("div");
    who.className = "who";
    who.textContent = label;
    const body = document.createElement("div");
    body.className = "body";
    wrap.append(who, body);
    transcriptEl.appendChild(wrap);
    return wrap;
  }

  function onTranscript({ role, text, partial }) {
    // A new speaker closes the other speaker's open bubble.
    closeBubble(role === "user" ? "agent" : "user");
    let bubble = openBubble[role];
    if (!bubble) {
      const label = role === "user" ? "You" : "Assistant";
      bubble = openBubble[role] = newBubble(role, label);
    }
    const body = bubble.querySelector(".body");
    if (partial) {
      body.textContent += text;
    } else {
      body.textContent = text.trim(); // final = full text for this utterance
      closeBubble(role);
    }
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  function addActivity(kind, text) {
    const li = document.createElement("li");
    const tag = document.createElement("span");
    tag.className = "tag " + kind;
    tag.textContent = kind;
    const span = document.createElement("span");
    span.textContent = " " + text;
    li.append(tag, span);
    activityEl.prepend(li);
    while (activityEl.children.length > 50) activityEl.lastChild.remove();
  }

  // ---------- Server events ----------
  function onEvent(msg) {
    switch (msg.type) {
      case "audio":
        if (playerNode) {
          const buf = b64ToBuffer(msg.data);
          playerNode.port.postMessage(buf, [buf]);
        }
        break;
      case "transcript":
        onTranscript(msg);
        break;
      case "active_agent":
        agentPill.textContent = msg.name;
        addActivity("agent", "Active agent: " + msg.name);
        break;
      case "tool_call":
        if (msg.name === "transfer_to_agent") {
          addActivity("transfer", "→ " + (msg.args.agent_name || ""));
        } else {
          addActivity("tool", `${msg.name}(${JSON.stringify(msg.args)})`);
        }
        break;
      case "tool_result": {
        const r = msg.response || {};
        if (msg.name !== "transfer_to_agent") {
          // Gateway envelope: show next_step, or the violation code when it failed.
          const v = (r.violations && r.violations[0]) || null;
          const step = (r.responseData && r.responseData.next_step) || r.next_step;
          const what = v ? `fail ${v.code}` : step || r.status || (r.error ? "error" : "done");
          addActivity("tool", `${msg.name} → ${what}`);
        }
        break;
      }
      case "handover":
        showHandover(msg);
        break;
      case "call_ended":
        addActivity("handover", "Call ended by the agent");
        setStatus("Call ended", "");
        endingCall = true;
        setTimeout(() => { endingCall = false; stop(); setStatus("Call ended", ""); }, 3000);
        break;
      case "interrupted":
        if (playerNode) playerNode.port.postMessage({ command: "clear" });
        closeBubble("agent");
        break;
      case "turn_complete":
        closeBubble("user");
        closeBubble("agent");
        setStatus(msg.state === "IN_PROGRESS" ? "Thinking…" : "Listening", "on");
        break;
      case "error":
        addActivity("error", msg.message);
        break;
      // "ready", "pong": no-op
    }
  }

  // ---------- base64 helpers ----------
  function bufferToB64(buffer) {
    const bytes = new Uint8Array(buffer);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  }

  function b64ToBuffer(b64) {
    const bin = atob(b64);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    return bytes.buffer;
  }

  // ---------- Audio + connection ----------
  async function start() {
    setStatus("Connecting…", "wait");
    micStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });

    playerCtx = new AudioContext({ sampleRate: 24000 });
    await playerCtx.audioWorklet.addModule("/static/pcm-player.js");
    playerNode = new AudioWorkletNode(playerCtx, "pcm-player");
    playerNode.connect(playerCtx.destination);

    recorderCtx = new AudioContext({ sampleRate: 16000 });
    await recorderCtx.audioWorklet.addModule("/static/pcm-recorder.js");
    const source = recorderCtx.createMediaStreamSource(micStream);
    const recorder = new AudioWorkletNode(recorderCtx, "pcm-recorder");
    source.connect(recorder);

    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    recorder.port.onmessage = (e) => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "audio", data: bufferToB64(e.data) }));
      }
    };

    ws.onopen = () => {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
      ws.send(JSON.stringify({ type: "init", timezone, caller: callerSelect.value }));
      setConnected(true);
      setStatus("Listening", "on");
      agentPill.textContent = "voice_assistant";
    };
    ws.onmessage = (e) => {
      try {
        onEvent(JSON.parse(e.data));
      } catch (err) {
        console.warn("Bad server message");
      }
    };
    ws.onclose = () => {
      if (endingCall) return; // stop() runs after the goodbye has played
      stop();
      setStatus("Disconnected", "");
    };
  }

  function stop() {
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    ws = null;
    if (micStream) micStream.getTracks().forEach((t) => t.stop());
    micStream = null;
    if (recorderCtx) recorderCtx.close();
    if (playerCtx) playerCtx.close();
    recorderCtx = playerCtx = playerNode = null;
    closeBubble("user");
    closeBubble("agent");
    setConnected(false);
    setStatus("Not connected", "");
  }

  // ---------- Handover card (end_session) ----------
  const LABELS = {
    intent: "Intent", summary: "Summary", language: "Language", use_case: "Use case",
    destination: "Destination", pass: "Pass", customer_request: "Customer wanted",
  };

  function showHandover({ session_escalated, reason, params, agent }) {
    handoverEl.hidden = false;
    handoverEl.className = "handover " + (session_escalated ? "escalated" : "contained");
    handoverTitle.textContent = session_escalated
      ? "Escalated to a person (Genesys would route to a queue)"
      : "Completed by the bot";
    handoverBody.replaceChildren();
    const rows = [["Reason", reason], ["Ended by", agent]];
    for (const [k, label] of Object.entries(LABELS)) {
      if (params && params[k]) rows.push([label, params[k]]);
    }
    for (const [k, v] of rows) {
      if (!v) continue;
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = String(v);
      handoverBody.append(dt, dd);
    }
    addActivity("handover", session_escalated ? "end_session: escalated" : "end_session: completed");
  }

  // ---------- Test callers ----------
  function showHint() {
    const c = callers.find((x) => x.key === callerSelect.value);
    callerHint.textContent = c ? "Try: " + c.hint : "";
  }

  async function loadCallers() {
    try {
      const res = await fetch("/api/test-callers");
      const data = await res.json();
      callers = data.callers || [];
      modePill.textContent = `routing: ${data.routing_mode} · gateway: ${data.gateway}`;
      if (data.gateway === "mock") failBtn.dataset.enabled = "1";
    } catch (err) {
      callers = [];
    }
    callerSelect.replaceChildren();
    for (const c of callers) {
      const opt = document.createElement("option");
      opt.value = c.key;
      opt.textContent = c.label;
      callerSelect.appendChild(opt);
    }
    if (!callers.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No test callers (withheld)";
      callerSelect.appendChild(opt);
    }
    showHint();
  }

  callerSelect.addEventListener("change", showHint);

  failBtn.addEventListener("click", () => {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "mock", action: "fail_next", operation: "*", count: 2 }));
    addActivity("error", "Mock gateway: next 2 roaming calls will fail");
  });

  micBtn.addEventListener("click", async () => {
    if (ws) return stop();
    handoverEl.hidden = true;
    try {
      await start();
    } catch (err) {
      stop();
      setStatus("Mic/connection failed", "");
      addActivity("error", "Could not start: " + (err && err.name ? err.name : "unknown error"));
    }
  });

  textForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = textInput.value.trim();
    if (!text || !ws || ws.readyState !== WebSocket.OPEN) return;
    closeBubble("agent");
    const bubble = newBubble("user", "You (typed)");
    bubble.querySelector(".body").textContent = text;
    bubble.classList.remove("live");
    ws.send(JSON.stringify({ type: "text", data: text }));
    textInput.value = "";
  });
  loadCallers();
})();
