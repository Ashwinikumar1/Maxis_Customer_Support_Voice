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

  def __init__(self, session_id: str):
    self.session_id = session_id
    self.span = None
    self.start_ns = 0
    self.got_audio = False
    self.trigger = "model"

  def start(self, agent: str = "", trigger: str = "model"):
    # [#5] `trigger` is fixed by whoever opens the turn FIRST. If the agent has
    # already started speaking (a `model` turn is open) when the caller's final
    # transcript arrives, the turn stays `model` - its true start time is
    # unknown, so it is left out of the metric rather than recorded wrongly.
    if self.span is None:
      self.start_ns = time.time_ns()
      self.got_audio = False
      self.trigger = trigger
      self.span = _tracer.start_span("model_turn", start_time=self.start_ns, attributes={
          "session.id": self.session_id, "gen_ai.agent.name": agent, "voice.turn_trigger": trigger})

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

  def usage(self, usage):
    if self.span is not None and usage:
      if usage.prompt_token_count:
        self.span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_token_count)
      if usage.candidates_token_count:
        self.span.set_attribute("gen_ai.usage.output_tokens", usage.candidates_token_count)

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
    self.turn = _TurnTracker(session_id)  # OTel: one "model_turn" span per reply
    self.resumption_updates = 0           # Live API session resumption updates seen

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

    turn.usage(event.usage_metadata)

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
