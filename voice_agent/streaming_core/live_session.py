"""Channel-agnostic Gemini Live session.

The server feeds client input in (`send_audio` / `send_text`) and receives
everything the model does through one async callback `emit(type, **data)`:

  audio(data=pcm16_24k) transcript(role, text, partial, author)
  tool_call(name, args, author) tool_result(name, response, author)
  active_agent(name) interrupted() turn_complete(state)

Today the browser WebSocket uses it; a telephony gateway (e.g. Genesys) can
reuse it later by writing its own `emit` + audio conversion.

How the pieces fit:

    client ──send_audio/send_text──▶ LiveRequestQueue ──▶ runner.run_live() ──▶ Gemini Live
    client ◀──────── emit(...) ◀──────── run() loop ◀──────── ADK Events ◀────────┘
"""

import time

from google.adk.agents import LiveRequestQueue
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types
from opentelemetry import metrics, trace

from .gcs_logger import TranscriptLogger
from .utils import SEND_SAMPLE_RATE, logger

MAX_TEXT_LEN = 2000  # cap typed messages so a client can't flood the model

# ADK's live mode only emits invoke_agent / execute_tool spans, not one per model
# reply, so we add a "model_turn" span and a time-to-first-audio metric here.
_tracer = trace.get_tracer("maxis_voice_agent.live")
_meter = metrics.get_meter("maxis_voice_agent.live")
# [Observability #5] voice.time_to_first_audio = how long a caller waits, after
# they finish speaking (or send typed text), before the agent starts talking.
# It is the delay the caller actually hears, so it is THE speed metric for a
# voice agent: service-level targets and alerts (e.g. "95% of answers start
# within 1.5 s") are built on it. It must therefore contain only REAL waits -
# see _TurnTracker.first_audio() for the rule that guarantees that.
# Honest limitation: the clock starts when the FINAL transcript of the caller's
# speech arrives from Gemini, which is slightly after they actually stopped
# talking, so real waits are a little longer than this metric shows.
_ttfa = _meter.create_histogram(
    "voice.time_to_first_audio", unit="ms",
    description="Final user transcript (or typed text) received -> first model audio chunk. "
                "Model-initiated turns (greeting, continuation after a tool) are not recorded.")
_turns = _meter.create_counter("voice.model_turns", description="Model turns by outcome")


def default_run_config(voice_name: str) -> RunConfig:
  """Live API settings for a voice conversation.

  - streaming_mode=BIDI            : full-duplex streaming (talk and listen at once)
  - response_modalities=[AUDIO]    : the model answers with speech
  - speech_config                  : which prebuilt voice speaks (e.g. "Kore")
  - input/output_audio_transcription: also return text of what the user said and
                                     what the model said (for UI + transcript log)
  - session_resumption             : ADK transparently reconnects when Gemini closes
                                     the socket (~10 min limit) without losing context
  - context_window_compression     : sliding window so long calls don't hit the
                                     context limit
  """
  return RunConfig(
      streaming_mode=StreamingMode.BIDI,
      response_modalities=[types.Modality.AUDIO],
      speech_config=types.SpeechConfig(
          voice_config=types.VoiceConfig(
              prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
          )
      ),
      input_audio_transcription=types.AudioTranscriptionConfig(),
      output_audio_transcription=types.AudioTranscriptionConfig(),
      session_resumption=types.SessionResumptionConfig(),
      context_window_compression=types.ContextWindowCompressionConfig(
          sliding_window=types.SlidingWindow()
      ),
  )

# [Observability #12] Tokens used per call, recorded once when the call ends.
# One histogram sample per call and per token type, so Cloud Monitoring can
# show both the distribution (p50 / p95 tokens per call) and the sum (total
# tokens across all calls) - the basis for cost per call.
_call_tokens = _meter.create_histogram(
    "voice.call.tokens", unit="{token}",
    description="Tokens used by one call, by type (input / output / total). "
                "Recorded at the end of the call from the Live API's usage reports.")


class _CallTokenUsage:
  """[Observability #12] Running token totals for ONE call.

  WHY THIS IS CODE AND NOT A GOOGLE CLOUD SETTING (checked before writing)
    - ADK's own token metric (gen_ai.client.token.usage) is NOT recorded for
      live voice calls: in ADK 2.12 only the text request path
      (flows/llm_flows/core) calls the token telemetry; google/adk/live/ never
      does. For this agent that metric stays empty.
    - Vertex AI publishes aiplatform.googleapis.com/publisher/online_serving/
      token_count in Cloud Monitoring, but only as project-wide totals per
      model (by input/output and modality) - it cannot say which CALL used
      the tokens. https://docs.cloud.google.com/monitoring/api/metrics_gcp_a_b
    So a per-call figure has to be built here, from the token reports the
    Live API sends during the call.

  WHAT THE LIVE API SENDS - AND WHY WE DO NOT SIMPLY ADD THE REPORTS UP
    Each usage report is "a report of the number of tokens used by the
    session so far" - a RUNNING TOTAL, not the tokens of one message.
    https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/models/live-api
    Adding the reports together would count the early part of the call again
    with every new report and grossly overstate usage. The correct per-call
    figure is the LATEST report. ADK passes each report through unchanged:
    google/adk/models/gemini_llm_connection.py turns it into an event, and
    flows/llm_flows/core/_finalizer.py copies usage_metadata onto that event.

  THE ONE CASE THE DOCS DO NOT COVER: A COUNTER RESTART
    Gemini Live connections last about 10 minutes, and ADK reconnects long
    calls using session resumption. The docs do not say whether the running
    total continues or starts again from zero on the new connection. So if a
    report is ever LOWER than the previous one, we treat it as a restart: the
    previous connection's last figures are kept ("carried") and the new
    running total is added on top. If the counter never restarts, this branch
    simply never runs. `counter_resets` records how often it happened, so a
    real call can show which behaviour Gemini actually has.

  WHAT IS COUNTED (names from the API, mapped to plain keys)
    input            prompt_token_count   - everything the model read (caller
                                            audio, system prompt, history, ...)
    output           candidates_token_count - what the model produced (speech)
    thoughts         thoughts_token_count - internal reasoning, if any
    tool_use_prompt  tool_use_prompt_token_count - tool results fed back in
    cached           cached_content_token_count - input served from cache
    total            total_token_count    - the API's own total
    input.<MODALITY> / output.<MODALITY>  - the same split by AUDIO / TEXT /
                                            ...: Google prices audio and text
                                            tokens differently, so cost needs it
  """

  _FIELDS = {
      "input": "prompt_token_count",
      "output": "candidates_token_count",
      "thoughts": "thoughts_token_count",
      "tool_use_prompt": "tool_use_prompt_token_count",
      "cached": "cached_content_token_count",
      "total": "total_token_count",
  }
  _DETAILS = {"input": "prompt_tokens_details", "output": "candidates_tokens_details"}

  def __init__(self):
    self.reports = 0         # usage reports received during the call
    self.counter_resets = 0  # times the running total started again (see above)
    self._latest = {}        # the most recent report, as plain keys
    self._carried = {}       # final figures of earlier connections, if any restart happened

  @classmethod
  def _flatten(cls, usage) -> dict:
    """One usage report -> {"input": n, "output": n, ..., "input.AUDIO": n, ...}."""
    values = {key: getattr(usage, field, None) or 0 for key, field in cls._FIELDS.items()}
    for key, field in cls._DETAILS.items():
      for detail in getattr(usage, field, None) or []:
        modality = getattr(getattr(detail, "modality", None), "value", None) or "UNSPECIFIED"
        values[f"{key}.{modality}"] = values.get(f"{key}.{modality}", 0) + (detail.token_count or 0)
    if not values["total"]:  # some reports may omit the total: rebuild it from its parts
      values["total"] = values["input"] + values["output"] + values["thoughts"] + values["tool_use_prompt"]
    return values

  def update(self, usage) -> None:
    """Takes one running-total report from the Live API."""
    values = self._flatten(usage)
    if self._latest and values["total"] < self._latest["total"]:
      # The running total went DOWN: the counter restarted (see class docstring).
      for key, value in self._latest.items():
        self._carried[key] = self._carried.get(key, 0) + value
      self.counter_resets += 1
    self._latest = values
    self.reports += 1

  def totals(self) -> dict:
    """Tokens used by the call so far: latest report + anything carried over."""
    keys = set(self._latest) | set(self._carried)
    return {key: self._carried.get(key, 0) + self._latest.get(key, 0) for key in sorted(keys)}


class _TurnTracker:
  """One OTel span per model reply ("model_turn"), plus voice latency metrics.

  The turn starts when the user's speech is final, when typed text is sent, or
  (greeting, continuation after a tool) at the first model output, and ends on
  turn_complete or barge-in. `trigger` records which: only user-triggered
  turns have a real start time, so only they feed voice.time_to_first_audio.

  [Observability #5] WHY `trigger` EXISTS
    A turn can be started in three ways, and they are NOT equal:
      user_speech - the caller finished speaking  -> a person IS waiting
      user_text   - the caller sent typed text    -> a person IS waiting
      model       - the agent speaks on its own: the greeting at the start of
                    the call, or carrying on after "let me check that" and an
                    FAQ search                    -> NOBODY is waiting
    For a `model` turn the stopwatch is started by the agent's first audio
    itself, so start and stop happen at the same instant and the "wait" reads
    ~0 ms. The old code recorded those 0 ms values too. Mixed into the
    histogram they made p50 / p95 look better than calls really are (example:
    three real waits of 2 s plus two fake 0 s waits averaged 1.2 s instead of
    2 s), which could hide a real slowdown. Now only user-triggered turns are
    recorded. `model` turns are still traced (their model_turn span exists,
    labelled voice.turn_trigger="model"); they just stay out of the metric.
    The time spent inside an FAQ search is measured separately
    (voice.faq_search.duration, from the observability add-on).
  """

  # Turns where a real person is waiting - the only ones whose wait is measured.
  _USER_TRIGGERS = ("user_speech", "user_text")

  def __init__(self, session_id: str, tokens: "_CallTokenUsage | None" = None):
    self.session_id = session_id
    self.span = None
    self.start_ns = 0
    self.got_audio = False
    self.trigger = "model"
    # [#12] The call's token totals, and their value when this turn started:
    # the difference is what this turn used (see usage()).
    self.tokens = tokens
    self.tokens_at_start = {}
    self.agent = ""  # gen_ai.agent.name of the open turn, once known

  def start(self, agent: str = "", trigger: str = "model"):
    # [#5] `trigger` is fixed by whoever opens the turn FIRST. If the agent has
    # already started speaking (a `model` turn is open) when the caller's final
    # transcript arrives, the turn stays `model` - its true start time is
    # unknown, so it is left out of the metric rather than recorded wrongly.
    if self.span is None:
      self.start_ns = time.time_ns()
      self.got_audio = False
      self.trigger = trigger
      self.tokens_at_start = self.tokens.totals() if self.tokens else {}  # [#12] baseline for this turn
      self.span = _tracer.start_span("model_turn", start_time=self.start_ns, attributes={
          "session.id": self.session_id, "voice.turn_trigger": trigger})
      self.agent = ""
    self.set_agent(agent)

  def set_agent(self, agent: str):
    """Names the agent answering this turn (gen_ai.agent.name), once it is known.

    WHY: a typed question opens its turn in send_text(), before the model has
    replied, so the agent's name is not known yet. Found in Cloud Trace on
    22 Oct 2026: such turns showed an EMPTY gen_ai.agent.name. The name is now
    set by the first agent event of the turn (speech, tool call, ...) instead,
    and an empty value is never written. Once transfers to other agents exist,
    this shows which agent handled each turn.
    """
    if self.span is not None and agent and agent != "user" and not self.agent:
      self.agent = agent
      self.span.set_attribute("gen_ai.agent.name", agent)

  def first_audio(self, agent: str):
    self.start(agent)
    if not self.got_audio:
      self.got_audio = True
      self.span.add_event("first_audio")
      # [#5] THE RULE: record the wait only when a person was actually waiting.
      # A model-initiated turn starts AT its first output, so its latency would
      # read ~0 ms and drag the histogram down: record user-triggered turns only.
      # The `trigger` label on each sample lets dashboards show spoken and typed
      # turns separately, since they reach the model by different paths
      # (streamed audio vs. a complete text message).
      if self.trigger in self._USER_TRIGGERS:
        ms = (time.time_ns() - self.start_ns) / 1e6
        self.span.set_attribute("voice.time_to_first_audio_ms", round(ms))
        _ttfa.record(ms, {"agent": agent, "trigger": self.trigger})

  def event(self, name: str, **attrs):
    if self.span is not None:
      self.span.add_event(name, attrs)

  def usage(self):
    """[#12] Tokens used during THIS turn, on the turn's span.

    WHAT CHANGED AND WHY: the old code copied each usage report straight onto
    the turn as gen_ai.usage.input_tokens / output_tokens. Because the Live
    API reports a RUNNING TOTAL for the whole session (see _CallTokenUsage),
    the tenth turn of a call showed the tokens of all ten turns - wrong, and
    misleading for anyone comparing turns. Now the turn shows the increase
    since it started: call totals now minus call totals at turn start.
    Limitation: a report that arrives after the turn has ended is counted in
    the next turn. The per-call total is not affected by this.
    """
    if self.span is None or self.tokens is None:
      return
    now = self.tokens.totals()
    for key, attribute in (("input", "gen_ai.usage.input_tokens"), ("output", "gen_ai.usage.output_tokens")):
      used = now.get(key, 0) - self.tokens_at_start.get(key, 0)
      if used > 0:
        self.span.set_attribute(attribute, used)

  def end(self, outcome: str):
    if self.span is not None:
      self.span.set_attribute("voice.turn_outcome", outcome)
      self.span.end()
      _turns.add(1, {"outcome": outcome})
      self.span = None


class LiveVoiceSession:
  """One live conversation between a client and the ADK agent tree."""

  def __init__(self, *, runner, user_id: str, session_id: str, run_config: RunConfig,
               transcript: TranscriptLogger):
    self.runner = runner              # ADK Runner bound to the agent tree + SessionService
    self.user_id = user_id
    self.session_id = session_id
    self.run_config = run_config
    self.transcript = transcript      # collects turns; saved to GCS by the server at the end
    # LiveRequestQueue = the "inbox" for the model. Anything put here is streamed
    # to Gemini Live by run_live(). A NEW queue per conversation (never reuse).
    self.queue = LiveRequestQueue()
    self.tokens = _CallTokenUsage()       # [#12] token totals for this call
    self.turn = _TurnTracker(session_id, self.tokens)  # OTel: one "model_turn" span per reply
    self.resumption_updates = 0           # Live API session resumption updates seen
    self._tokens_recorded = False         # [#12] the per-call metric is recorded once

  def record_token_usage(self) -> dict:
    """[Observability #12] Final token totals of this call; records the metric once.

    Called by server.py when the call ends, BEFORE the transcript is saved, so
    the same figures go to the call's trace, its closing log line, its
    transcript and the voice.call.tokens metric - one number, everywhere.
    Returns {} when the Live API sent no usage report (e.g. the call failed
    before the model answered): recording zeros would wrongly pull the
    tokens-per-call average down, so nothing is recorded in that case.
    """
    if not self.tokens.reports:
      return {}
    totals = {**self.tokens.totals(), "reports": self.tokens.reports,
              "counter_resets": self.tokens.counter_resets}
    if not self._tokens_recorded:
      self._tokens_recorded = True
      for kind in ("input", "output", "total"):
        _call_tokens.record(totals.get(kind, 0), {"type": kind})
    return totals

  # ===========================================================================
  # Client -> model (called by the server's upstream task)
  # ===========================================================================
  def send_audio(self, pcm16: bytes, rate: int = SEND_SAMPLE_RATE):
    """Streams a raw mic chunk (PCM16 mono, default 16 kHz) to Gemini.

    send_realtime() is for continuous media: Gemini's voice activity detection
    decides when the user has finished speaking, and new speech while the
    model is talking triggers an interruption (barge-in).
    """
    if pcm16:
      self.queue.send_realtime(types.Blob(data=pcm16, mime_type=f"audio/pcm;rate={rate}"))

  def send_text(self, text: str):
    """Sends a typed message as a complete user turn (send_content = discrete turn)."""
    text = (text or "").strip()[:MAX_TEXT_LEN]
    if text:
      self.transcript.add_turn("User", text)  # typed text has no audio transcription, log it here
      self.turn.start(trigger="user_text")  # typed turn: the latency clock starts now
      self.queue.send_content(types.Content(role="user", parts=[types.Part(text=text)]))

  def close(self):
    """Ends the live stream; run_live() finishes and the Gemini connection closes."""
    self.queue.close()

  # ===========================================================================
  # Model -> client (the server runs this as the downstream task)
  # ===========================================================================
  async def run(self, emit):
    """Consumes ADK events from run_live() and forwards them via emit().

    One ADK Event can carry several things at once (audio + transcription +
    turn flags), so each block below checks its own field independently.
    """
    current_agent = None
    turn = self.turn
    try:
      async for event in self.runner.run_live(
          user_id=self.user_id, session_id=self.session_id,
          live_request_queue=self.queue, run_config=self.run_config):
        await self._handle(event, emit, turn, current_agent)
        if event.author and event.author != "user":
          current_agent = event.author
    finally:
      turn.end("session_closed")

  async def _handle(self, event, emit, turn: _TurnTracker, current_agent):
    """Forwards one ADK event to the client and records it in telemetry."""
    # --- Which agent is speaking? event.author is the agent name. It changes
    # after transfer_to_agent (e.g. maxis_agent -> roaming_agent).
    author = event.author or ""
    if author and author != "user" and author != current_agent:
      await emit("active_agent", name=author)
    turn.set_agent(author)  # names a turn opened before the agent was known (e.g. typed text)

    # --- Tool calls the model decided to make (e.g. search_knowledge_base).
    # ADK executes the tool itself; we only report it to the UI and the log.
    for call in event.get_function_calls():
      args = dict(call.args or {})
      turn.event("tool_call", tool=call.name)
      await emit("tool_call", name=call.name, args=args, author=author)
      self.transcript.add_event("tool_call", name=call.name, args=args, agent=author)

    # --- Tool results returned to the model.
    for resp in event.get_function_responses():
      turn.event("tool_result", tool=resp.name)
      await emit("tool_result", name=resp.name, response=resp.response or {}, author=author)

    # --- Model speech: PCM16 24 kHz audio chunks, streamed as they're generated.
    if event.content and event.content.parts:
      for part in event.content.parts:
        blob = part.inline_data
        if blob and blob.data and (blob.mime_type or "").startswith("audio/"):
          turn.first_audio(author)
          await emit("audio", data=blob.data)

    # --- Live transcripts of both sides.
    # Partial events carry small text chunks while speech is in progress (for
    # the live UI); the final event carries the full utterance, which is the
    # only one we write to the transcript log (avoids duplicates).
    for role, tr in (("user", event.input_transcription), ("agent", event.output_transcription)):
      if tr and tr.text:
        partial = bool(event.partial) and not tr.finished
        await emit("transcript", role=role, text=tr.text, partial=partial, author=author)
        if not partial:
          speaker = "User" if role == "user" else "Assistant"
          self.transcript.add_turn(speaker, tr.text, agent=None if role == "user" else author)
          if role == "user":
            turn.start(current_agent or "", trigger="user_speech")  # user finished speaking: the clock starts

    # --- [#12] Token usage. The Live API reports the session's RUNNING total;
    # _CallTokenUsage keeps the call's figures, the turn records its share.
    if event.usage_metadata:
      self.tokens.update(event.usage_metadata)
      turn.usage()

    # --- [Observability #10] Live API session resumption.
    # BACKGROUND: one connection to the Gemini Live API lasts only about 10
    # minutes. To keep longer calls going, Gemini periodically sends a
    # "session resumption update" carrying a handle; when the connection is
    # about to close, it sends a GoAway message and ADK reconnects using the
    # latest handle, so the conversation continues with its context.
    # Source: https://docs.cloud.google.com/gemini-enterprise-agent-platform/
    #         models/live-api/start-manage-session ("Session lifetime")
    # WHY WE RECORD IT: if a caller reports a pause mid-call, or the agent
    # "forgetting" what was said, the call's trace now shows how many resumption
    # updates the call had (live_api.resumption_updates) and whether the session
    # was still resumable (live_api.resumable) - pointing at a reconnect.
    # WHY THE HANDLE ITSELF IS NEVER RECORDED: the handle lets a new connection
    # take over this conversation - it behaves like a temporary credential.
    # It must not end up in traces or logs. (ADK itself logs it at INFO; the
    # observability add-on redacts it from that log line.)
    # WHY THERE IS NO GoAway CHECK HERE: verified in ADK 2.12's source
    # (google/adk/live/_live_llm_flow.py) - ADK consumes GoAway internally,
    # logs "Received go away signal" at INFO and reconnects; the GoAway event
    # never reaches this loop, so a check here would never run.
    # WHY ON THE CALL SPAN, AS A COUNT: these updates can arrive many times in a
    # long call. One counter attribute on voice_session stays small, whereas an
    # event per update could exceed Cloud Trace's limit of 256 events per span.
    update = event.live_session_resumption_update
    if update:
      self.resumption_updates += 1
      span = trace.get_current_span()  # the call's voice_session span
      span.set_attribute("live_api.resumption_updates", self.resumption_updates)
      span.set_attribute("live_api.resumable", bool(update.resumable))
      # DEBUG, not INFO: it can repeat often, and ADK already logs it at INFO.
      logger.debug("Live API session resumption update (resumable=%s)", update.resumable)

    # --- Barge-in: the user started talking over the model. The client must
    # immediately drop any audio it has queued but not yet played.
    if event.interrupted:
      turn.end("interrupted")
      await emit("interrupted")

    # --- End of a model turn. With gemini-3.8-live, turn_complete alone does
    # not mean "idle": interaction_status says IN_PROGRESS (more output may
    # follow, e.g. after a tool) or IDLE (done). Older models send no status,
    # so we treat a missing status as IDLE.
    if event.turn_complete:
      status = event.interaction_status
      state = status.value if status else "IDLE"
      turn.end(state.lower())
      await emit("turn_complete", state=state)
