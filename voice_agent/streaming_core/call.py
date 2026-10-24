"""One conversation, independent of the channel that carries its audio.

A channel adapter (browser WebSocket today, Genesys Audio Connector next) only:
  1. builds a CallInfo from its own handshake: MSISDN, conversation id,
     timezone and any channel variables (e.g. Genesys inputVariables);
  2. converts audio to PCM16 16 kHz for call.send_audio() and plays the PCM16
     24 kHz it receives (streaming_core/audio.py has mu-law / resampling helpers);
  3. renders emit(type, **data) events in its own protocol, including
     "call_outcome" (action=handoff|end), which it turns into its own action
     (Genesys: a "disconnect" message with outputVariables).

Everything else (ADK session, agent tree, transcript, outcome detection) lives here.

    channel ──CallInfo──▶ Call.start() ──▶ ADK session (user_id=MSISDN, session_id=conversation id)
    channel ──send_audio/send_text──▶ Call ──▶ LiveVoiceSession ──▶ Gemini Live
    channel ◀──emit(type, **data)── Call ◀── LiveVoiceSession ◀── ADK events
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from google.adk.runners import Runner

from .gcs_logger import TranscriptLogger
from .live_session import LiveVoiceSession
from .utils import SEND_SAMPLE_RATE, get_datetime_info

# Transcripts made only of markers like "<no speech>", "<noise>" or "{pause}".
_MARKERS_ONLY = re.compile(r"\s*(?:(?:<[^<>]{1,40}>|\{[^{}]{1,40}\})\s*)+")

# Session-state key tools write to request a handoff or the end of the call
# (see voice_assistant/shared/tools.py). Read once per finished model turn.
CALL_OUTCOME_KEY = "call_outcome"

Emit = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class CallInfo:
  """Who is calling and on which channel, known when the call starts."""
  msisdn: str                 # caller's number, digits only: ADK user_id + X-Caller-Id
  conversation_id: str        # unique per call: ADK session_id + X-Conversation-Id
  channel: str                # "browser", "genesys", ...
  timezone: str | None = None
  channel_vars: dict = field(default_factory=dict)  # e.g. Genesys inputVariables (strings)


class Call:
  """A running conversation. Create with `await Call.start(...)`."""

  def __init__(self, info: CallInfo, ctx: dict, live: LiveVoiceSession,
               transcript: TranscriptLogger, session_service, app_name: str):
    self.info = info
    self.ctx = ctx                      # {"timezone","date","time","greeting"}
    self.live = live
    self.transcript = transcript
    self._session_service = session_service
    self._app_name = app_name
    self.outcome: dict | None = None    # set once a tool requests handoff / end
    # Set when the conversation is over (end of call, or handoff after its last line).
    # From then on nothing from the model reaches the caller and input is ignored;
    # the channel hangs up (browser: after the goodbye plays; Genesys: "disconnect").
    self.ended = asyncio.Event()

  @classmethod
  async def start(cls, info: CallInfo, *, session_service, app_name: str,
                  agent_factory: Callable[[dict], object], run_config) -> "Call":
    ctx = get_datetime_info(info.timezone)
    # Identity goes into session state: tools (via the gateway client) read it from
    # there, so it's never a tool parameter the model could see or change.
    state = {**ctx, "msisdn": info.msisdn, "conversation_id": info.conversation_id,
             "channel": info.channel, "channel_vars": dict(info.channel_vars)}
    await session_service.create_session(app_name=app_name, user_id=info.msisdn,
                                         session_id=info.conversation_id, state=state)
    runner = Runner(app_name=app_name, agent=agent_factory(ctx), session_service=session_service)
    transcript = TranscriptLogger(info.conversation_id, channel=info.channel,
                                  timezone=ctx["timezone"], msisdn_last4=info.msisdn[-4:])
    live = LiveVoiceSession(runner=runner, user_id=info.msisdn, session_id=info.conversation_id,
                            run_config=run_config, transcript=transcript)
    return cls(info, ctx, live, transcript, session_service, app_name)

  # --- Channel -> model --------------------------------------------------------
  def send_audio(self, pcm16: bytes, rate: int = SEND_SAMPLE_RATE):
    if not self.ended.is_set():
      self.live.send_audio(pcm16, rate)

  def send_text(self, text: str):
    if not self.ended.is_set():
      self.live.send_text(text)

  # --- Model -> channel --------------------------------------------------------
  async def run(self, emit: Emit):
    """Streams model events to the channel until the live session ends."""
    async def forward(kind, **data):
      if kind == "transcript" and _MARKERS_ONLY.fullmatch(data.get("text") or ""):
        return  # "<no speech>", "{pause}": transcription markers, not words
      if self.ended.is_set() and kind != "transcript":
        # Call is over: no more audio (e.g. the model's "<no speech>{pause}" reply
        # after endCall). Text still passes: the goodbye's transcript lags its audio.
        return
      await emit(kind, **data)
      if self.outcome is None and kind in ("tool_result", "turn_complete"):
        await self._check_outcome(kind, data, emit)

    await self.live.run(forward)

  async def _check_outcome(self, kind: str, data: dict, emit: Emit):
    """endCall: over as soon as the tool runs (the goodbye was said before it).
    Handoff: over once the "connecting you" reply is finished."""
    finished_reply = kind == "turn_complete" and data.get("state", "IDLE") == "IDLE"
    outcome = await self._read_outcome()
    if not outcome or (outcome.get("action") != "end" and not finished_reply):
      return
    self.outcome = outcome
    self.transcript.add_event("call_outcome", **outcome)
    await emit("call_outcome", **outcome)
    self.ended.set()

  async def _read_outcome(self) -> dict | None:
    session = await self._session_service.get_session(
        app_name=self._app_name, user_id=self.info.msisdn, session_id=self.info.conversation_id)
    outcome = (session.state or {}).get(CALL_OUTCOME_KEY) if session else None
    return dict(outcome) if isinstance(outcome, dict) else None

  async def finish(self):
    """Closes the live connection and saves the transcript (once)."""
    self.live.close()
    await self.transcript.save()
