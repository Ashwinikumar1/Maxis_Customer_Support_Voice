"""Security guardrails for the Maxis voice agent.

Everything the model may do passes through code here, never through the prompt alone:

  safety.py         Gemini safety settings, forwarded by ADK into every live session (AI-03)
  tool_policy.py    ADK plugin: tool allow-list, argument checks, identity never from the model,
                    verification tier, confirmation tokens, write limits, kill flags (AG-02..06, AP-*)
  injection.py      ADK plugins that flag prompt injection: a fast local check in English and
                    Malay, and Model Armor screening; a flag disables write tools for the call (AP-09)
  redaction.py      Transcript redaction before storage, with spoken-number normalisation (DP-08..11)
  events.py         Structured security events for the SOC, with no personal data (AP-18, LM-13)
  sessions.py       Persistent session store factory (AG-07)
  identity.py       Caller identity from the signed IAP header on /ws, never from the client (CR-02, AG-03)
  call_limits.py    Silence watchdog that ends calls nobody is speaking on (AP-11)

See security/README.md at the repository root for how these map to the security baseline.
"""

from .events import security_event
from .injection import InjectionFlagPlugin, build_model_armor_plugin
from .redaction import RedactingTranscriptLogger, redact_text
from .safety import SAFETY_SETTINGS, generate_content_config
from .sessions import make_session_service
from .tool_policy import (
    STATE_PREFIX,
    ToolPolicy,
    ToolPolicyPlugin,
    ToolRule,
    issue_confirmation,
    set_verification_level,
)


def build_plugins(policy: ToolPolicy | None = None) -> list:
  """The plugin set every Runner/App should use, in order.

  Injection screening runs first so a flag raised on this turn already applies to its tool calls.
  Model Armor is added only when its templates are configured (MODEL_ARMOR_PROMPT_TEMPLATE /
  MODEL_ARMOR_RESPONSE_TEMPLATE).
  """
  plugins = [InjectionFlagPlugin()]
  armor = build_model_armor_plugin()
  if armor is not None:
    plugins.append(armor)
  plugins.append(ToolPolicyPlugin(policy or ToolPolicy.default()))
  return plugins


__all__ = [
    "InjectionFlagPlugin",
    "RedactingTranscriptLogger",
    "SAFETY_SETTINGS",
    "STATE_PREFIX",
    "ToolPolicy",
    "ToolPolicyPlugin",
    "ToolRule",
    "build_model_armor_plugin",
    "build_plugins",
    "generate_content_config",
    "issue_confirmation",
    "make_session_service",
    "redact_text",
    "security_event",
    "set_verification_level",
]
