"""FAQ search tool: queries the Maxis Vertex AI Search app, returns the top 3 snippets."""

import asyncio
import os

import google.auth
from google.auth.transport.requests import AuthorizedSession

_ENGINE = os.getenv("FAQ_SEARCH_ENGINE_ID", "maxis-faq-search-app_1792223644923")
_session = None


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
  except Exception:
    return {"error": "The knowledge base is unavailable right now."}
