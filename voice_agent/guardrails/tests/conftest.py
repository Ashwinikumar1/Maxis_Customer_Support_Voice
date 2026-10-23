"""Make voice_agent/ importable (guardrails, streaming_core, voice_assistant) from any cwd."""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[2]
if str(APP_DIR) not in sys.path:
  sys.path.insert(0, str(APP_DIR))
