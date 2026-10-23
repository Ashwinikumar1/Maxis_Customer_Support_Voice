"""Business rules of the mock gateway (SID-02, SID-03, SID-04 and roaming.yaml next_step values)."""

from tests.conftest import NOT_MAXIS, PREPAID, PRINCIPAL, SLOW, TOO_LATE, TRAVELLER


def step(resp):
  assert resp["status"] == "success", resp
  return resp["responseData"]["next_step"]


def violation(resp):
  assert resp["status"] == "fail", resp
  return resp["violations"][0]["code"], resp["violations"][0]["action"]


# ---------------------------------------------------------------- caller identity
def test_withheld_and_non_maxis_callers(run):
  assert violation(run("find_passes", caller_id="", destinations="Japan")) == (
      "CALLER_NOT_IDENTIFIED", "general_help_only")
  assert violation(run("list_roaming_lines", caller_id=NOT_MAXIS)) == ("NOT_A_MAXIS_NUMBER", "general_help_only")


def test_local_number_format_is_normalised(run):
  assert step(run("list_roaming_lines", caller_id="0100000001")) == "confirm_which_line_if_unclear"


# ---------------------------------------------------------------- lines
def test_principal_sees_share_lines(run):
  data = run("list_roaming_lines", caller_id=PRINCIPAL)["responseData"]
  assert data["can_manage_share_lines"] is True
  assert [line["line_ref"] for line in data["lines"]] == ["L-P1-OWN", "L-P1-S1", "L-P1-S2"]
  assert data["lines"][1]["nickname"] == "Adam's phone"
  assert data["lines"][2]["why_not"] == "roaming_not_allowed_on_this_line"
  assert data["next_step"] == "confirm_which_line_if_unclear"


def test_single_line_and_foreign_line_ref(run):
  assert step(run("list_roaming_lines", caller_id=TRAVELLER)) == "use_callers_own_line"
  # A caller can't act on someone else's line.
  assert violation(run("find_passes", caller_id=TRAVELLER, destinations="Japan", line_ref="L-P1-S1")) == (
      "LINE_NOT_FOUND", "list_lines_again")


def test_prepaid_and_blocked_share_line_cannot_buy(run):
  data = run("find_passes", caller_id=PREPAID, destinations="Japan")["responseData"]
  assert (data["next_step"], data["why_not"]) == ("line_cannot_buy_pass", "prepaid_line")
  data = run("find_passes", caller_id=PRINCIPAL, destinations="Japan", line_ref="L-P1-S2")["responseData"]
  assert (data["next_step"], data["why_not"]) == ("line_cannot_buy_pass", "roaming_not_allowed_on_this_line")


# ---------------------------------------------------------------- findPasses
def test_just_asking_lists_options_without_recommending(run):
  data = run("find_passes", caller_id=PRINCIPAL, destinations="Japan")["responseData"]
  assert data["next_step"] == "ask_trip_dates"
  assert "recommended" not in data
  assert [p["days"] for p in data["options"]] == [1, 3, 7, 15, 30]
  assert data["options"][2]["price_spoken"] == "fifty-eight ringgit"


def test_start_without_length_asks_for_length(run, clock):
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Tokyo", start_date=clock.day(2))) == \
      "ask_trip_length"


def test_recommendation_for_a_trip(run, clock):
  data = run("find_passes", caller_id=PRINCIPAL, destinations="Tokyo, Seoul", start_date=clock.day(2),
             days=7)["responseData"]
  assert data["next_step"] == "recommend_pass"
  assert data["destinations"] == ["Japan", "South Korea"]
  assert data["recommended"]["pass_id"] == "DEMO-ASIAPACIFIC-7D"
  assert data["trip"]["days"] == 7 and data["trip"]["starts"] == "Friday, 23 October"
  assert all(not k.startswith("_") for k in data["recommended"])


def test_end_date_sets_trip_length(run, clock):
  data = run("find_passes", caller_id=PRINCIPAL, destinations="Bangkok", start_date=clock.day(1),
             end_date=clock.day(4))["responseData"]
  assert data["trip"]["days"] == 4
  assert data["recommended"]["days"] == 7  # cheapest ASEAN pass that covers 4 days


def test_destination_problems(run):
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Georgia")) == "clarify_destination"
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Antarctica")) == "destination_not_supported"
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Tokyo, Atlantis")) == "clarify_destination"
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Maldives")) == "pay_per_use_only"
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Japan, Nepal")) == \
      "no_pass_covers_all_destinations"


def test_notices_and_day_pass_countries(run, clock):
  data = run("find_passes", caller_id=PRINCIPAL, destinations="Mecca", start_date=clock.day(1),
             days=7)["responseData"]
  assert data["notices"] and data["next_step"] == "recommend_pass"
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Kathmandu", start_date=clock.day(0),
                  days=3)) == "recommend_day_pass"
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Kathmandu", start_date=clock.day(3),
                  days=3)) == "day_pass_buy_on_the_day"


def test_trip_date_rules(run, clock):
  find = lambda **kw: step(run("find_passes", caller_id=PRINCIPAL, destinations="Japan", **kw))  # noqa: E731
  assert find(start_date=clock.day(-1), days=3) == "start_date_in_past"
  assert find(start_date=clock.day(15), days=3) == "beyond_booking_window"
  assert find(start_date=clock.day(14), days=3) == "recommend_pass"
  assert find(start_date=clock.day(5), end_date=clock.day(3)) == "end_before_start"
  assert find(start_date=clock.day(1), days=31) == "trip_too_long"
  clock.advance(hours=10)  # 8 pm: tomorrow's midnight is under 6 h away
  assert find(start_date=clock.day(1), days=3) == "too_late_to_schedule"
  assert find(start_date=clock.day(0), days=3) == "recommend_pass"  # buying for today is fine


def test_bad_date_is_invalid_request(run):
  assert violation(run("find_passes", caller_id=PRINCIPAL, destinations="Japan", start_date="next friday",
                       days=3)) == ("INVALID_REQUEST", "fix_the_request")


# ---------------------------------------------------------------- buyPass
def test_two_step_purchase_switches_roaming_on(run, clock):
  args = dict(caller_id=PRINCIPAL, pass_id="DEMO-ASIAPACIFIC-7D", destinations="Tokyo", start_date=clock.day(2))
  summary = run("buy_pass", **args)["responseData"]
  assert summary["next_step"] == "confirm_purchase"
  assert summary["roaming_switched_on_first"] is True
  assert summary["starts"] == "Friday, 23 October"
  # Nothing is bought without confirmed.
  assert run("list_my_passes", caller_id=PRINCIPAL)["responseData"]["passes"] == []

  done = run("buy_pass", **args, confirmed=True)["responseData"]
  assert done["next_step"] == "purchase_done"
  assert (done["outcome"], done["roaming_switched_on"], done["offer_whatsapp"]) == ("scheduled", True, "insurance")
  assert run("list_roaming_lines", caller_id=PRINCIPAL)["responseData"]["lines"][0]["roaming"] == "on"

  again = run("buy_pass", **args, confirmed=True)["responseData"]
  assert again["repeated_request"] is True
  assert len(run("list_my_passes", caller_id=PRINCIPAL)["responseData"]["passes"]) == 1  # never charged twice


def test_buy_today_is_active_now(run, clock):
  done = run("buy_pass", caller_id=TRAVELLER, pass_id="DEMO-ASEAN-3D", destinations="Bali",
             start_date=clock.day(0), confirmed=True)["responseData"]
  assert (done["outcome"], done["roaming_switched_on"]) == ("active_now", False)
  assert "offer_whatsapp" not in done  # 3-day passes have no insurance offer


def test_buy_for_share_line(run, clock):
  done = run("buy_pass", caller_id=PRINCIPAL, line_ref="L-P1-S1", pass_id="DEMO-ASEAN-7D",
             destinations="Singapore", start_date=clock.day(1), confirmed=True)["responseData"]
  assert done["next_step"] == "purchase_done" and done["line"]["nickname"] == "Adam's phone"
  assert run("list_my_passes", caller_id=PRINCIPAL)["responseData"]["passes"] == []  # own line untouched


def test_slow_roaming_switch_means_pass_not_bought_yet(run, clock):
  args = dict(caller_id=SLOW, pass_id="DEMO-ASEAN-3D", destinations="Bali", start_date=clock.day(0),
              confirmed=True)
  first = run("buy_pass", **args)["responseData"]
  assert first["next_step"] == "roaming_still_switching_on"
  assert run("list_my_passes", caller_id=SLOW)["responseData"]["passes"] == []
  clock.advance(seconds=90)
  second = run("buy_pass", **args)["responseData"]
  assert second["next_step"] == "purchase_done" and second["roaming_switched_on"] is True


def test_purchase_rules(run, clock):
  buy = lambda **kw: step(run("buy_pass", caller_id=PRINCIPAL, **kw))  # noqa: E731
  assert buy(pass_id="DEMO-ASEAN-7D", destinations="Tokyo", start_date=clock.day(1)) == \
      "pass_not_available_for_this_trip"   # ASEAN pass doesn't cover Japan
  assert buy(pass_id="DEMO-NOPE", destinations="Tokyo", start_date=clock.day(1)) == \
      "pass_not_available_for_this_trip"
  assert buy(pass_id="DEMO-ASEAN-1D", destinations="Bali", start_date=clock.day(2)) == \
      "pass_cannot_be_booked_ahead"
  assert buy(pass_id="DEMO-ASEAN-7D", destinations="Bali", start_date=clock.day(-1)) == "start_date_in_past"


# ---------------------------------------------------------------- passes and wrong pass (SID-03)
def test_list_my_passes_filters(run):
  data = run("list_my_passes", caller_id=TRAVELLER)["responseData"]
  assert [p["status"] for p in data["passes"]] == ["scheduled", "active", "past"]
  assert data["next_step"] == "ask_which_pass"
  scheduled = run("list_my_passes", caller_id=TRAVELLER, when="scheduled")["responseData"]
  assert scheduled["next_step"] == "confirm_this_pass"
  assert scheduled["passes"][0]["can_cancel_in_app"] is True
  assert scheduled["passes"][0]["cancel_in_app_by"].endswith("at 10 pm")


def test_wrong_pass_outcomes(run):
  ok = run("help_with_wrong_pass", caller_id=TRAVELLER, pass_ref="P-T1-1")["responseData"]
  assert (ok["next_step"], ok["offer_whatsapp"]) == ("explain_cancel_in_app", "manage_passes")
  assert step(run("help_with_wrong_pass", caller_id=TRAVELLER, pass_ref="P-T1-2")) == "no_refund_offer_agent"
  assert step(run("help_with_wrong_pass", caller_id=TRAVELLER, pass_ref="P-T1-3")) == "no_refund_offer_agent"
  assert step(run("help_with_wrong_pass", caller_id=TOO_LATE, pass_ref="P-L1-1")) == \
      "too_late_to_cancel_offer_agent"
  assert step(run("help_with_wrong_pass", caller_id=TRAVELLER, pass_ref="P-X")) == \
      "pass_not_found_list_passes_again"


def test_history_off_matches_production(run, gw):
  gw.pass_history = False
  data = run("list_my_passes", caller_id=TRAVELLER)["responseData"]
  assert data["history_available"] is False
  assert [p["status"] for p in data["passes"]] == ["scheduled"]


# ---------------------------------------------------------------- switch roaming (SID-04)
def test_switch_off_with_passes_warns_then_confirms(run, clock):
  assert step(run("switch_roaming", caller_id=TRAVELLER, turn="off")) == "warn_passes_will_not_work"
  assert step(run("switch_roaming", caller_id=TRAVELLER, turn="off", acknowledged_active_pass=True)) == \
      "confirm_switch"
  done = run("switch_roaming", caller_id=TRAVELLER, turn="off", acknowledged_active_pass=True,
             confirmed=True)["responseData"]
  assert (done["next_step"], done["ready_in_minutes"]) == ("switch_requested", 5)
  assert step(run("switch_roaming", caller_id=TRAVELLER, turn="on")) == "still_setting_up"
  clock.advance(minutes=1)
  assert step(run("switch_roaming", caller_id=TRAVELLER, turn="off")) == "still_setting_up"  # as told: ~5 min
  clock.advance(minutes=5)
  assert step(run("switch_roaming", caller_id=TRAVELLER, turn="off")) == "already_off"


def test_switch_on_and_duplicate(run):
  assert step(run("switch_roaming", caller_id=PRINCIPAL, turn="off")) == "already_off"
  assert step(run("switch_roaming", caller_id=PRINCIPAL, turn="on")) == "confirm_switch"
  assert step(run("switch_roaming", caller_id=PRINCIPAL, turn="on", confirmed=True)) == "switch_requested"
  again = run("switch_roaming", caller_id=PRINCIPAL, turn="on", confirmed=True)["responseData"]
  assert again["repeated_request"] is True
  assert step(run("switch_roaming", caller_id=PREPAID, turn="on")) == "line_cannot_use_roaming"


# ---------------------------------------------------------------- WhatsApp
def test_whatsapp_once_per_kind_per_call(run):
  assert step(run("send_whatsapp_link", caller_id=TRAVELLER, kind="manage_passes", conversation_id="c1")) == \
      "tell_caller_it_is_on_whatsapp"
  assert step(run("send_whatsapp_link", caller_id=TRAVELLER, kind="manage_passes", conversation_id="c1")) == \
      "tell_caller_it_was_already_sent"
  assert step(run("send_whatsapp_link", caller_id=TRAVELLER, kind="manage_passes", conversation_id="c2")) == \
      "tell_caller_it_is_on_whatsapp"
  assert step(run("send_whatsapp_link", caller_id=TRAVELLER, kind="pay_bill", conversation_id="c1")) == \
      "explain_link_not_available"


# ---------------------------------------------------------------- admin
def test_fail_next_and_reset(run, gw, clock):
  gw.fail_next("findPasses", 1)
  assert violation(run("find_passes", caller_id=PRINCIPAL, destinations="Japan")) == (
      "BACKEND_UNAVAILABLE", "apologise_and_offer_agent")
  assert step(run("find_passes", caller_id=PRINCIPAL, destinations="Japan")) == "ask_trip_dates"
  run("buy_pass", caller_id=PRINCIPAL, pass_id="DEMO-ASEAN-7D", destinations="Bali", start_date=clock.day(1),
      confirmed=True)
  gw.reset(PRINCIPAL)
  assert run("list_my_passes", caller_id=PRINCIPAL)["responseData"]["passes"] == []
  assert run("list_roaming_lines", caller_id=PRINCIPAL)["responseData"]["lines"][0]["roaming"] == "off"
