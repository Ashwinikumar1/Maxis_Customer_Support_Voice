"""Prompt-injection flagging (baseline AP-09, MA-02, MA-10, MA-13).

Two screens run on what the caller said (in live mode, ADK screens each final input
transcription and typed message through before_model_callback):

1. InjectionFlagPlugin: a fast local check for well-known override and extraction phrasings in
   English, Malay and mixed speech. It never blocks the call; it raises the injection flag.
2. Model Armor (ADK's ModelArmorPlugin), when templates are configured. In MODEL_ARMOR_MODE=inspect
   (the default for the first weeks) a match raises the flag and is logged but the call goes on;
   in MODEL_ARMOR_MODE=block a match also replaces the reply with a refusal.

Either way, a raised flag makes the tool policy refuse every write tool for the rest of the call.
Model Armor screens text only, after the live model has already heard the audio, so prevention
stays in the tool policy; these screens detect and contain.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from google.adk.plugins.base_plugin import BasePlugin

from .tool_policy import K_INJECTION, flag_injection

logger = logging.getLogger("voice_agent.guardrails")

# Each pattern names an attack category used in security events and the evaluation set.
PATTERNS: dict[str, re.Pattern] = {
    "instruction_override": re.compile(
        r"\b(ignore|forget|disregard|override)\b.{0,30}\b(instructions?|rules?|prompt|guidelines|"
        r"everything (you were|i) told)\b"
        r"|\b(abaikan|lupakan|langgar)\b.{0,30}\b(arahan|peraturan|prompt)\b",
        re.I),
    "mode_switch": re.compile(
        r"\b(developer|debug|maintenance|admin|god|jailbreak|unrestricted)\s+mode\b"
        r"|\bmod\s+(pembangun|penyelenggaraan|admin)\b|\byou are now\b|\bkamu sekarang\b",
        re.I),
    "prompt_extraction": re.compile(
        r"\b(system prompt|your (instructions|prompt|rules)|repeat (everything|the text) above|"
        r"what (were|are) you told)\b"
        r"|\b(arahan sistem|arahan anda|ulang semua (di atas|arahan))\b",
        re.I),
    "fake_tool_output": re.compile(
        r"\b(tool (response|output|result)|next_step\s*[:=]|confirmed\s*(=|equals|is)\s*true|"
        r"status\s*[:=]\s*success)\b",
        re.I),
    "fake_authority": re.compile(
        r"\b(new rule from|system update|message from (maxis )?(management|it|security))\b"
        r"|\b(peraturan baru|kemas kini sistem)\b",
        re.I),
}


def classify(text: str) -> Optional[str]:
  """Returns the first matching attack category, or None."""
  for category, pattern in PATTERNS.items():
    if pattern.search(text or ""):
      return category
  return None


def _latest_user_text(llm_request) -> str:
  for content in reversed(llm_request.contents or []):
    if content.role in (None, "user") and content.parts:
      text = " ".join(p.text for p in content.parts if getattr(p, "text", None))
      if text:
        return text
  return ""


class InjectionFlagPlugin(BasePlugin):
  """Flags, never blocks: a false positive costs only write tools for this call."""

  def __init__(self, name: str = "injection_flag"):
    super().__init__(name)

  async def before_model_callback(self, *, callback_context, llm_request):
    category = classify(_latest_user_text(llm_request))
    if category:
      flag_injection(callback_context.state, source="heuristic", category=category)
    return None


def build_model_armor_plugin():
  """ADK's Model Armor plugin, wrapped to raise the injection flag; None when not configured.

  MODEL_ARMOR_PROMPT_TEMPLATE / MODEL_ARMOR_RESPONSE_TEMPLATE take full template names
  (projects/P/locations/L/templates/T, from security/terraform/modules/model-armor).
  """
  prompt = os.getenv("MODEL_ARMOR_PROMPT_TEMPLATE", "").strip() or None
  response = os.getenv("MODEL_ARMOR_RESPONSE_TEMPLATE", "").strip() or None
  if not prompt and not response:
    return None
  mode = os.getenv("MODEL_ARMOR_MODE", "inspect").lower()
  if mode not in ("inspect", "block"):
    raise ValueError("MODEL_ARMOR_MODE must be inspect or block")

  # Configured but not installed is a deployment error: fail at startup, not silently.
  from google.adk.integrations.model_armor import ModelArmorConfig, ModelArmorPlugin

  screening = os.getenv("MODEL_ARMOR_OUTPUT_SCREENING", "sentence").lower()
  if screening not in ("sentence", "chunk"):
    raise ValueError("MODEL_ARMOR_OUTPUT_SCREENING must be sentence or chunk")

  class FlaggingModelArmorPlugin(ModelArmorPlugin):
    """ADK's Model Armor plugin, plus flagging and live-mode output throttling.

    In live mode ADK calls after_model_callback on *every* output-transcription chunk, each
    time with the whole turn's text so far (google/adk/live/_live_llm_flow.py, "Live callback
    site 2 of 3"). Screening every chunk means one Model Armor call per chunk and, in inspect
    mode, one warning event per chunk. With MODEL_ARMOR_OUTPUT_SCREENING=sentence (default)
    the text is screened when a sentence ends or 200 more characters have arrived, and the
    flag is raised at most once per turn. `chunk` screens every chunk (earliest block, highest
    cost). Either way the caller may already have heard the audio: Model Armor screens the
    transcription, not the audio stream (ADK docs: Model Armor integration).
    """

    def __init__(self, *args, **kwargs):
      super().__init__(*args, **kwargs)
      # invocation_id -> (length of text last screened, already flagged this turn)
      self._turns: dict[str, tuple[int, bool]] = {}

    async def before_model_callback(self, *, callback_context, llm_request):
      blocked = await super().before_model_callback(callback_context=callback_context, llm_request=llm_request)
      if blocked is not None:
        flag_injection(callback_context.state, source="model_armor", category="input_match")
        return blocked if mode == "block" else None
      return None

    async def after_model_callback(self, *, callback_context, llm_response):
      live_text = getattr(getattr(llm_response, "output_transcription", None), "text", None)
      key = callback_context.invocation_id
      last_len, flagged = self._turns.get(key, (0, False))
      if live_text is not None:
        if len(live_text) < last_len:  # the text restarted: a new turn
          last_len, flagged = 0, False
        sentence_end = live_text.rstrip().endswith((".", "?", "!"))
        if screening == "sentence" and not sentence_end and len(live_text) - last_len < 200:
          return None  # wait for the end of the sentence; the next chunk carries this text too
        last_len = len(live_text)
      self._turns[key] = (last_len, flagged)
      blocked = await super().after_model_callback(callback_context=callback_context, llm_response=llm_response)
      if blocked is not None:
        if not flagged:
          flag_injection(callback_context.state, source="model_armor", category="output_match")
          self._turns[key] = (last_len, True)
        return blocked if mode == "block" else None
      return None

  config = ModelArmorConfig(
      prompt_template_name=prompt,
      response_template_name=response,
      block_on_screening_failure=(mode == "block"),
      input_blocked_message="Sorry, I can't help with that on this call. I can help with roaming or a general Maxis question.",
      output_blocked_message="Sorry, let me put that another way. What can I help you with about your Maxis service?",
  )
  logger.info("Model Armor screening on (mode=%s)", mode)
  return FlaggingModelArmorPlugin(config=config, name="model_armor")


def injection_flagged(state) -> bool:
  return bool(state.get(K_INJECTION))
