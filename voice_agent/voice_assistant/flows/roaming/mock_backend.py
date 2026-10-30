"""Dummy roaming gateway (used only when GATEWAY_URL is unset).

Implements the roaming OpenAPI contract with invented data so the agent can be
demoed end to end. Answers depend on the MSISDN the call started with; state
(purchases, roaming switches) is kept per conversation.

Demo MSISDNs:
  60123000001  single line: postpaid, roaming off, one past pass.
  60123000005  family plan principal with 4 share lines (5 lines in total):
               own line (roaming off, a scheduled pass), "Mum" (active pass),
               "Adam" (roaming off), "the share line ending in 33" (no nickname)
               and "the share line ending in 57" (roaming not allowed).
  Any other MSISDN answers NOT_A_MAXIS_NUMBER.

Handy places: Tokyo, Bali, Bangkok (Asia Pacific passes), London, Dubai
(Worldwide), Georgia (unclear), Maldives (day pass only), Nepal (pay per use),
Antarctica (not a roaming destination).
"""

import copy
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from ...gateway.envelope import fail, ok
from ...gateway.mock import BadRequest, MockContext, register, session_store

MYT = ZoneInfo("Asia/Kuala_Lumpur")
BOOKING_WINDOW_DAYS = 90
MAX_TRIP_DAYS = 90
SWITCH_MINUTES = 5
CANCEL_CUTOFF = timedelta(hours=2)

# --- Catalogue (invented prices) --------------------------------------------
_AP = ("Japan", "South Korea", "Singapore", "Thailand", "Indonesia", "Vietnam", "Philippines",
       "China", "Hong Kong", "Taiwan", "India", "Australia", "New Zealand")
_WORLD = _AP + ("United Kingdom", "United States", "France", "Germany", "Italy", "Spain",
                "Switzerland", "United Arab Emirates", "Saudi Arabia", "Turkey", "Georgia")
_AP_SPOKEN = "Japan, Korea, Singapore, Thailand, Indonesia and eight more Asia Pacific countries"
_WORLD_SPOKEN = "over 20 countries across Asia Pacific, Europe, the US and the Middle East"


def _pass(pass_id, name, price, days, data, covers, covers_spoken, book_ahead, insurance):
  return {"pass_id": pass_id, "name": name, "price_rm": price, "days": days, "data": data,
          "covers": frozenset(covers), "covers_spoken": covers_spoken,
          "can_book_ahead": book_ahead, "travel_insurance": insurance}


PASSES = {p["pass_id"]: p for p in [
    _pass("AP1D", "Asia Pacific 1-day pass", 10, 1, "2GB", _AP, _AP_SPOKEN, False, False),
    _pass("AP3D", "Asia Pacific 3-day pass", 28, 3, "6GB", _AP, _AP_SPOKEN, True, False),
    _pass("AP7D", "Asia Pacific 7-day pass", 48, 7, "20GB", _AP, _AP_SPOKEN, True, True),
    _pass("AP15D", "Asia Pacific 15-day pass", 68, 15, "30GB", _AP, _AP_SPOKEN, True, True),
    _pass("AP30D", "Asia Pacific 30-day pass", 98, 30, "50GB", _AP, _AP_SPOKEN, True, True),
    _pass("WW1D", "Worldwide 1-day pass", 25, 1, "1GB", _WORLD + ("Maldives",),
          "over 20 countries, including the Maldives", False, False),
    _pass("WW7D", "Worldwide 7-day pass", 88, 7, "10GB", _WORLD, _WORLD_SPOKEN, True, True),
    _pass("WW15D", "Worldwide 15-day pass", 128, 15, "20GB", _WORLD, _WORLD_SPOKEN, True, True),
    _pass("WW30D", "Worldwide 30-day pass", 188, 30, "30GB", _WORLD, _WORLD_SPOKEN, True, True),
]}
PAY_PER_USE = {"Nepal"}
NOTICES = {
    "Saudi Arabia": "During Umrah and Hajj peak periods, networks around Makkah and Madinah "
                    "can be congested, so data may be slower.",
    "Thailand": "Network upgrades on some Thai islands may make data slower there until the "
                "end of November.",
}

# --- Destination resolution --------------------------------------------------
_COUNTRY_ALIASES = {
    "Japan": ["japan", "tokyo", "osaka", "kyoto", "hokkaido", "sapporo"],
    "South Korea": ["south korea", "korea", "seoul", "busan", "jeju"],
    "Singapore": ["singapore"],
    "Thailand": ["thailand", "bangkok", "phuket", "krabi", "chiang mai", "hat yai"],
    "Indonesia": ["indonesia", "bali", "jakarta", "bandung", "medan", "lombok"],
    "Vietnam": ["vietnam", "hanoi", "ho chi minh", "ho chi minh city", "saigon", "da nang"],
    "Philippines": ["philippines", "manila", "cebu"],
    "China": ["china", "beijing", "shanghai", "guangzhou", "shenzhen"],
    "Hong Kong": ["hong kong"],
    "Taiwan": ["taiwan", "taipei"],
    "India": ["india", "delhi", "new delhi", "mumbai", "chennai", "bangalore"],
    "Australia": ["australia", "sydney", "melbourne", "perth", "brisbane"],
    "New Zealand": ["new zealand", "auckland"],
    "United Kingdom": ["united kingdom", "uk", "england", "london", "manchester", "scotland"],
    "United States": ["united states", "usa", "us", "america", "new york", "los angeles",
                      "san francisco", "georgia usa", "georgia in the united states", "atlanta"],
    "France": ["france", "paris"],
    "Germany": ["germany", "berlin", "munich"],
    "Italy": ["italy", "rome", "milan"],
    "Spain": ["spain", "madrid", "barcelona"],
    "Switzerland": ["switzerland", "zurich", "geneva"],
    "United Arab Emirates": ["united arab emirates", "uae", "dubai", "abu dhabi"],
    "Saudi Arabia": ["saudi arabia", "saudi", "makkah", "mecca", "madinah", "medina", "jeddah"],
    "Turkey": ["turkey", "turkiye", "istanbul"],
    "Georgia": ["georgia the country", "georgia country", "tbilisi"],
    "Maldives": ["maldives", "male"],
    "Nepal": ["nepal", "kathmandu"],
}
_ALIASES = {alias: country for country, names in _COUNTRY_ALIASES.items() for alias in names}
_UNCLEAR = {"georgia": ["Georgia, the country", "Georgia in the United States"]}


def _norm(place: str) -> str:
  text = re.sub(r"[^a-z ]", " ", place.lower())
  text = re.sub(r"\s+", " ", text).strip()
  return text[4:] if text.startswith("the ") else text


def _resolve(destinations) -> tuple[list[str], list[dict], list[str]]:
  text = str(destinations or "")
  places = [p.strip() for p in text.split(",") if p.strip()]
  if not places:
    raise BadRequest("destinations is required.")
  if len(places) > 10 or len(text) > 300:
    raise BadRequest("Too many destinations.")
  countries, unclear, unsupported = [], [], []
  for place in places:
    key = _norm(place)
    if key in _UNCLEAR:
      unclear.append({"place": place[:60], "could_be": _UNCLEAR[key]})
    elif key in _ALIASES:
      if _ALIASES[key] not in countries:
        countries.append(_ALIASES[key])
    else:
      unsupported.append(place[:60])
  return countries, unclear, unsupported


# --- Dates -------------------------------------------------------------------
def _now() -> datetime:
  return datetime.now(MYT)


def _midnight(d: date) -> datetime:
  return datetime.combine(d, time(0), MYT)


def _day(d: date) -> str:
  return f"{d:%A}, {d.day} {d:%B}"


def _moment(dt: datetime) -> str:
  hour = dt.hour % 12 or 12
  minutes = f":{dt.minute:02d}" if dt.minute else ""
  return f"{hour}{minutes} {'am' if dt.hour < 12 else 'pm'} on {_day(dt.date())}"


def _parse_date(value, field: str) -> date | None:
  if value in (None, ""):
    return None
  if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
    raise BadRequest(f"{field} must be YYYY-MM-DD.")
  try:
    return date.fromisoformat(value)
  except ValueError as e:
    raise BadRequest(f"{field} is not a real date.") from e


def _trip(start_value, end_value, days_value) -> tuple[dict, str | None, int | None]:
  """Returns (trip, problem next_step or None, trip length in days)."""
  today = _now().date()
  trip = {"today": today.isoformat(), "today_spoken": _day(today),
          "last_bookable_date": (today + timedelta(days=BOOKING_WINDOW_DAYS)).isoformat(),
          "max_trip_days": MAX_TRIP_DAYS}
  start = _parse_date(start_value, "start_date")
  end = _parse_date(end_value, "end_date")
  if days_value not in (None, ""):
    try:
      days_value = int(days_value)
    except (TypeError, ValueError) as e:
      raise BadRequest("days must be a whole number.") from e
    if days_value < 1:
      raise BadRequest("days must be at least 1.")
  if start is None:
    return trip, "ask_trip_dates", None
  trip.update(start_date=start.isoformat(), starts=_day(start), starts_today=start == today)
  if start < today:
    trip["earliest_booking_date"] = today.isoformat()
    return trip, "start_date_in_past", None
  if start > today + timedelta(days=BOOKING_WINDOW_DAYS):
    trip["blocked_reason"] = "Passes can only be booked up to 90 days ahead."
    return trip, "beyond_booking_window", None
  if end is not None:
    if end < start:
      return trip, "end_before_start", None
    days = (end - start).days + 1
  elif days_value not in (None, ""):
    days = days_value
    end = start + timedelta(days=days - 1)
  else:
    return trip, "ask_trip_length", None
  trip.update(end_date=end.isoformat(), days=days)
  if days > MAX_TRIP_DAYS:
    return trip, "trip_too_long", None
  return trip, None, days


# --- Demo accounts (keyed by MSISDN) -------------------------------------------
def _line(ref, label, own, roaming, nickname=None, why_not=None):
  line = {"line_ref": ref, "label": label, "is_callers_own": own,
          "can_buy_pass": why_not is None, "roaming": roaming}
  if nickname:
    line["nickname"] = nickname
  if why_not:
    line["why_not"] = why_not
  return line


_OWN = "the line you're calling from"
_ACCOUNTS = {
    "60123000001": {  # single line
        "principal": False,
        "lines": [_line("LN-0001-A", _OWN, True, "off")],
        "passes": {"LN-0001-A": [("PS-1001", "AP3D", -30)]},
    },
    "60123000005": {  # family plan: principal + 4 share lines
        "principal": True,
        "lines": [
            _line("LN-0005-A", _OWN, True, "off"),
            _line("LN-0005-B", "the share line ending in 21", False, "on", nickname="Mum"),
            _line("LN-0005-C", "the share line ending in 48", False, "off", nickname="Adam"),
            _line("LN-0005-D", "the share line ending in 33", False, "off"),
            _line("LN-0005-E", "the share line ending in 57", False, "off",
                  why_not="roaming_not_allowed_on_this_line"),
        ],
        # (pass_ref, pass_id, start offset in days from today)
        "passes": {"LN-0005-A": [("PS-5001", "AP7D", 3), ("PS-5002", "WW1D", -20)],
                   "LN-0005-B": [("PS-5101", "AP15D", -4)]},
    },
}


def _owned(pass_ref: str, pass_id: str, start: datetime, bought: datetime) -> dict:
  p = PASSES[pass_id]
  return {"pass_ref": pass_ref, "name": p["name"], "start": start,
          "end": start + timedelta(days=p["days"]), "bought": bought}


def _account(ctx: MockContext) -> dict | None:
  """This conversation's copy of the caller's demo account (None if unknown MSISDN)."""
  seed = _ACCOUNTS.get(ctx.msisdn)
  if seed is None:
    return None
  store = session_store(f"roaming:{ctx.conversation_id}")
  if "lines" not in store:
    now = _now()
    store["principal"] = seed["principal"]
    store["lines"] = {l["line_ref"]: copy.deepcopy(l) for l in seed["lines"]}
    store["passes"] = {ref: [] for ref in store["lines"]}
    for ref, items in seed["passes"].items():
      for pass_ref, pass_id, offset in items:
        start = _midnight(now.date() + timedelta(days=offset))
        store["passes"][ref].append(_owned(pass_ref, pass_id, start, start - timedelta(days=2)))
    store["done"], store["seq"] = set(), 9000
  return store


def _not_maxis() -> dict:
  return fail("NOT_A_MAXIS_NUMBER", "This number isn't a Maxis account.", "general_help_only")


def _line_not_found() -> dict:
  return fail("LINE_NOT_FOUND", "That line wasn't found on this account.", "list_lines_again")


def _find_line(store: dict, line_ref) -> dict | None:
  if not line_ref:
    return next(l for l in store["lines"].values() if l["is_callers_own"])
  return store["lines"].get(str(line_ref)[:40])


def _line_out(line: dict) -> dict:
  out = {"line_ref": line["line_ref"], "label": line["label"],
         "is_callers_own": line["is_callers_own"]}
  if line.get("nickname"):
    out["nickname"] = line["nickname"]
  return out


def _status(owned: dict, now: datetime) -> str:
  if owned["start"] > now:
    return "scheduled"
  return "past" if owned["end"] <= now else "active"


def _my_pass_out(owned: dict, now: datetime) -> dict:
  status = _status(owned, now)
  last_day = (owned["end"] - timedelta(seconds=1)).date()
  out = {"pass_ref": owned["pass_ref"], "name": owned["name"], "status": status,
         "bought": _moment(owned["bought"]), "ends": f"the end of {_day(last_day)}"}
  if status == "scheduled":
    cutoff = owned["start"] - CANCEL_CUTOFF
    out.update(starts=_day(owned["start"].date()), can_cancel_in_app=now < cutoff,
               cancel_in_app_by=_moment(cutoff))
  return out


def _pass_out(p: dict) -> dict:
  return {"pass_id": p["pass_id"], "name": p["name"], "price_rm": p["price_rm"],
          "price_spoken": f"{p['price_rm']} ringgit", "days": p["days"], "data": p["data"],
          "covers": p["covers_spoken"], "can_book_ahead": p["can_book_ahead"],
          "travel_insurance": p["travel_insurance"]}


def _destination_step(data: dict, destinations) -> str | None:
  """Fills destination fields in data; returns a next_step if the places need attention."""
  countries, unclear, unsupported = _resolve(destinations)
  data["destinations"] = countries
  if unclear:
    data["unclear_places"] = unclear
  if unsupported:
    data["unsupported_places"] = unsupported
  pay_per_use = [c for c in countries if c in PAY_PER_USE]
  if pay_per_use:
    data["pay_per_use_countries"] = pay_per_use
  if unclear or (unsupported and countries):
    return "clarify_destination"
  if not countries:
    return "destination_not_supported"
  if len(pay_per_use) == len(countries):
    return "pay_per_use_only"
  return None


def _recommend(covering: list[dict], days: int, starts_today: bool):
  """Returns (recommended, alternatives, next_step)."""
  by_length = lambda p: (p["days"], p["price_rm"])  # noqa: E731
  if all(p["days"] == 1 for p in covering):
    rec = min(covering, key=lambda p: p["price_rm"])
    return rec, [], "recommend_day_pass" if starts_today else "day_pass_buy_on_the_day"
  bookable = covering if starts_today else [p for p in covering if p["can_book_ahead"]]
  fits = sorted((p for p in bookable if p["days"] >= days), key=by_length)
  if fits:
    longer = [p for p in fits[1:] if p["days"] > fits[0]["days"]]
    return fits[0], longer[:1], "recommend_pass"
  any_fit = sorted((p for p in covering if p["days"] >= days), key=by_length)
  if any_fit:
    return any_fit[0], [], "buy_on_the_day"
  return max(bookable or covering, key=by_length), [], "trip_longer_than_any_pass"


# --- Operations --------------------------------------------------------------
@register("listRoamingLines")
def _list_roaming_lines(ctx: MockContext, query: dict, body: dict) -> dict:
  store = _account(ctx)
  if store is None:
    return _not_maxis()
  now = _now()
  lines = []
  for line in store["lines"].values():
    summary = {**_line_out(line), "can_buy_pass": line["can_buy_pass"], "roaming": line["roaming"],
               "has_active_pass": any(_status(o, now) == "active"
                                      for o in store["passes"][line["line_ref"]])}
    if line.get("why_not"):
      summary["why_not"] = line["why_not"]
    lines.append(summary)
  many = len(lines) > 1
  return ok({"lines": lines, "can_manage_share_lines": store["principal"],
             "next_step": "confirm_which_line_if_unclear" if many else "use_callers_own_line"})


@register("findPasses")
def _find_passes(ctx: MockContext, query: dict, body: dict) -> dict:
  store = _account(ctx)
  if store is None:
    return _not_maxis()
  line = _find_line(store, query.get("line_ref"))
  if line is None:
    return _line_not_found()
  data = {"line": _line_out(line)}
  if not line["can_buy_pass"]:
    return ok({**data, "why_not": line["why_not"], "next_step": "line_cannot_buy_pass"})
  step = _destination_step(data, query.get("destinations"))
  if step:
    return ok({**data, "next_step": step})
  countries = [c for c in data["destinations"] if c not in PAY_PER_USE]
  covering = [p for p in PASSES.values() if set(countries) <= p["covers"]]
  if not covering:
    return ok({**data, "next_step": "no_pass_covers_all_destinations"})
  notices = [NOTICES[c] for c in countries if c in NOTICES]
  if notices:
    data["notices"] = notices
  trip, step, days = _trip(query.get("start_date"), query.get("end_date"), query.get("days"))
  data["trip"] = trip
  if step == "ask_trip_dates":  # "how much is roaming in Japan?": cheapest per length
    cheapest: dict[int, dict] = {}
    for p in covering:
      if p["days"] not in cheapest or p["price_rm"] < cheapest[p["days"]]["price_rm"]:
        cheapest[p["days"]] = p
    data["options"] = [_pass_out(cheapest[d]) for d in sorted(cheapest)]
  if step:
    return ok({**data, "next_step": step})
  rec, alternatives, step = _recommend(covering, days, trip["starts_today"])
  data["recommended"] = _pass_out(rec)
  if alternatives:
    data["alternatives"] = [_pass_out(p) for p in alternatives]
  return ok({**data, "next_step": step})


@register("buyPass")
def _buy_pass(ctx: MockContext, query: dict, body: dict) -> dict:
  for field in ("pass_id", "destinations", "start_date"):
    if not body.get(field):
      raise BadRequest(f"{field} is required.")
  store = _account(ctx)
  if store is None:
    return _not_maxis()
  line = _find_line(store, body.get("line_ref"))
  if line is None:
    return _line_not_found()
  data = {"line": _line_out(line)}
  if not line["can_buy_pass"]:
    return ok({**data, "why_not": line["why_not"], "next_step": "line_cannot_buy_pass"})
  step = _destination_step(data, body["destinations"])
  if step:
    return ok({**data, "next_step": step})
  countries = [c for c in data["destinations"] if c not in PAY_PER_USE]
  p = PASSES.get(str(body["pass_id"]))
  if p is None or not set(countries) <= p["covers"]:
    return ok({**data, "next_step": "pass_not_available_for_this_trip"})
  data["pass"] = _pass_out(p)
  trip, step, _ = _trip(body["start_date"], None, p["days"])
  data["trip"] = trip
  if step:
    return ok({**data, "next_step": step})
  starts_today = trip["starts_today"]
  if not starts_today and not p["can_book_ahead"]:
    return ok({**data, "next_step": "pass_cannot_be_booked_ahead"})
  data["starts"] = "now" if starts_today else trip["starts"]
  if body.get("confirmed") is not True:
    return ok({**data, "roaming_switched_on_first": line["roaming"] != "on",
               "next_step": "confirm_purchase"})
  outcome = "active_now" if starts_today else "scheduled"
  key = ("buy", line["line_ref"], p["pass_id"], trip["start_date"])
  if key in store["done"]:
    return ok({**data, "outcome": outcome, "repeated_request": True, "sms_when_active": True,
               "next_step": "purchase_done"})
  switched = line["roaming"] != "on"
  line["roaming"] = "on"
  now = _now()
  start = now if starts_today else _midnight(date.fromisoformat(trip["start_date"]))
  store["seq"] += 1
  store["passes"][line["line_ref"]].append(_owned(f"PS-{store['seq']}", p["pass_id"], start, now))
  store["done"].add(key)
  result = {**data, "outcome": outcome, "roaming_switched_on": switched, "sms_when_active": True,
            "next_step": "purchase_done"}
  if switched:
    result["ready_in_minutes"] = SWITCH_MINUTES
  if p["travel_insurance"]:
    result["offer_whatsapp"] = "insurance"
  return ok(result)


@register("listMyPasses")
def _list_my_passes(ctx: MockContext, query: dict, body: dict) -> dict:
  when = query.get("when") or "all"
  if when not in ("scheduled", "active", "past", "all"):
    raise BadRequest("when must be scheduled, active, past or all.")
  store = _account(ctx)
  if store is None:
    return _not_maxis()
  line = _find_line(store, query.get("line_ref"))
  if line is None:
    return _line_not_found()
  now = _now()
  order = {"scheduled": 0, "active": 1, "past": 2}
  passes = [_my_pass_out(o, now) for o in store["passes"][line["line_ref"]]]
  passes = sorted((p for p in passes if when in ("all", p["status"])),
                  key=lambda p: order[p["status"]])
  step = ("no_passes_found" if not passes else
          "confirm_this_pass" if len(passes) == 1 else "ask_which_pass")
  return ok({"line": _line_out(line), "passes": passes, "history_available": True,
             "next_step": step})


@register("helpWithWrongPass")
def _help_with_wrong_pass(ctx: MockContext, query: dict, body: dict) -> dict:
  if not body.get("pass_ref"):
    raise BadRequest("pass_ref is required.")
  store = _account(ctx)
  if store is None:
    return _not_maxis()
  line = _find_line(store, body.get("line_ref"))
  if line is None:
    return _line_not_found()
  data = {"line": _line_out(line)}
  owned = next((o for o in store["passes"][line["line_ref"]]
                if o["pass_ref"] == str(body["pass_ref"])), None)
  if owned is None:
    return ok({**data, "next_step": "pass_not_found_list_passes_again"})
  mine = _my_pass_out(owned, _now())
  data["pass"] = mine
  if mine["status"] == "scheduled":
    if mine["can_cancel_in_app"]:
      return ok({**data, "offer_whatsapp": "manage_passes", "next_step": "explain_cancel_in_app"})
    return ok({**data, "next_step": "too_late_to_cancel_offer_agent"})
  return ok({**data, "next_step": "no_refund_offer_agent"})


@register("switchRoaming")
def _switch_roaming(ctx: MockContext, query: dict, body: dict) -> dict:
  turn = body.get("turn")
  if turn not in ("on", "off"):
    raise BadRequest("turn must be on or off.")
  store = _account(ctx)
  if store is None:
    return _not_maxis()
  line = _find_line(store, body.get("line_ref"))
  if line is None:
    return _line_not_found()
  data = {"line": _line_out(line), "action": f"switch_{turn}", "roaming": line["roaming"]}
  if not line["can_buy_pass"]:
    return ok({**data, "why_not": line["why_not"], "next_step": "line_cannot_use_roaming"})
  key = ("switch", line["line_ref"], turn)
  if key in store["done"]:
    return ok({**data, "repeated_request": True, "ready_in_minutes": SWITCH_MINUTES,
               "next_step": "switch_requested"})
  if line["roaming"] == turn:
    return ok({**data, "next_step": f"already_{turn}"})
  if turn == "off" and body.get("acknowledged_active_pass") is not True:
    now = _now()
    live = [_my_pass_out(o, now) for o in store["passes"][line["line_ref"]]
            if _status(o, now) in ("active", "scheduled")]
    if live:
      return ok({**data, "passes": live, "next_step": "warn_passes_will_not_work"})
  if body.get("confirmed") is not True:
    return ok({**data, "ready_in_minutes": SWITCH_MINUTES, "next_step": "confirm_switch"})
  line["roaming"] = turn
  store["done"].add(key)
  return ok({**data, "ready_in_minutes": SWITCH_MINUTES, "next_step": "switch_requested"})
