"""Gemini safety settings for the live agent (baseline AI-03).

Vertex AI documents the content filters as OFF by default for gemini-3.5-flash and later
models, so they must be set explicitly (docs: Vertex AI > Configure safety filters).
Set them on the agent's generate_content_config: ADK's Gemini.connect() forwards
generate_content_config.safety_settings into the live session's connect config
(google/adk/models/google_llm.py), and the Live API accepts `safety_settings` in its setup
message (Live API reference, BidiGenerateContentSetup). The test
tests/test_adk_integration.py::test_safety_settings_reach_the_live_connect_config checks that ADK
still forwards them.

`method=SEVERITY` is set explicitly because two Vertex pages disagree on the default (the
SafetySetting reference says probability, the safety-filters guide says severity). It is a
Vertex-only field: the SDK rejects it on the Gemini Developer API, which this app doesn't use.

When the model blocks a turn, Live ends it with a turn_complete_reason such as
GENERATED_AUDIO_SAFETY or PROHIBITED_INPUT_CONTENT; streaming_core/live_session.py logs a
`safety_block` security event for those.
"""

from __future__ import annotations

import os

from google.genai import types

_CATEGORIES = (
    types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
    types.HarmCategory.HARM_CATEGORY_HARASSMENT,
    types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
    types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
)


def _threshold() -> types.HarmBlockThreshold:
  name = os.getenv("SAFETY_BLOCK_THRESHOLD", "BLOCK_MEDIUM_AND_ABOVE")
  allowed = {"BLOCK_LOW_AND_ABOVE", "BLOCK_MEDIUM_AND_ABOVE"}
  if name not in allowed:  # never weaker than medium, whatever the environment says
    name = "BLOCK_MEDIUM_AND_ABOVE"
  return types.HarmBlockThreshold[name]


def safety_settings() -> list[types.SafetySetting]:
  return [
      types.SafetySetting(category=c, threshold=_threshold(),
                          method=types.HarmBlockMethod.SEVERITY)
      for c in _CATEGORIES
  ]


SAFETY_SETTINGS = safety_settings()


def generate_content_config() -> types.GenerateContentConfig:
  """Config for the voice agent: safety settings that also apply in live mode."""
  return types.GenerateContentConfig(safety_settings=safety_settings())
