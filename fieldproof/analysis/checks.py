from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from sqlite3 import Connection

from fieldproof.analysis.cohorts import ARMS
from fieldproof.analysis.evidence import Material
from fieldproof.common import missing
from fieldproof.design.registry import Trial, roster
from fieldproof.signals.cycles import Cycle

PRIORITIES = {
    "unresolved_alarm": 0,
    "integrity": 1,
    "pending": 2,
    "energy": 3,
}


def queue_entry(cycle: Cycle, site: str, cell: str, arm: str) -> dict | None:
    if not cycle.usable:
        if cycle.alarm:
            category = "unresolved_alarm"
        else:
            category = "pending" if cycle.state == "pending" else "integrity"
        issues = cycle.issues
    elif not cycle.failed and cycle.energy.problem:
        category = "energy"
        issues = [cycle.energy.problem]
    else:
        return None
    return {
        "category": category,
        "unit_id": cycle.unit_id,
        "cycle_id": cycle.cycle_id,
        "local_date": cycle.day.isoformat() if cycle.day is not None else None,
        "site": site,
        "arm": arm,
        "cell": cell,
        "issues": issues,
        "source_revisions": cycle.sources,
    }


def inspection_queue(
    trial: Trial,
    material: Material,
    market: str,
    site: str | None,
    offset: int,
    limit: int,
) -> dict:
    if trial.plan.market(market) is None:
        raise missing("market", market)
    units = {
        unit_id: item
        for unit_id, item in material.units.items()
        if item.market == market and (site is None or item.site_id == site)
    }
    entries = []
    for cycle in material.cycles:
        item = units.get(cycle.unit_id)
        if item is None or (cycle.day is not None and not material.window.contains(cycle.day)):
            continue
        entry = queue_entry(cycle, item.site_id, item.cell, item.arm)
        if entry is not None:
            entries.append(entry)
    entries.sort(key=lambda item: (
        PRIORITIES[item["category"]], item["local_date"] or "", item["unit_id"], item["cycle_id"]
    ))
    totals = Counter(item["category"] for item in entries)
    return {
        "market_id": market,
        "cut": material.cut,
        "window": material.window.payload(),
        "total": len(entries),
        "counts": {category: totals[category] for category in PRIORITIES},
        "items": entries[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(entries) else None,
    }


@dataclass
class CoverageProgress:
    population: int
    minimum: int
    required_cycles: int
    minimum_coverage_bp: int
    counts: Counter[str] = field(default_factory=Counter)
    eligible: int = 0
    total_cycles: int = 0
    energy_cycles: int = 0

    def tally(self, cycle: Cycle):
        self.counts[cycle.unit_id] += 1
        if self.counts[cycle.unit_id] == self.required_cycles:
            self.eligible += 1
        self.total_cycles += 1
        self.energy_cycles += int(cycle.energy.wh is not None)

    def status(self) -> dict:
        missing_units = max(0, self.minimum - self.eligible)
        target = (self.minimum_coverage_bp * self.population + 9_999) // 10_000
        return {
            "enrolled_for_window": self.population,
            "qualified_to_date": self.eligible,
            "short_of_minimum": missing_units,
            "short_of_coverage_target": max(0, target - self.eligible),
            "observed_cycles_to_date": self.total_cycles,
            "measured_cycles_to_date": self.energy_cycles,
        }


def local_dates(start: date, stop: date):
    for offset in range((stop - start).days):
        yield start + timedelta(days=offset)


def safety_register(material: Material, market: str, offset: int, limit: int) -> dict:
    """Every real alarm and device failure, independent of comparability.

    Reviewers asked for a query that never hides behind primary-comparison
    eligibility. The register therefore walks all observed cycles in the market
    window and reports alarms, device-first anomalies and failures, even when
    the cycle also sits under a curtailed period.
    """
    units = {
        unit_id: item for unit_id, item in material.units.items() if item.market == market
    }
    events = []
    for cycle in material.cycles:
        item = units.get(cycle.unit_id)
        if item is None or (cycle.day is not None and not material.window.contains(cycle.day)):
            continue
        if not (cycle.alarm or cycle.attribution in {
            "equipment_failure", "equipment_anomaly_first"
        }):
            continue
        events.append({
            "unit_id": cycle.unit_id,
            "cycle_id": cycle.cycle_id,
            "site": item.site_id,
            "cell": item.cell,
            "arm": item.arm,
            "local_date": cycle.day.isoformat() if cycle.day else None,
            "state": cycle.state,
            "attribution": cycle.attribution,
            "exposure": cycle.exposure,
            "alarm": cycle.alarm,
            "alarm_at": cycle.alarm_at.isoformat() if cycle.alarm_at else None,
            "alarm_reason": cycle.alarm_events[0].reason if cycle.alarm_events else None,
            "alarm_events": [
                {
                    "event_id": event.event_id,
                    "occurred_at": event.occurred_at.isoformat(),
                    "reason": event.reason,
                    "revision": event.revision,
                }
                for event in cycle.alarm_events
            ],
            "comparable": cycle.comparable,
            "primary": cycle.primary,
            "resolved": cycle.usable,
            "source_revisions": cycle.sources,
        })
    events.sort(key=lambda entry: (entry["local_date"] or "", entry["unit_id"],
                                   entry["cycle_id"]))
    priorities = {"equipment_anomaly_first": 0, "equipment_failure": 1}
    events.sort(key=lambda entry: (
        priorities.get(entry["attribution"], 2),
        entry["local_date"] or "", entry["unit_id"], entry["cycle_id"],
    ))
    return {
        "market_id": market,
        "cut": material.cut,
        "window": material.window.payload(),
        "total": len(events),
        "items": events[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(events) else None,
    }


def readiness_calendar(db: Connection, trial: Trial, material: Material, market_id: str) -> dict:
    market = trial.plan.market(market_id)
    if market is None:
        raise missing("market", market_id)
    assigned = roster(db, trial.trial_id)
    units = {item.unit_id: item for item in material.units.values() if item.market == market_id}
    populations = Counter((item.cell, item.arm) for item in units.values())
    policy = trial.plan.policy
    progress = {
        (cell.key, arm): CoverageProgress(
            populations[cell.key, arm], policy.required_units_per_arm,
            policy.required_cycles_per_unit, policy.minimum_unit_coverage_bp,
        )
        for cell in market.cells for arm in ARMS
    }
    cycles_by_date: dict[date, list[Cycle]] = defaultdict(list)
    for cycle in material.cycles:
        # The calendar tracks progress toward the primary comparison, so it
        # counts the same runs the comparison would accept, not every usable
        # run including curtailed or unconfirmed exposure.
        if cycle.unit_id in units and cycle.primary and material.window.contains(cycle.day):
            cycles_by_date[cycle.day].append(cycle)
    enrollment_edges = defaultdict(Counter)
    for unit_id, item in units.items():
        unit = assigned.units[unit_id]
        key = item.cell, item.arm
        enrollment_edges[max(unit.active_from, material.window.start)][key] += 1
        enrollment_edges[min(unit.active_until, material.window.end)][key] -= 1
    active = Counter()
    days = []
    for day in local_dates(material.window.start, material.window.end):
        active.update(enrollment_edges[day])
        for cycle in cycles_by_date[day]:
            unit = units[cycle.unit_id]
            progress[unit.cell, unit.arm].tally(cycle)
        days.append({
            "local_date": day.isoformat(),
            "cells": [
                {
                    "cell": cell.key,
                    "arms": {
                        arm: {
                            "active_on_date": active[cell.key, arm],
                            **progress[cell.key, arm].status(),
                        }
                        for arm in ARMS
                    },
                }
                for cell in market.cells
            ],
        })
    return {"market_id": market_id, "cut": material.cut, "window": material.window.payload(), "days": days}
