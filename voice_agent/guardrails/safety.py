"""Gemini safety settings for the live agent (baseline AI-03).

Content filters are off by default from Gemini 2.5 onwards, so they must be set explicitly.
Set them on the agent's generate_content_config: ADK's Gemini.connect() forwards
generate_content_config.safety_settings into the live session's connect config
(google/adk/models/google_llm.py), and tests/test_safety.py checks that it still does.
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
