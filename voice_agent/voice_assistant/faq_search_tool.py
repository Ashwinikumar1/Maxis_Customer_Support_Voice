"""FAQ search tool: queries the Maxis Vertex AI Search app, returns the top 3 snippets."""

import asyncio
import logging
import os

import google.auth
from google.auth.transport.requests import AuthorizedSession

_ENGINE = os.getenv("FAQ_SEARCH_ENGINE_ID", "maxis-faq-search-app_1792223644923")
_session = None
# [Observability #3b] Logger for FAQ search failures (see search_knowledge_base).
logger = logging.getLogger("voice_agent.faq_search")


def _search(query: str) -> list[str]:
  global _session
  if _session is None:  # reused: keeps the token and connection warm
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    _session = AuthorizedSession(creds)
  project = os.environ["GOOGLE_CLOUD_PROJECT"]
  url = (f"https://discoveryengine.googleapis.com/v1/projects/{project}/locations/global"
         f"/collections/default_collection/engines/{_ENGINE}/servingConfigs/default_search:search")
  resp = _session.post(url, timeout=5, headers={"X-Goog-User-Project": project}, json={
      "query": query,
      "pageSize": 3,
      "contentSearchSpec": {"snippetSpec": {"returnSnippet": True}},
      "spellCorrectionSpec": {"mode": "SUGGESTION_ONLY"},  # saves ~250 ms
      "queryExpansionSpec": {"condition": "DISABLED"},
  })
  resp.raise_for_status()
  return [s["snippet"]
          for r in resp.json().get("results", [])
          for s in r["document"].get("derivedStructData", {}).get("snippets", [])[:1]]


async def search_knowledge_base(query: str) -> dict:
  """Searches the Maxis FAQ for general questions about Maxis plans, services,
  roaming, coverage, the Maxis app and policies.

  Args:
    query: The caller's question as a short search query in English.
  """
  try:
    return {"results": await asyncio.to_thread(_search, query)}
  except Exception as e:
    # [Observability #3b] Record every FAQ search failure, once.
    # THE DESIGN: on failure the tool does NOT raise - it returns a friendly
    # message, so the agent can tell the caller "the knowledge base is
    # unavailable" and the conversation continues. That part is unchanged.
    # WHY THE LOG LINE MATTERS: before, this `except` swallowed the error
    # completely. From the outside a broken FAQ (wrong engine ID, missing
    # permission, Vertex AI Search outage, timeout) looked exactly like an
    # agent that simply did not know the answer, and ADK even recorded the tool
    # call as successful. Now each failure is written to the logs.
    # WHAT IS LOGGED: the error type in the message, plus the full traceback
    # (logger.exception), so Error Reporting can group repeated failures.
    # Source: https://docs.cloud.google.com/error-reporting/docs/formatting-error-messages
    # WHAT IS NOT LOGGED: the caller's question (`query`) - it can contain
    # personal information, so it stays out of the logs.
    # WHY "ONCE": the observability add-on's span around _search records the
    # failure on the trace but deliberately does not log it, so each failure
    # appears exactly one time in the logs and error counts stay accurate.
    logger.exception("FAQ search failed (%s)", type(e).__name__)
    return {"error": "The knowledge base is unavailable right now."}
