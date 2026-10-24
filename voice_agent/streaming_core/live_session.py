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
from .utils import SEND_SAMPLE_RATE

MAX_TEXT_LEN = 2000  # cap typed messages so a client can't flood the model

# ADK's live mode only emits invoke_agent / execute_tool spans, not one per model
# reply, so we add a "model_turn" span and a time-to-first-audio metric here.
_tracer = trace.get_tracer("maxis_voice_agent.live")
_meter = metrics.get_meter("maxis_voice_agent.live")
_ttfa = _meter.create_histogram(
    "voice.time_to_first_audio", unit="ms",
    description="User stops speaking (or turn starts) -> first model audio chunk")
_turns = _meter.create_counter("voice.model_turns", description="Model turns by outcome")


def default_run_config(voice_name: str, language_code: str | None = None) -> RunConfig:
  """Live API settings for a voice conversation.

  - streaming_mode=BIDI            : full-duplex streaming (talk and listen at once)
  - response_modalities=[AUDIO]    : the model answers with speech
  - speech_config                  : which prebuilt voice speaks (e.g. "Kore") and,
                                     optionally, the BCP-47 language_code of the
                                     speech (e.g. "en-US", "ms-MY"). None/empty =
                                     model auto-detects. Native-audio models may
                                     treat it as a hint only; the system prompt
                                     remains the strongest language control.
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
          ),
          language_code=language_code or None,
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

  The turn starts when the user's speech is final (or, for the greeting / typed
  text, at the first model output) and ends on turn_complete or barge-in.
  """

  def __init__(self, session_id: str):
    self.session_id = session_id
    self.span = None
    self.start_ns = 0
    self.got_audio = False

  def start(self, agent: str = ""):
    if self.span is None:
      self.start_ns = time.time_ns()
      self.got_audio = False
      self.span = _tracer.start_span("model_turn", start_time=self.start_ns, attributes={
          "session.id": self.session_id, "gen_ai.agent.name": agent})

  def first_audio(self, agent: str):
    self.start(agent)
    if not self.got_audio:
      self.got_audio = True
      ms = (time.time_ns() - self.start_ns) / 1e6
      self.span.set_attribute("voice.time_to_first_audio_ms", round(ms))
      self.span.add_event("first_audio")
      _ttfa.record(ms, {"agent": agent})

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
      self.turn.start()  # typed turn: the latency clock starts now
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
            turn.start(current_agent or "")  # user finished speaking: the clock starts

    turn.usage(event.usage_metadata)

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
