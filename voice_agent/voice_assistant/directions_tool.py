"""Directions tool: Google Maps Routes API (computeRoutes).

Auth: GOOGLE_MAPS_API_KEY if set, otherwise Application Default Credentials
billed to GOOGLE_CLOUD_PROJECT (same identity as Vertex AI - no extra key).
Requires the Routes API (routes.googleapis.com) enabled on the project.
"""

import asyncio
import os

import google.auth
from google.auth.transport.requests import AuthorizedSession
import requests

_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
_FIELD_MASK = (
    "routes.duration,routes.distanceMeters,routes.description,"
    "routes.legs.steps.navigationInstruction.instructions"
)
_MODES = {"driving": "DRIVE", "walking": "WALK", "transit": "TRANSIT",
          "bicycling": "BICYCLE", "motorcycle": "TWO_WHEELER"}
_MAX_TEXT = 200
_MAX_STEPS = 8
_session = None


def _http():
  global _session
  if _session is None:
    if os.getenv("GOOGLE_MAPS_API_KEY"):
      _session = requests.Session()
    else:
      creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
      _session = AuthorizedSession(creds)
  return _session


def _call_routes(body: dict) -> dict:
  headers = {"X-Goog-FieldMask": _FIELD_MASK}
  if key := os.getenv("GOOGLE_MAPS_API_KEY"):
    headers["X-Goog-Api-Key"] = key
  else:
    headers["X-Goog-User-Project"] = os.getenv("GOOGLE_CLOUD_PROJECT", "")
  resp = _http().post(_ROUTES_URL, json=body, headers=headers, timeout=10)
  resp.raise_for_status()
  return resp.json()


async def get_directions(
    origin: str, destination: str, travel_mode: str = "driving", language: str = "en"
) -> dict:
  """Gets real directions, distance and travel time between two places in Malaysia or elsewhere.

  Args:
    origin: Starting place or address, e.g. "KL Sentral". Ask the user if missing.
    destination: Destination place or address, e.g. "KLIA".
    travel_mode: One of driving, walking, transit, bicycling, motorcycle.
    language: "en" for English or "ms" for Malay - match the user's language.
  """
  origin = " ".join(str(origin or "").split())[:_MAX_TEXT]
  destination = " ".join(str(destination or "").split())[:_MAX_TEXT]
  if not origin or not destination:
    return {"status": "error", "message": "I need both a starting point and a destination."}
  mode = _MODES.get((travel_mode or "").lower().strip(), "DRIVE")
  body = {
      "origin": {"address": origin},
      "destination": {"address": destination},
      "travelMode": mode,
      "languageCode": "ms" if language == "ms" else "en",
      "regionCode": "MY",
      "units": "METRIC",
  }
  if mode in ("DRIVE", "TWO_WHEELER"):
    body["routingPreference"] = "TRAFFIC_AWARE"

  try:
    data = await asyncio.to_thread(_call_routes, body)
  except Exception:  # network / quota / auth - keep details out of the voice reply
    return {"status": "error", "message": "The directions service is unavailable right now."}

  routes = data.get("routes") or []
  if not routes:
    return {"status": "not_found", "message": "No route found between those places."}
  route = routes[0]
  seconds = int(str(route.get("duration", "0s")).rstrip("s") or 0)
  steps = [
      s["navigationInstruction"]["instructions"].replace("\n", ". ")
      for leg in route.get("legs", [])
      for s in leg.get("steps", [])
      if s.get("navigationInstruction", {}).get("instructions")
  ]
  return {
      "status": "success",
      "origin": origin,
      "destination": destination,
      "travel_mode": mode.lower(),
      "via": route.get("description", ""),
      "distance_km": round(route.get("distanceMeters", 0) / 1000, 1),
      "duration_minutes": round(seconds / 60),
      "key_steps": steps[:_MAX_STEPS],
      "total_steps": len(steps),
  }
