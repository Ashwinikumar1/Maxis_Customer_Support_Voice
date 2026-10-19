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
          addActivity("tool", `${msg.name} → ${r.status || (r.error ? "error" : "done")}`);
        }
        break;
      }
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
      ws.send(JSON.stringify({ type: "init", timezone }));
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

  micBtn.addEventListener("click", async () => {
    if (ws) return stop();
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
})();
