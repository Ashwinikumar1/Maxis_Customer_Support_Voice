"""Mock agent gateway: the roaming and WhatsApp business rules from the hand-over pack.

Implements the operations in CustomerDocs/adk-handover-pack/05-tool-api-specs/
roaming.yaml and messages.yaml with the rules in SID-02, SID-03 and SID-04, over
the DEMO DATA in data.py. Every answer uses the gateway's response envelope:

    {"status": "success", "responseData": {..., "next_step": ...}, "timeStamp": ...}
    {"status": "fail", "message": ..., "violations": [{code, message, action}], "timeStamp": ...}

State (accounts, passes, pending switches, duplicate guards) lives in memory,
like the production gateway pinned to one instance.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import data as D

MYT = ZoneInfo("Asia/Kuala_Lumpur")
DUPLICATE_WINDOW = timedelta(minutes=10)
PURCHASE_POLL_LIMIT_S = 20        # production polls every 2 s for up to 20 s
REPORTED_SWITCH_MINUTES = 5       # what production tells the caller
# How long an explicit switchRoaming stays "setting_up" in the mock: by default as long as the caller is
# told (5 minutes), like production, where the gateway reports setting_up until Maxis shows the change.
# (Roaming switched on by a purchase uses each test line's own switch_seconds, so purchases complete.)
SWITCH_SECONDS = int(os.getenv("MOCK_SWITCH_SECONDS", str(REPORTED_SWITCH_MINUTES * 60)))
WHATSAPP_TEMPLATES = {"manage_passes", "wrong_pass", "insurance"}  # pay_bill isn't set up


class _Fail(Exception):
  def __init__(self, code: str, message: str, action: str):
    super().__init__(message)
    self.code, self.message, self.action = code, message, action


def _now() -> datetime:
  return datetime.now(MYT)


def _spoken_date(d: date | datetime) -> str:
  return f"{d.strftime('%A')}, {d.day} {d.strftime('%B')}"


def _spoken_time(dt: datetime) -> str:
  hour = dt.strftime("%I").lstrip("0")
  minute = "" if dt.minute == 0 else dt.strftime(":%M")
  return f"{_spoken_date(dt)} at {hour}{minute} {dt.strftime('%p').lower()}"


def _parse_date(value: str | None, field: str) -> date | None:
  if value in (None, ""):
    return None
  if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
    raise _Fail("INVALID_REQUEST", f"{field} must be YYYY-MM-DD.", "fix_the_request")
  try:
    return date.fromisoformat(value)
  except ValueError:
    raise _Fail("INVALID_REQUEST", f"{field} is not a real date.", "fix_the_request") from None


def _public_pass(p: dict) -> dict:
  return {k: v for k, v in p.items() if not k.startswith("_")}


class MockGateway:

  def __init__(self):
    self.pass_history = os.getenv("MOCK_PASS_HISTORY", "true").lower() == "true"
    self.accounts: dict = {}
    self._purchases: dict = {}     # duplicate guard: key -> (time, response)
    self._switches: dict = {}      # duplicate guard: key -> (time, response)
    self._whatsapp: set = set()    # (conversation, kind) already sent
    self._fail_next: dict[str, int] = {}
    self._pass_seq = 0
    self.reset()

  # ------------------------------------------------------------------ admin
  def reset(self, msisdn: str | None = None):
    """Restores the demo accounts (all, or one caller's) to their starting state."""
    fresh = D.seed_accounts(_now())
    if msisdn:
      if msisdn in fresh:
        self.accounts[msisdn] = fresh[msisdn]
      for store in (self._purchases, self._switches):
        for key in [k for k in store if k[0] == msisdn]:
          del store[key]
      self._whatsapp = {k for k in self._whatsapp if k[1] != msisdn}
    else:
      self.accounts = fresh
      self._purchases.clear()
      self._switches.clear()
      self._whatsapp.clear()

  def fail_next(self, operation: str = "*", count: int = 1):
    """Makes the next `count` calls to `operation` (or any, with "*") fail with BACKEND_UNAVAILABLE."""
    self._fail_next[operation] = max(0, int(count))

  def _maybe_fail(self, operation: str):
    for key in (operation, "*"):
      if self._fail_next.get(key, 0) > 0:
        self._fail_next[key] -= 1
        raise _Fail("BACKEND_UNAVAILABLE", "Maxis systems didn't answer in time.", "apologise_and_offer_agent")

  # ---------------------------------------------------------------- helpers
  def _account(self, caller_id: str | None) -> tuple[str, dict]:
    digits = re.sub(r"\D", "", caller_id or "")
    if not digits:
      raise _Fail("CALLER_NOT_IDENTIFIED", "The calling number is withheld.", "general_help_only")
    if digits.startswith("0"):
      digits = "6" + digits
    account = self.accounts.get(digits)
    if account is None:
      raise _Fail("NOT_A_MAXIS_NUMBER", "This number isn't a Maxis line.", "general_help_only")
    return digits, account

  def _line(self, account: dict, line_ref: str | None) -> dict:
    lines = account["lines"]
    if not line_ref:
      return next(line for line in lines if line["is_callers_own"])
    visible = lines if account["principal"] else [line for line in lines if line["is_callers_own"]]
    for line in visible:
      if line["line_ref"] == line_ref:
        return line
    raise _Fail("LINE_NOT_FOUND", "That line isn't one this caller can manage.", "list_lines_again")

  @staticmethod
  def _why_not(line: dict) -> str | None:
    if not line["mobile"]:
      return "not_a_mobile_line"
    if line["prepaid"]:
      return "prepaid_line"
    if not line["roaming_allowed"]:
      return "roaming_not_allowed_on_this_line"
    return None

  @staticmethod
  def _line_out(line: dict) -> dict:
    out = {"line_ref": line["line_ref"], "label": line["label"], "is_callers_own": line["is_callers_own"]}
    if line.get("nickname"):
      out["nickname"] = line["nickname"]
    return out

  @staticmethod
  def _refresh_roaming(line: dict, now: datetime):
    """A pending switch completes once its time has passed."""
    if line["roaming"] == "setting_up" and line["roaming_ready_at"] and now >= line["roaming_ready_at"]:
      line["roaming"] = line.pop("_target", "on")
      line["roaming_ready_at"] = None

  def _refresh_passes(self, account: dict, now: datetime):
    for passes in account["passes"].values():
      for p in passes:
        if p["status"] == "scheduled" and now >= p["start"]:
          p["status"] = "active"
        if p["status"] == "active" and now >= p["end"]:
          p["status"] = "past"

  def _mypass_out(self, p: dict, now: datetime) -> dict:
    catalogue = {c["pass_id"]: c for c in D.CATALOGUE}
    out = {"pass_ref": p["pass_ref"], "name": catalogue[p["pass_id"]]["name"], "status": p["status"],
           "bought": _spoken_date(p["bought"]), "ends": _spoken_date(p["end"])}
    if p["status"] == "scheduled":
      out["starts"] = _spoken_date(p["start"])
      cutoff = p["start"] - D.CANCEL_IN_APP_CUTOFF
      out["can_cancel_in_app"] = now < cutoff
      if now < cutoff:
        out["cancel_in_app_by"] = _spoken_time(cutoff)
    return out

  @staticmethod
  def _resolve_places(destinations: str) -> tuple[list[str], list[dict], list[str]]:
    countries, unclear, unsupported = [], [], []
    for raw in (destinations or "").split(","):
      place = raw.strip()
      if not place:
        continue
      key = place.lower().removeprefix("the ").strip()
      if key in D.UNCLEAR_PLACES:
        unclear.append({"place": place, "could_be": D.UNCLEAR_PLACES[key]})
      elif key in D.UNSUPPORTED:
        unsupported.append(place)
      else:
        country = D.CITY_TO_COUNTRY.get(key) or next(
            (c for c in D.ALL_COUNTRIES if c.lower() == key), None)
        if country is None:
          unsupported.append(place)
        elif country not in countries:
          countries.append(country)
    if not countries and not unclear and not unsupported:
      raise _Fail("INVALID_REQUEST", "destinations is required.", "fix_the_request")
    return countries, unclear, unsupported

  def _plan_trip(self, start: date | None, end: date | None, days: int | None,
                 now: datetime) -> tuple[dict, str | None]:
    """Trip dates and Maxis's booking limits. Returns (trip, problem next_step or None)."""
    today = now.date()
    trip = {"today": today.isoformat(), "today_spoken": _spoken_date(today),
            "last_bookable_date": (today + timedelta(days=D.BOOKING_WINDOW_DAYS)).isoformat(),
            "max_trip_days": D.MAX_TRIP_DAYS}
    if start is None:
      return trip, None
    trip.update(start_date=start.isoformat(), starts=_spoken_date(start), starts_today=start == today)
    if start < today:
      return trip, "start_date_in_past"
    if start > today + timedelta(days=D.BOOKING_WINDOW_DAYS):
      return trip, "beyond_booking_window"
    if end is not None:
      if end < start:
        trip["end_date"] = end.isoformat()
        return trip, "end_before_start"
      days = (end - start).days + 1
    if days is not None:
      if days < 1:
        raise _Fail("INVALID_REQUEST", "days must be at least 1.", "fix_the_request")
      trip.update(days=days, end_date=(start + timedelta(days=days - 1)).isoformat())
      if days > D.MAX_TRIP_DAYS:
        return trip, "trip_too_long"
    if start.isoformat() in D.BLOCKED_START_DATES:
      trip["blocked_reason"] = "Passes can't start on that day."
      return trip, "blocked_start_date"
    if start > today:
      start_at = datetime.combine(start, datetime.min.time(), MYT)
      if start_at - now < D.SCHEDULE_MIN_LEAD:
        earliest = (now + D.SCHEDULE_MIN_LEAD)
        earliest_day = earliest.date() + timedelta(days=1)
        trip["earliest_booking_date"] = earliest_day.isoformat()
        return trip, "too_late_to_schedule"
    return trip, None

  @staticmethod
  def _covering(countries: list[str]) -> list[dict]:
    wanted = set(countries)
    return [p for p in D.CATALOGUE if wanted <= D.pass_covers(p)]

  @staticmethod
  def _cheapest_per_length(passes: list[dict]) -> list[dict]:
    best: dict[int, dict] = {}
    for p in passes:
      if p["days"] not in best or p["price_rm"] < best[p["days"]]["price_rm"]:
        best[p["days"]] = p
    return [_public_pass(best[d]) for d in sorted(best)]

  # ------------------------------------------------------------- operations
  async def list_roaming_lines(self, caller_id: str | None) -> dict:
    self._maybe_fail("listRoamingLines")
    _, account = self._account(caller_id)
    now = _now()
    lines = account["lines"] if account["principal"] else [l for l in account["lines"] if l["is_callers_own"]]
    out = []
    for line in lines:
      self._refresh_roaming(line, now)
      why = self._why_not(line)
      item = {**self._line_out(line), "can_buy_pass": why is None,
              "roaming": line["roaming"] if line["mobile"] else "off",
              "has_active_pass": any(p["status"] == "active" for p in account["passes"][line["line_ref"]])}
      if why:
        item["why_not"] = why
      out.append(item)
    return {"lines": out, "can_manage_share_lines": account["principal"],
            "next_step": "confirm_which_line_if_unclear" if len(out) > 1 else "use_callers_own_line"}

  async def find_passes(self, caller_id, destinations, start_date=None, end_date=None, days=None,
                        line_ref=None) -> dict:
    self._maybe_fail("findPasses")
    _, account = self._account(caller_id)
    line = self._line(account, line_ref)
    now = _now()
    result = {"line": self._line_out(line)}
    why = self._why_not(line)
    if why:
      return {**result, "why_not": why, "next_step": "line_cannot_buy_pass"}

    countries, unclear, unsupported = self._resolve_places(destinations)
    result["destinations"] = countries
    if unclear or unsupported:
      result.update(unclear_places=unclear, unsupported_places=unsupported)
      only_unsupported = unsupported and not unclear and not countries
      return {**result, "next_step": "destination_not_supported" if only_unsupported else "clarify_destination"}

    start = _parse_date(start_date, "start_date")
    end = _parse_date(end_date, "end_date")
    days = int(days) if days not in (None, "") else None
    trip, problem = self._plan_trip(start, end, days, now)
    result["trip"] = trip

    notices = [D.NOTICES[c] for c in countries if c in D.NOTICES]
    if notices:
      result["notices"] = notices
    ppu = [c for c in countries if c in D.PAY_PER_USE]
    if ppu:
      result["pay_per_use_countries"] = ppu
      countries = [c for c in countries if c not in D.PAY_PER_USE]
      if not countries:
        return {**result, "next_step": "pay_per_use_only"}

    covering = self._covering(countries)
    if not covering:
      return {**result, "next_step": "no_pass_covers_all_destinations"}
    result["options"] = self._cheapest_per_length(covering)

    if start is None:
      return {**result, "next_step": "ask_trip_dates"}
    if problem:
      return {**result, "next_step": problem}
    if "days" not in trip:
      return {**result, "next_step": "ask_trip_length"}

    trip_days, starts_today = trip["days"], trip["starts_today"]
    if all(c in D.DAY_PASS_ONLY for c in countries):
      result["recommended"] = _public_pass(covering[0])
      return {**result, "next_step": "recommend_day_pass" if starts_today else "day_pass_buy_on_the_day"}

    bookable = [p for p in covering if starts_today or p["can_book_ahead"]]
    if not bookable:
      return {**result, "next_step": "buy_on_the_day"}
    zone_rank = {z: i for i, z in enumerate(D.ZONE_ORDER)}
    rank = lambda p: (p["price_rm"], zone_rank.get(p["_zone"], 9), p["days"])  # noqa: E731
    fitting = sorted((p for p in bookable if p["days"] >= trip_days), key=rank)
    if not fitting:
      longest = max(bookable, key=lambda p: (p["days"], -p["price_rm"]))
      result["recommended"] = _public_pass(longest)
      return {**result, "next_step": "trip_longer_than_any_pass"}
    result["recommended"] = _public_pass(fitting[0])
    result["alternatives"] = [_public_pass(p) for p in fitting[1:3]]
    return {**result, "next_step": "recommend_pass"}

  async def buy_pass(self, caller_id, pass_id, destinations, start_date, line_ref=None,
                     confirmed=False) -> dict:
    self._maybe_fail("buyPass")
    msisdn, account = self._account(caller_id)
    line = self._line(account, line_ref)
    now = _now()
    result = {"line": self._line_out(line)}
    why = self._why_not(line)
    if why:
      return {**result, "why_not": why, "next_step": "line_cannot_buy_pass"}

    countries, unclear, unsupported = self._resolve_places(destinations)
    result["destinations"] = countries
    if unclear or unsupported:
      result.update(unclear_places=unclear, unsupported_places=unsupported)
      only_unsupported = unsupported and not unclear and not countries
      return {**result, "next_step": "destination_not_supported" if only_unsupported else "clarify_destination"}

    chosen = next((p for p in D.CATALOGUE if p["pass_id"] == pass_id), None)
    passable = [c for c in countries if c not in D.PAY_PER_USE]
    if chosen is None or not set(passable) <= D.pass_covers(chosen):
      return {**result, "next_step": "pass_not_available_for_this_trip"}
    result["pass"] = _public_pass(chosen)

    start = _parse_date(start_date, "start_date")
    if start is None:
      raise _Fail("INVALID_REQUEST", "start_date is required.", "fix_the_request")
    trip, problem = self._plan_trip(start, None, chosen["days"], now)
    result["trip"] = trip
    if problem in ("start_date_in_past", "beyond_booking_window", "blocked_start_date", "too_late_to_schedule"):
      return {**result, "next_step": problem}
    if not trip["starts_today"] and not chosen["can_book_ahead"]:
      return {**result, "next_step": "pass_cannot_be_booked_ahead"}
    result["starts"] = "now" if trip["starts_today"] else trip["starts"]

    self._refresh_roaming(line, now)
    if not confirmed:
      return {**result, "roaming_switched_on_first": line["roaming"] == "off", "next_step": "confirm_purchase"}

    key = (msisdn, line["line_ref"], pass_id, start.isoformat())
    previous = self._purchases.get(key)
    if previous and now - previous[0] < DUPLICATE_WINDOW:
      return {**deepcopy(previous[1]), "repeated_request": True}

    # Switch roaming on first if needed, then wait (production polls every 2 s for up to 20 s).
    if line["roaming"] == "off":
      line.update(roaming="setting_up", roaming_ready_at=now + timedelta(seconds=line["switch_seconds"]),
                  _target="on", _switched_by_purchase=True)
    if line["roaming"] == "setting_up":
      remaining = (line["roaming_ready_at"] - now).total_seconds()
      if remaining > PURCHASE_POLL_LIMIT_S:
        return {**result, "ready_in_minutes": max(1, math.ceil(remaining / 60)),
                "roaming_switched_on": False, "next_step": "roaming_still_switching_on"}
      await asyncio.sleep(max(0.0, min(remaining, 3.0)))   # a short, realistic wait
      line.update(roaming=line.pop("_target", "on"), roaming_ready_at=None)
      now = _now()

    self._pass_seq += 1
    if trip["starts_today"]:
      p_start, status, outcome = now, "active", "active_now"
    else:
      p_start, status, outcome = datetime.combine(start, datetime.min.time(), MYT), "scheduled", "scheduled"
    account["passes"][line["line_ref"]].insert(0, {
        "pass_ref": f"P-NEW-{self._pass_seq}", "pass_id": pass_id, "status": status, "start": p_start,
        "end": p_start + timedelta(days=chosen["days"]), "bought": now})

    done = {**result, "outcome": outcome, "roaming_switched_on": bool(line.pop("_switched_by_purchase", False)),
            "sms_when_active": True, "ready_in_minutes": REPORTED_SWITCH_MINUTES, "next_step": "purchase_done"}
    if chosen["travel_insurance"]:
      done["offer_whatsapp"] = "insurance"
    self._purchases[key] = (now, deepcopy(done))
    return done

  async def list_my_passes(self, caller_id, when="all", line_ref=None) -> dict:
    self._maybe_fail("listMyPasses")
    _, account = self._account(caller_id)
    line = self._line(account, line_ref)
    now = _now()
    self._refresh_passes(account, now)
    when = when or "all"
    if when not in ("scheduled", "active", "past", "all"):
      raise _Fail("INVALID_REQUEST", "when must be scheduled, active, past or all.", "fix_the_request")
    passes = account["passes"][line["line_ref"]]
    if not self.pass_history:   # production today: only scheduled passes can be read
      passes = [p for p in passes if p["status"] == "scheduled"]
    passes = [p for p in passes if p["status"] != "past" or now - p["end"] <= timedelta(days=60)]
    if when != "all":
      passes = [p for p in passes if p["status"] == when]
    order = {"scheduled": 0, "active": 1, "past": 2}
    passes = sorted(passes, key=lambda p: order[p["status"]])
    out = [self._mypass_out(p, now) for p in passes]
    step = "no_passes_found" if not out else "confirm_this_pass" if len(out) == 1 else "ask_which_pass"
    return {"line": self._line_out(line), "passes": out, "history_available": self.pass_history,
            "next_step": step}

  async def help_with_wrong_pass(self, caller_id, pass_ref, line_ref=None) -> dict:
    self._maybe_fail("helpWithWrongPass")
    _, account = self._account(caller_id)
    line = self._line(account, line_ref)
    now = _now()
    self._refresh_passes(account, now)
    result = {"line": self._line_out(line)}
    found = next((p for p in account["passes"][line["line_ref"]] if p["pass_ref"] == pass_ref), None)
    if found is None:
      return {**result, "next_step": "pass_not_found_list_passes_again"}
    result["pass"] = self._mypass_out(found, now)
    if found["status"] == "scheduled":
      if now < found["start"] - D.CANCEL_IN_APP_CUTOFF:
        return {**result, "offer_whatsapp": "manage_passes", "next_step": "explain_cancel_in_app"}
      return {**result, "next_step": "too_late_to_cancel_offer_agent"}
    return {**result, "next_step": "no_refund_offer_agent"}

  async def switch_roaming(self, caller_id, turn, line_ref=None, confirmed=False,
                           acknowledged_active_pass=False) -> dict:
    self._maybe_fail("switchRoaming")
    if turn not in ("on", "off"):
      raise _Fail("INVALID_REQUEST", "turn must be on or off.", "fix_the_request")
    msisdn, account = self._account(caller_id)
    line = self._line(account, line_ref)
    now = _now()
    self._refresh_roaming(line, now)
    self._refresh_passes(account, now)
    result = {"line": self._line_out(line), "action": f"switch_{turn}", "roaming": line["roaming"]}
    why = self._why_not(line)
    if why:
      return {**result, "why_not": why, "next_step": "line_cannot_use_roaming"}

    key = (msisdn, line["line_ref"], turn)
    previous = self._switches.get(key)
    if confirmed and previous and now - previous[0] < DUPLICATE_WINDOW:
      return {**deepcopy(previous[1]), "repeated_request": True}
    if line["roaming"] == "setting_up":
      return {**result, "ready_in_minutes": REPORTED_SWITCH_MINUTES, "next_step": "still_setting_up"}
    if line["roaming"] == turn:
      return {**result, "next_step": f"already_{turn}"}
    if turn == "off" and not acknowledged_active_pass:
      live = [p for p in account["passes"][line["line_ref"]] if p["status"] in ("active", "scheduled")]
      if live:
        return {**result, "passes": [self._mypass_out(p, now) for p in live],
                "next_step": "warn_passes_will_not_work"}
    if not confirmed:
      return {**result, "next_step": "confirm_switch"}

    line.update(roaming="setting_up", roaming_ready_at=now + timedelta(seconds=SWITCH_SECONDS),
                _target=turn)
    done = {**result, "ready_in_minutes": REPORTED_SWITCH_MINUTES, "next_step": "switch_requested"}
    self._switches[key] = (now, deepcopy(done))
    return done

  async def send_whatsapp_link(self, caller_id, kind, conversation_id=None) -> dict:
    self._maybe_fail("sendWhatsAppLink")
    if kind not in ("manage_passes", "wrong_pass", "insurance", "pay_bill"):
      raise _Fail("INVALID_REQUEST", "Unknown kind.", "fix_the_request")
    msisdn, _ = self._account(caller_id)
    key = (conversation_id or msisdn, msisdn, kind)
    if key in self._whatsapp:
      return {"sent": False, "reason": "already_sent", "next_step": "tell_caller_it_was_already_sent"}
    if kind not in WHATSAPP_TEMPLATES:
      return {"sent": False, "reason": "not_available", "next_step": "explain_link_not_available"}
    self._whatsapp.add(key)
    return {"sent": True, "next_step": "tell_caller_it_is_on_whatsapp"}


def envelope_ok(data: dict) -> dict:
  return {"status": "success", "responseData": data, "timeStamp": _now().isoformat()}


def envelope_fail(err: _Fail) -> dict:
  return {"status": "fail", "message": err.message,
          "violations": [{"code": err.code, "message": err.message, "action": err.action}],
          "timeStamp": _now().isoformat()}


async def call(gateway: MockGateway, operation: str, **kwargs) -> dict:
  """Runs one operation and wraps it in the response envelope."""
  try:
    return envelope_ok(await getattr(gateway, operation)(**kwargs))
  except _Fail as err:
    return envelope_fail(err)
