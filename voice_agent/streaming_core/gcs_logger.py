"""Transcript dump: collected during the session, uploaded once when it ends.

Writes gs://<GCS_BUCKET>/<GCS_LOG_PREFIX>/conversation_<id>_<ts>.json using
Application Default Credentials. If GCS_BUCKET is unset (local dev), it saves
to ./transcripts/ instead.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .redaction import redact_transcript
from .utils import logger


def _now() -> str:
  return datetime.now(timezone.utc).isoformat()


class TranscriptLogger:

  def __init__(self, session_id: str, **metadata):
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", session_id)[:64] or "session"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    prefix = os.getenv("GCS_LOG_PREFIX", "VoiceAgentLogs").strip("/")
    self.bucket = os.getenv("GCS_BUCKET")
    self.object_name = f"{prefix}/conversation_{safe_id}_{stamp}.json"
    self.data = {
        "session_id": safe_id,
        "session_start": _now(),
        "session_end": None,
        "metadata": {k: v for k, v in metadata.items() if v is not None},
        "conversation": [],
    }

  def add_turn(self, speaker: str, text: str, agent: str | None = None):
    text = (text or "").strip()
    if text:
      entry = {"speaker": speaker, "text": text, "timestamp": _now()}
      if agent:
        entry["agent"] = agent
      self.data["conversation"].append(entry)

  def set_metadata(self, key: str, value):
    """Adds one entry to the transcript's metadata (written when save() runs).

    [Observability #12] Used at the end of a call to store its token usage,
    so whoever reads a transcript (QA, cost review) sees what that call
    consumed without searching the metrics. Skips None, like __init__ does.
    """
    if value is not None:
      self.data["metadata"][key] = value

  def add_event(self, event: str, **details):
    self.data["conversation"].append({"speaker": "System", "event": event, **details, "timestamp": _now()})

  async def save(self):
    """Uploads the full transcript to GCS. Never raises and never loses data:
    if GCS_BUCKET is unset OR the upload fails (e.g. expired credentials),
    the transcript is written to ./transcripts/ instead."""
    self.data["session_end"] = _now()
    try:  # personal data out before anything is stored (design section 7)
      await asyncio.to_thread(redact_transcript, self.data)
    except Exception:  # never store unredacted speech: drop the text instead
      logger.exception("Transcript redaction failed; text removed")
      for entry in self.data["conversation"]:
        if "text" in entry:
          entry["text"] = "[redaction failed]"
      self.data["metadata"]["redaction"] = "failed"
    payload = json.dumps(self.data, ensure_ascii=False, indent=2)
    if self.bucket:
      try:
        await asyncio.to_thread(self._upload_gcs, payload)
        logger.info("Transcript saved to gs://%s/%s", self.bucket, self.object_name)
        return
      except Exception as e:
        # [Observability #8a] Log the FULL error, not just its type.
        # WHY: the old line said only e.g. "GCS upload failed (Forbidden)". The
        # type alone does not say WHICH bucket, WHICH permission is missing, or
        # whether credentials expired - the message and traceback do, so the
        # cause can be fixed without reproducing the failure.
        # WHY IT MATTERS ON CLOUD RUN: when the upload fails, the fallback below
        # writes the transcript to the container's local disk, which is
        # in-memory and lost when the instance stops. So this log line is often
        # the only trace that a transcript was lost and why.
        # exc_info=True puts the traceback into the same log entry, where Error
        # Reporting can pick it up and group repeated failures.
        logger.error("GCS upload failed (%s) - saving transcript locally instead",
                     type(e).__name__, exc_info=True)
    try:
      path = Path("transcripts") / Path(self.object_name).name
      path.parent.mkdir(exist_ok=True)
      path.write_text(payload, encoding="utf-8")
      logger.warning("Transcript saved locally to %s", path)
    except Exception:
      logger.exception("Failed to save transcript")

  def _upload_gcs(self, payload: str):
    from google.cloud import storage

    client = storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None)
    client.bucket(self.bucket).blob(self.object_name).upload_from_string(
        payload, content_type="application/json")
