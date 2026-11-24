from __future__ import annotations

from datetime import datetime, timedelta

from tests.support import ApiCase, MAIN, ROOT, enrollment, observations


def utc(text: str) -> str:
    return datetime.fromisoformat(text).isoformat()


def same_instant(left: str, right: str) -> bool:
    return datetime.fromisoformat(left.replace("Z", "+00:00")) == datetime.fromisoformat(
        right.replace("Z", "+00:00")
    )


def notice(
    *,
    notice_id: str = "grid-one",
    site_id: str = "north",
    starts: str = "2026-10-25T09:00:00+00:00",
    ends: str = "2026-10-25T10:00:00+00:00",
    scope=None,
    source: str = "grid-operator",
    revision: int = 1,
    revocation=None,
    void: bool = False,
):
    return {
        "notice_id": notice_id,
        "revision": revision,
        "site_id": site_id,
        "starts_at": starts,
        "ends_at": ends,
        "scope": scope or {"mode": "site", "unit_ids": [], "cells": [], "arms": []},
        "source": source,
        "reason": "winter load shedding",
        "source_ref": None,
        "revocation": revocation,
        "void": void,
    }


def ack(unit_id: str, *, notice_id: str = "grid-one", status: str = "affected",
        affected_from: str = "2026-10-25T09:00:00+00:00",
        affected_until: str = "2026-10-25T10:00:00+00:00",
        revision: int = 1, void: bool = False):
    return {
        "ack_id": f"{unit_id}-{notice_id}",
        "revision": revision,
        "notice_id": notice_id,
        "unit_id": unit_id,
        "status": status,
        "observed_at": "2026-10-25T09:05:00+00:00",
        "affected_from": affected_from if status == "affected" else None,
        "affected_until": affected_until if status == "affected" else None,
        "note": None,
        "void": void,
    }


class CurtailmentCase(ApiCase):
    def east(self):
        self.register({"site_id": "south", "market_id": "alpine",
                       "climate": "cold", "timezone": "Europe/Berlin"})

    def two_site_units(self):
        return [
            enrollment("s-one", "standard"),
            enrollment("s-two", "standard"),
            enrollment("a-one", "assisted"),
            enrollment("a-two", "assisted"),
        ]

    def post_notices(self, notices, delivery="notices"):
        return self.call("POST", ROOT + "/curtailment/notices", {
            "delivery_id": delivery, "notices": notices,
        })

    def post_acks(self, acknowledgments, delivery="acks"):
        return self.call("POST", ROOT + "/curtailment/acks", {
            "delivery_id": delivery, "acknowledgments": acknowledgments,
        })


class NoticeLifecycleTest(CurtailmentCase):
    def test_notice_revisions_keep_effective_interval_and_revocation(self):
        self.setup_trial()
        first = self.post_notices([notice()])
        self.assertEqual(first["accepted"], 1)
        cut_one = first["current_cut"]
        revoked = notice(
            revision=2,
            revocation={
                "revoked_at": "2026-10-25T09:20:00+00:00",
                "by": "operator",
                "reason": "command withdrawn",
            },
        )
        cut_two = self.post_notices([revoked], delivery="notice-revoke")["current_cut"]
        history = self.call("GET", ROOT + "/curtailment/notices/grid-one")["revisions"]
        self.assertEqual([item["notice"]["revision"] for item in history], [1, 2])
        as_of = self.call("GET", ROOT + "/curtailment/notices", params={"cut": cut_one})
        self.assertEqual(as_of["notices"][0]["state"], "active")
        after = self.call("GET", ROOT + "/curtailment/notices", params={"cut": cut_two})
        self.assertEqual(after["notices"][0]["state"], "revoked")
        self.assertTrue(same_instant(after["notices"][0]["effective_ends_at"],
                                     "2026-10-25T09:20:00+00:00"))

    def test_retraction_is_visible_and_not_executed_exposure(self):
        self.setup_trial()
        self.post_notices([notice()])
        self.post_notices(
            [{**notice(revision=2), "void": True, "revocation": None}],
            delivery="notice-retract",
        )
        ledger = self.call("GET", ROOT + "/curtailment/notices")
        self.assertEqual(ledger["notices"][0]["state"], "retracted")
        report = self.call("GET", ROOT + "/curtailment/exposure")
        self.assertEqual(report["units"][0]["unknown"], [])

    def test_scope_selects_units_and_rejects_foreign_site(self):
        self.create()
        self.register()
        self.register({"site_id": "west", "market_id": "alpine",
                       "climate": "cold", "timezone": "UTC"})
        units = self.two_site_units() + [enrollment("w-one", "standard", at="west")]
        self.open(self.units(units)["version"])
        scoped = notice(scope={"mode": "units", "unit_ids": ["a-one"], "cells": [], "arms": []})
        self.post_notices([scoped])
        report = self.call("GET", ROOT + "/curtailment/exposure")
        rows = {row["unit_id"]: row for row in report["units"]}
        self.assertEqual(rows["a-one"]["command_intervals"], [{
            "starts_at": "2026-10-25T09:00:00+00:00",
            "ends_at": "2026-10-25T10:00:00+00:00",
        }])
        self.assertEqual(rows["s-one"]["command_intervals"], [])
        foreign = notice(
            notice_id="grid-two",
            scope={"mode": "units", "unit_ids": ["a-one"], "cells": [], "arms": []},
        )
        self.call("POST", ROOT + "/curtailment/notices", {
            "delivery_id": "foreign", "notices": [{**foreign, "site_id": "west"}],
        }, status=422)

    def test_overlapping_notices_are_reported_without_collapsing_intervals(self):
        self.setup_trial()
        self.post_notices([
            notice(notice_id="grid-one"),
            notice(notice_id="grid-two", starts="2026-10-25T09:30:00+00:00",
                   ends="2026-10-25T10:30:00+00:00"),
        ])
        overlap = self.call("GET", ROOT + "/curtailment/overlaps")
        self.assertEqual(len(overlap["overlaps"]), 1)
        self.assertEqual(overlap["overlaps"][0]["notice_ids"], ["grid-one", "grid-two"])
        self.assertEqual(overlap["overlaps"][0]["overlap_starts_at"],
                         "2026-10-25T09:30:00+00:00")

    def test_unconfirmed_source_is_not_used_as_execution(self):
        self.setup_trial()
        self.post_notices([notice(source="unconfirmed")])
        report = self.call("GET", ROOT + "/curtailment/exposure")
        self.assertTrue(all(row["executed_intervals"] == [] for row in report["units"]))
        self.assertTrue(all("notice_source_unconfirmed" in row["issues"]
                            for row in report["units"]))

    def test_unconfirmed_site_period_is_unknown_for_both_arms(self):
        self.setup_trial()
        self.post_notices([notice(source="unconfirmed")])
        self.deliver("signals", self.green_events())
        result = self.draft()["market_results"][0]
        arms = result["cells"][0]["arms"]
        self.assertEqual(arms["standard"]["primary_cycles"], 0)
        self.assertEqual(arms["assisted"]["primary_cycles"], 0)
        self.assertEqual(arms["assisted"]["exposure_unknown_cycles"], 2)
        self.assertEqual(arms["standard"]["exposure_unknown_cycles"], 2)

    def test_scoped_unconfirmed_notice_does_not_taint_the_other_units(self):
        self.setup_trial()
        scoped = notice(
            source="unconfirmed",
            scope={"mode": "units", "unit_ids": ["a-one"], "cells": [], "arms": []},
        )
        self.post_notices([scoped])
        self.deliver("signals", self.green_events())
        result = self.draft()["market_results"][0]
        arms = result["cells"][0]["arms"]
        self.assertEqual(arms["standard"]["primary_cycles"], 2)
        self.assertEqual(arms["assisted"]["primary_cycles"], 1)


class AttributionTest(CurtailmentCase):
    def test_external_stop_differs_from_device_failure_in_counts(self):
        self.setup_trial()
        self.post_notices([notice()])
        self.post_acks([ack("a-one"), ack("a-two"), ack("s-one"), ack("s-two")])
        events = self.green_events()
        # a-one is stopped by the grid before it can report a terminal event.
        events += observations("a-one", "cut", start="2026-10-25T09:00:00+00:00",
                               wh=None, seconds=600)
        events = [item for item in events
                  if not (item["unit_id"] == "a-one" and item["cycle_id"] == "cut"
                          and item["kind"] in {"complete", "abort"})]
        self.deliver("signals", events)
        report = self.call("GET", ROOT + "/curtailment/exposure")
        row = next(item for item in report["units"] if item["unit_id"] == "a-one")
        cycle = next(item for item in row["cycles"] if item["cycle_id"] == "cut")
        self.assertEqual(cycle["state"], "external_stop")
        self.assertEqual(cycle["attribution"], "external_stop")
        safety = self.call("GET", MAIN + "/safety")
        self.assertTrue(all(item["attribution"] != "external_stop"
                            for item in safety["items"]))
        result = self.draft()["market_results"][0]
        self.assertEqual(result["safety"]["totals"]["external_stop_cycles"], 1)
        self.assertEqual(result["safety"]["totals"]["failure_cycles"], 0)

    def test_device_alarm_before_abort_stays_a_failure_under_a_command(self):
        self.setup_trial()
        self.post_notices([notice()])
        self.post_acks([ack("a-one"), ack("a-two"), ack("s-one"), ack("s-two")])
        events = self.green_events()
        events += observations("a-one", "warm", start="2026-10-25T09:10:00+00:00",
                               alarm=True, abort=True, wh=None)
        self.deliver("signals", events)
        report = self.call("GET", ROOT + "/curtailment/exposure")
        row = next(item for item in report["units"] if item["unit_id"] == "a-one")
        cycle = next(item for item in row["cycles"] if item["cycle_id"] == "warm")
        self.assertEqual(cycle["attribution"], "equipment_anomaly_first")
        safety = self.call("GET", MAIN + "/safety")
        kinds = {item["cycle_id"]: item for item in safety["items"]}
        self.assertIn("warm", kinds)
        self.assertEqual(kinds["warm"]["attribution"], "equipment_anomaly_first")
        result = self.draft()["market_results"][0]
        self.assertEqual(result["safety"]["totals"]["anomaly_first_cycles"], 1)
        self.assertEqual(result["safety"]["totals"]["failure_cycles"], 1)

    def test_resolved_emergency_forces_hold_even_with_clean_comparison(self):
        self.setup_trial()
        events = self.green_events()
        events += observations("a-one", "smoke", start="2026-10-25T11:00:00+00:00",
                               alarm=True)
        self.deliver("signals", events)
        result = self.draft()["market_results"][0]
        gate = next(item for item in result["gates"] if item["gate"] == "emergency_events")
        self.assertEqual(gate["state"], "fail")
        self.assertEqual(result["recommendation"], "hold")
        self.assertTrue(result["emergency_blocked"])

    def test_unconfirmed_execution_is_not_counted_as_normal_for_either_arm(self):
        self.setup_trial()
        scoped = notice(scope={"mode": "units", "unit_ids": ["a-one"],
                               "cells": [], "arms": []})
        self.post_notices([scoped])
        self.post_acks([ack("a-one", status="unknown")])
        self.deliver("signals", self.green_events())
        result = self.draft()["market_results"][0]
        assisted = result["cells"][0]["arms"]["assisted"]
        self.assertEqual(assisted["exposure_unknown_cycles"], 1)
        self.assertEqual(assisted["primary_cycles"], 1)
        self.assertEqual(result["participation"]["restricted"][0]["unit_id"], "a-one")


class ComparisonSupportTest(CurtailmentCase):
    def test_primary_comparison_uses_registered_windows_only(self):
        plan = __import__("tests.support", fromlist=["plan"]).plan()
        plan["primary_windows"] = [
            {"label": "opening", "starts_on": "2026-10-25", "ends_before": "2026-10-27"},
        ]
        self.setup_trial(plan)
        events = self.green_events()
        events += observations("a-one", "late", start="2026-10-28T09:00:00+00:00")
        events += observations("a-two", "late", start="2026-10-28T09:00:00+00:00")
        self.deliver("signals", events)
        result = self.draft()["market_results"][0]
        assisted = result["cells"][0]["arms"]["assisted"]
        self.assertEqual(assisted["usable_cycles"], 4)
        self.assertEqual(assisted["primary_cycles"], 2)

    def test_one_sided_curtailment_leaves_the_period_uncontrolled(self):
        self.setup_trial()
        scoped = notice(scope={"mode": "units", "unit_ids": ["a-one"],
                               "cells": [], "arms": []})
        self.post_notices([scoped])
        self.post_acks([ack("a-one")])
        self.deliver("signals", self.green_events())
        result = self.draft()["market_results"][0]
        cells = result["cells"][0]["arms"]
        self.assertEqual(cells["assisted"]["primary_cycles"], 1)
        self.assertEqual(cells["standard"]["primary_cycles"], 2)
        self.assertEqual(result["participation"]["restricted"][0]["cycles"], 1)

    def test_common_coverage_minimum_can_remove_support(self):
        from tests.support import plan
        custom = plan()
        custom["policy"]["minimum_common_coverage_bp"] = 10_000
        self.setup_trial(custom, units=[
            enrollment("s-one", "standard"),
            enrollment("s-two", "standard"),
            enrollment("a-one", "assisted"),
            enrollment("a-two", "assisted"),
            enrollment("a-extra", "assisted"),
        ])
        events = observations("s-one", manual=True) + observations("s-two", manual=True)
        events += observations("a-one", wh=90) + observations("a-two", wh=90)
        self.deliver("signals", events)
        result = self.draft()["market_results"][0]
        self.assertEqual(result["cells"][0]["arms"]["assisted"]["primary_cycles"], 0)
        self.assertEqual(result["recommendation"], "insufficient")


class ExternalStopInfluenceTest(CurtailmentCase):
    def _external_stop(self, unit="a-one", *, start="2026-10-25T09:00:00+00:00"):
        rows = observations(unit, "cut", start=start, wh=None)
        return [item for item in rows if item["kind"] not in {"complete", "abort"}]

    def test_restoring_one_stop_reports_other_arm_opportunity(self):
        self.setup_trial()
        scoped = notice(scope={"mode": "units", "unit_ids": ["a-one"],
                               "cells": [], "arms": []})
        self.post_notices([scoped])
        self.post_acks([ack("a-one")])
        events = [item for item in self.green_events()
                  if not (item["unit_id"] == "a-one" and item["cycle_id"] == "a")]
        events += self._external_stop()
        self.deliver("signals", events)
        readout = self.call("GET", MAIN + "/external-stop-influence")
        self.assertEqual(len(readout["scenarios"]), 1)
        scenario = readout["scenarios"][0]
        self.assertEqual(scenario["unit_id"], "a-one")
        self.assertEqual(scenario["restored_cycles"], ["cut"])
        self.assertEqual(scenario["opportunity"]["before"]["standard"]["usable_cycles"], 2)
        self.assertIn("standard", scenario["opportunity"]["before"])
        self.assertIn("assisted", scenario["opportunity"]["before"])
        self.assertEqual(scenario["opportunity"]["other_arm_changed"], {})

    def test_removal_scenario_does_not_invent_a_terminal_for_the_stopped_run(self):
        self.setup_trial()
        scoped = notice(scope={"mode": "units", "unit_ids": ["a-one"],
                               "cells": [], "arms": []})
        self.post_notices([scoped])
        self.post_acks([ack("a-one")])
        events = [item for item in self.green_events()
                  if not (item["unit_id"] == "a-one" and item["cycle_id"] == "a")]
        events += self._external_stop()
        self.deliver("signals", events)
        readout = self.call("GET", MAIN + "/external-stop-influence")
        scenario = readout["scenarios"][0]
        self.assertEqual(readout["full_recommendation"], "insufficient")
        self.assertEqual(scenario["scenario_recommendation"], "insufficient")
        gates = {item["gate"]: item for item in scenario["gate_changes"]}
        self.assertIn("units_with_unresolved_cycles", gates)
        self.assertEqual(gates["units_with_unresolved_cycles"]["after"], "fail")


class SafetyAndHistoryTest(CurtailmentCase):
    def test_safety_register_reports_all_real_events_including_unresolved(self):
        self.setup_trial()
        partial = observations("a-one", "spark", alarm=True)[1]
        self.deliver("signals", [partial, observations("a-two")[0]])
        register = self.call("GET", MAIN + "/safety")
        self.assertEqual(register["total"], 1)
        item = register["items"][0]
        self.assertFalse(item["resolved"])
        self.assertEqual(item["alarm_reason"], "overheat")

    def test_issued_bulletin_keeps_historical_interpretation_after_later_notice(self):
        from tests.support import observations
        opened = self.setup_trial()
        cut = self.deliver("signals", self.green_events())["current_cut"]
        self.close(opened["version"], cut)
        frozen = self.freeze(cut)
        evidence_id = frozen["evidence_id"]
        self.review(evidence_id, "field", "ada")
        self.review(evidence_id, "methods", "bea")
        bulletin = self.issue(evidence_id)
        self.assertEqual(bulletin["cut"], cut)
        self.assertEqual(self.call("GET", MAIN + f"/bulletins/1"), bulletin)
        stored = self.call("GET", ROOT + f"/evidence/{evidence_id}")
        self.assertEqual(stored["cut"], cut)
        self.assertEqual(stored["market_results"][0]["safety"]["totals"]
                         ["external_stop_cycles"], 0)

    def test_old_cut_remains_reproducible_after_new_acknowledgment(self):
        self.setup_trial()
        first = self.post_notices([notice()])
        cut_one = first["current_cut"]
        self.post_acks([ack("a-one")])
        self.deliver("signals", self.green_events())
        before = self.call("GET", ROOT + "/curtailment/exposure", params={"cut": cut_one})
        row = next(item for item in before["units"] if item["unit_id"] == "a-one")
        self.assertEqual(row["executed_intervals"], [])
        self.assertTrue(row["unknown"])
        current = self.call("GET", ROOT + "/curtailment/exposure")
        row = next(item for item in current["units"] if item["unit_id"] == "a-one")
        self.assertEqual(len(row["executed_intervals"]), 1)
        self.assertEqual(current["cut"], self.call("GET", ROOT)["current_cut"])

    def test_ack_revision_retraction_restores_unknown_without_moving_cut(self):
        self.setup_trial()
        self.post_notices([notice()])
        first = self.post_acks([ack("a-one")])
        cut_one = first["current_cut"]
        retraction = {**ack("a-one", revision=2, void=True)}
        retraction["affected_from"] = None
        retraction["affected_until"] = None
        cut_two = self.post_acks([retraction], delivery="ack-retract")["current_cut"]
        self.assertGreater(cut_two, cut_one)
        report = self.call("GET", ROOT + "/curtailment/exposure", params={"cut": cut_one})
        row = next(item for item in report["units"] if item["unit_id"] == "a-one")
        self.assertEqual(len(row["executed_intervals"]), 1)
        report = self.call("GET", ROOT + "/curtailment/exposure")
        row = next(item for item in report["units"] if item["unit_id"] == "a-one")
        self.assertEqual(row["executed_intervals"], [])

    def test_curtailment_facts_advance_the_cut_and_old_reads_stay_fixed(self):
        opened = self.setup_trial()
        cut = self.deliver("signals", self.green_events())["current_cut"]
        before = self.draft(cut=cut)["market_results"][0]
        self.post_notices([notice()], delivery="late-notice")
        self.post_acks([ack("a-one"), ack("a-two"), ack("s-one"), ack("s-two")],
                       delivery="late-acks")
        current = self.call("GET", ROOT)["current_cut"]
        self.assertGreater(current, cut)
        before_again = self.draft(cut=cut)["market_results"][0]
        self.assertEqual(before, before_again)
        after = self.draft()["market_results"][0]
        self.assertNotEqual(before["cells"], after["cells"])
        self.close(opened["version"], current)
        stale = self.freeze(cut, status=409)
        self.assertEqual(stale["code"], "cut_changed")
        frozen = self.freeze(current)
        self.assertEqual(frozen["cut"], current)
        self.assertEqual(frozen["market_results"][0]["safety"]["totals"]
                         ["external_stop_cycles"], 0)


class NoticeValidationTest(CurtailmentCase):
    def test_scope_requires_units_and_shapes(self):
        self.setup_trial()
        bad = notice(scope={"mode": "units", "unit_ids": [], "cells": [], "arms": []})
        self.call("POST", ROOT + "/curtailment/notices", {
            "delivery_id": "bad", "notices": [bad],
        }, status=422)

    def test_notice_requires_positive_interval(self):
        self.setup_trial()
        bad = notice(starts="2026-10-25T10:00:00+00:00",
                     ends="2026-10-25T09:00:00+00:00")
        self.call("POST", ROOT + "/curtailment/notices", {
            "delivery_id": "bad", "notices": [bad],
        }, status=422)

    def test_repeated_notice_batch_is_idempotent(self):
        self.setup_trial()
        first = self.post_notices([notice()])
        again = self.post_notices([notice()])
        self.assertTrue(again["replayed"])
        self.assertEqual(first["current_cut"], again["current_cut"])

    def test_replayed_batch_still_works_after_close(self):
        opened = self.setup_trial()
        self.post_notices([notice()])
        cut = self.deliver("signals", self.green_events())["current_cut"]
        self.close(opened["version"], cut)
        again = self.post_notices([notice()])
        self.assertTrue(again["replayed"])
        self.assertEqual(again["current_cut"], cut)

    def test_revision_cannot_move_notice_to_another_site(self):
        self.create()
        self.register()
        self.register({"site_id": "south", "market_id": "alpine",
                       "climate": "cold", "timezone": "Europe/Berlin"})
        units = self.two_site_units() + [
            enrollment("s-three", "standard", at="south"),
            enrollment("a-three", "assisted", at="south"),
        ]
        self.open(self.units(units)["version"])
        self.post_notices([notice()])
        moved = notice(revision=2, site_id="south")
        failure = self.call("POST", ROOT + "/curtailment/notices", {
            "delivery_id": "moved", "notices": [moved],
        }, status=409)
        self.assertEqual(failure["code"], "revision_reassigned")