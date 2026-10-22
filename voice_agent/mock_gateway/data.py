"""DEMO DATA for the mock agent gateway. Not real Maxis passes, prices or customers.

Everything here is invented so the roaming agent can be demonstrated before
Maxis shares its gateway and APIs. Pass names, prices, countries per zone and
the test callers are placeholders; replace nothing here for production: the
real gateway (GATEWAY_URL) owns all of it.
"""

from datetime import timedelta

# --- Places: what a caller might say -> country. Lower-case keys.
CITY_TO_COUNTRY = {
    "tokyo": "Japan", "osaka": "Japan", "kyoto": "Japan", "sapporo": "Japan", "hokkaido": "Japan",
    "seoul": "South Korea", "busan": "South Korea", "jeju": "South Korea",
    "bangkok": "Thailand", "phuket": "Thailand", "chiang mai": "Thailand", "hat yai": "Thailand",
    "bali": "Indonesia", "jakarta": "Indonesia", "medan": "Indonesia", "batam": "Indonesia",
    "hanoi": "Vietnam", "ho chi minh": "Vietnam", "ho chi minh city": "Vietnam", "da nang": "Vietnam",
    "manila": "Philippines", "cebu": "Philippines",
    "phnom penh": "Cambodia", "siem reap": "Cambodia",
    "beijing": "China", "shanghai": "China", "guangzhou": "China", "shenzhen": "China",
    "hong kong": "Hong Kong", "macau": "Macau", "taipei": "Taiwan",
    "sydney": "Australia", "melbourne": "Australia", "perth": "Australia",
    "london": "United Kingdom", "manchester": "United Kingdom", "uk": "United Kingdom",
    "england": "United Kingdom", "britain": "United Kingdom",
    "paris": "France", "rome": "Italy", "milan": "Italy", "zurich": "Switzerland",
    "new york": "United States", "los angeles": "United States", "usa": "United States",
    "us": "United States", "america": "United States",
    "mecca": "Saudi Arabia", "makkah": "Saudi Arabia", "medina": "Saudi Arabia", "madinah": "Saudi Arabia",
    "dubai": "United Arab Emirates", "uae": "United Arab Emirates",
    "kathmandu": "Nepal", "male": "Maldives",
    "korea": "South Korea",
}

# Places that could be more than one country: ask the caller.
UNCLEAR_PLACES = {
    "georgia": ["Georgia (the country)", "Georgia in the United States"],
    "punjab": ["India", "Pakistan"],
    "kashmir": ["India", "Pakistan"],
}

# Not roaming destinations in the demo.
UNSUPPORTED = {"north korea", "antarctica", "malaysia", "kuala lumpur", "penang", "johor bahru"}

ZONES = {
    "ASEAN": ["Singapore", "Thailand", "Indonesia", "Vietnam", "Philippines", "Brunei", "Cambodia",
              "Laos", "Myanmar"],
    "Asia Pacific": ["Japan", "South Korea", "China", "Hong Kong", "Macau", "Taiwan", "India",
                     "Australia", "New Zealand", "Pakistan"],
    "World": ["United Kingdom", "France", "Italy", "Switzerland", "Germany", "Spain", "United States",
              "Canada", "Saudi Arabia", "United Arab Emirates", "Turkey", "Georgia"],
}
# A pass for a zone also covers the zones listed before it (Asia Pacific covers ASEAN, World covers all).
ZONE_ORDER = ["ASEAN", "Asia Pacific", "World"]
DAY_PASS_ONLY = {"Nepal"}            # only a 1-day pass, bought on the day
PAY_PER_USE = {"Maldives"}           # no passes at all
ALL_COUNTRIES = {c for cs in ZONES.values() for c in cs} | DAY_PASS_ONLY | PAY_PER_USE

# Official notices (production reads these from Remote Config; none are connected yet).
NOTICES = {
    "Saudi Arabia": "During the Hajj season coverage around Mecca can be busy, so data may be slower.",
}

# --- Pass catalogue (DEMO prices). price_rm is per pass.
_PRICES = {
    "ASEAN": {1: 10, 3: 25, 7: 38, 15: 58, 30: 88},
    "Asia Pacific": {1: 15, 3: 35, 7: 58, 15: 88, 30: 128},
    "World": {1: 25, 3: 55, 7: 88, 15: 138, 30: 198},
}
_DATA = {1: "2GB", 3: "6GB", 7: "15GB", 15: "30GB", 30: "50GB"}

_NUMBER_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight",
    9: "nine", 10: "ten", 11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
    16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty", 30: "thirty",
    40: "forty", 50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
}


def number_words(n: int) -> str:
  """Spoken English for 0-999 (enough for demo prices)."""
  if n < 20:
    return _NUMBER_WORDS[n]
  if n < 100:
    tens, ones = divmod(n, 10)
    return _NUMBER_WORDS[tens * 10] + ("" if not ones else "-" + _NUMBER_WORDS[ones])
  hundreds, rest = divmod(n, 100)
  return _NUMBER_WORDS[hundreds] + " hundred" + ("" if not rest else " and " + number_words(rest))


def _pass(zone: str, days: int) -> dict:
  price = _PRICES[zone][days]
  label = "1 Day" if days == 1 else f"{days} Days"
  return {
      "pass_id": f"DEMO-{zone.replace(' ', '').upper()}-{days}D",
      "name": f"{zone} Pass {label}",
      "price_rm": price,
      "price_spoken": f"{number_words(price)} ringgit",
      "days": days,
      "data": _DATA[days],
      "covers": _spoken_list(_zone_countries(zone)),
      "can_book_ahead": days > 1,
      "travel_insurance": days >= 7,
      "_zone": zone,
  }


def _zone_countries(zone: str) -> list[str]:
  out = []
  for z in ZONE_ORDER[: ZONE_ORDER.index(zone) + 1]:
    out += ZONES[z]
  return out


def _spoken_list(items: list[str]) -> str:
  if len(items) > 6:
    return ", ".join(items[:5]) + f" and {len(items) - 5} more countries"
  return ", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else items[0]


CATALOGUE = [_pass(z, d) for z in ZONE_ORDER for d in (1, 3, 7, 15, 30)]
CATALOGUE.append({
    "pass_id": "DEMO-NEPAL-1D", "name": "Nepal Day Pass", "price_rm": 20, "price_spoken": "twenty ringgit",
    "days": 1, "data": "1GB", "covers": "Nepal", "can_book_ahead": False, "travel_insurance": False,
    "_zone": "Nepal",
})


def pass_covers(p: dict) -> set[str]:
  if p["_zone"] == "Nepal":
    return {"Nepal"}
  return set(_zone_countries(p["_zone"]))


# --- Booking rules (production defaults while Remote Config isn't connected).
BOOKING_WINDOW_DAYS = 14
MAX_TRIP_DAYS = 30
SCHEDULE_MIN_LEAD = timedelta(hours=6)
CANCEL_IN_APP_CUTOFF = timedelta(hours=2)
BLOCKED_START_DATES: set[str] = set()   # YYYY-MM-DD

# --- Test callers. MSISDNs are fake test numbers (60 10 000 xxxx is not a live range in the demo).
# "seed" builds each line's roaming state and passes relative to the time of reset.
TEST_CALLERS = [
    {
        "key": "principal",
        "label": "Principal line + 2 share lines, roaming off",
        "msisdn": "60100000001",
        "hint": "Buy a pass (roaming gets switched on first); buy for 'Adam's phone'; the other share line can't roam.",
    },
    {
        "key": "traveller",
        "label": "Single line, roaming on, scheduled + active passes",
        "msisdn": "60100000002",
        "hint": "Wrong pass that can still be cancelled in the app; switch roaming off with passes (warning).",
    },
    {
        "key": "too_late",
        "label": "Scheduled pass starting in 1 hour",
        "msisdn": "60100000003",
        "hint": "Wrong pass, too late to cancel in the app -> offers a person.",
    },
    {
        "key": "slow_switch",
        "label": "Roaming off, slow to switch on",
        "msisdn": "60100000004",
        "hint": "Buy a pass: roaming is still switching on, pass NOT bought yet; retry after ~1 minute.",
    },
    {
        "key": "prepaid",
        "label": "Hotlink Prepaid line",
        "msisdn": "60100000005",
        "hint": "Prepaid can't buy roaming passes here.",
    },
    {
        "key": "not_maxis",
        "label": "Not a Maxis number",
        "msisdn": "60190000009",
        "hint": "Account tools return NOT_A_MAXIS_NUMBER; general questions still work.",
    },
    {
        "key": "withheld",
        "label": "Withheld number",
        "msisdn": "",
        "hint": "No caller ID: CALLER_NOT_IDENTIFIED; general questions only.",
    },
]


def seed_accounts(now_myt) -> dict:
  """Fresh demo accounts keyed by MSISDN. now_myt: timezone-aware datetime in Malaysia time."""

  def line(ref, label, own, *, nickname=None, mobile=True, prepaid=False, allowed=True,
           roaming="off", switch_seconds=3):
    return {"line_ref": ref, "label": label, "nickname": nickname, "is_callers_own": own,
            "mobile": mobile, "prepaid": prepaid, "roaming_allowed": allowed, "roaming": roaming,
            "roaming_ready_at": None, "switch_seconds": switch_seconds}

  def mypass(ref, pass_id, status, start, end, bought):
    return {"pass_ref": ref, "pass_id": pass_id, "status": status, "start": start, "end": end, "bought": bought}

  start_in_5_days = now_myt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=5)
  return {
      "60100000001": {
          "principal": True,
          "lines": [
              line("L-P1-OWN", "your own line", True),
              line("L-P1-S1", "the share line ending in two one", False, nickname="Adam's phone"),
              line("L-P1-S2", "the share line ending in seven seven", False, allowed=False),
          ],
          "passes": {"L-P1-OWN": [], "L-P1-S1": [], "L-P1-S2": []},
      },
      "60100000002": {
          "principal": False,
          "lines": [line("L-T1-OWN", "your own line", True, roaming="on")],
          "passes": {"L-T1-OWN": [
              mypass("P-T1-1", "DEMO-ASIAPACIFIC-7D", "scheduled", start_in_5_days,
                     start_in_5_days + timedelta(days=7), now_myt - timedelta(days=1)),
              mypass("P-T1-2", "DEMO-ASEAN-3D", "active", now_myt - timedelta(days=1),
                     now_myt + timedelta(days=2), now_myt - timedelta(days=1, hours=2)),
              mypass("P-T1-3", "DEMO-ASEAN-7D", "past", now_myt - timedelta(days=30),
                     now_myt - timedelta(days=23), now_myt - timedelta(days=31)),
          ]},
      },
      "60100000003": {
          "principal": False,
          "lines": [line("L-L1-OWN", "your own line", True, roaming="on")],
          "passes": {"L-L1-OWN": [
              mypass("P-L1-1", "DEMO-ASEAN-7D", "scheduled", now_myt + timedelta(hours=1),
                     now_myt + timedelta(days=7, hours=1), now_myt - timedelta(days=2)),
          ]},
      },
      "60100000004": {
          "principal": False,
          "lines": [line("L-S1-OWN", "your own line", True, switch_seconds=60)],
          "passes": {"L-S1-OWN": []},
      },
      "60100000005": {
          "principal": False,
          "lines": [line("L-H1-OWN", "your own line", True, prepaid=True)],
          "passes": {"L-H1-OWN": []},
      },
  }
