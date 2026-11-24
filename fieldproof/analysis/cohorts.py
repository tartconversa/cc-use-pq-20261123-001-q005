from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from fractions import Fraction
from typing import Iterable

from fieldproof.design.contracts import Enrollment, Policy, enrolled_days
from fieldproof.design.registry import Roster, Trial
from fieldproof.signals.cycles import Cycle

ARMS = ("standard", "assisted")


@dataclass
class UnitSummary:
    unit_id: str
    site_id: str
    market: str
    cell: str
    arm: str
    enrolled_days: int
    cycles: Counter[str] = field(default_factory=Counter)
    energy_issues: Counter[str] = field(default_factory=Counter)
    integrity_issues: Counter[str] = field(default_factory=Counter)
    usable_cycles: int = 0
    device_cycles: int = 0
    comparable_cycles: int = 0
    primary_cycles: int = 0
    restricted_cycles: int = 0
    completed: int = 0
    primary_completed: int = 0
    manual_all: int = 0
    manual: int = 0
    alarms: int = 0
    unsafe_unresolved: int = 0
    failed: int = 0
    external_stops: int = 0
    anomaly_first: int = 0
    exposure_unknown: int = 0
    energy_cycles: int = 0
    total_wh: int = 0
    elapsed_seconds: int = 0
    def record(self, cycle: Cycle):
        self.cycles[cycle.state] += 1
        cause = cycle.attribution
        if cause == "external_stop" and cycle.state != "aborted":
            # The grid stopped the run before the device produced a terminal
            # event. It is neither a device failure nor an ordinary chance.
            self.external_stops += 1
            self.restricted_cycles += 1
            return
        if not cycle.usable:
            self.integrity_issues.update(cycle.issues)
            self.unsafe_unresolved += int(cycle.alarm)
            if cycle.exposure == "unknown":
                self.exposure_unknown += 1
            return
        self.usable_cycles += 1
        self.device_cycles += 1
        self.completed += int(cycle.state == "complete")
        # Safety accounting always sees every run, curated or not.
        self.alarms += int(cycle.alarm)
        # A grid command that stops a run is not a device failure, but a device
        # that failed first still counts as one. Exposure we could not confirm
        # is held apart instead of being booked as normal operation.
        if cause in {"equipment_failure", "equipment_anomaly_first"}:
            self.failed += 1
        if cause == "equipment_anomaly_first":
            self.anomaly_first += 1
        if cause == "external_stop":
            self.external_stops += 1
        if cycle.exposure == "unknown":
            self.exposure_unknown += 1
        self.manual_all += int(cycle.manual)
        if cycle.comparable:
            self.comparable_cycles += 1
        if not cycle.primary:
            if cycle.restricted or cycle.comparable:
                self.restricted_cycles += 1
            return
        self.primary_cycles += 1
        self.primary_completed += int(cycle.state == "complete")
        self.manual += int(cycle.manual)
        self.elapsed_seconds += cycle.elapsed_seconds or 0
        if cycle.energy.wh is not None:
            self.energy_cycles += 1
            self.total_wh += cycle.energy.wh
        elif cycle.state == "complete":
            self.energy_issues[cycle.energy.problem] += 1

    def eligible(self, policy: Policy) -> bool:
        return self.primary_cycles >= policy.required_cycles_per_unit

    def rate(self, numerator: int, denominator: int) -> Fraction | None:
        return Fraction(numerator, denominator) if denominator else None

    def metric(self, name: str) -> Fraction | None:
        if name == "energy_wh":
            return self.rate(self.total_wh, self.energy_cycles)
        if name == "duration_seconds":
            return self.rate(self.elapsed_seconds, self.primary_cycles)
        if name == "manual":
            return self.rate(self.manual, self.primary_cycles)
        if name == "failure":
            # Failures are measured over device-resolved runs so an external
            # stop cannot inflate the denominator or hide behind the numerator.
            return self.rate(self.failed, self.device_cycles)
        return self.rate(self.alarms, self.usable_cycles)

    def payload(self, policy: Policy) -> dict:
        return {
            "unit_id": self.unit_id,
            "site_id": self.site_id,
            "market": self.market,
            "cell": self.cell,
            "arm": self.arm,
            "enrolled_days": self.enrolled_days,
            "cycle_states": dict(sorted(self.cycles.items())),
            "integrity_issues": dict(sorted(self.integrity_issues.items())),
            "energy_issues": dict(sorted(self.energy_issues.items())),
            "eligible": self.eligible(policy),
            "usable_cycles": self.usable_cycles,
            "device_resolved_cycles": self.device_cycles,
            "comparable_cycles": self.comparable_cycles,
            "primary_cycles": self.primary_cycles,
            "restricted_cycles": self.restricted_cycles,
            "completed": self.completed,
            "primary_completed_cycles": self.primary_completed,
            "manual_cycles": self.manual,
            "manual_cycles_all": self.manual_all,
            "alarm_cycles": self.alarms,
            "unresolved_with_alarm": self.unsafe_unresolved,
            "failed_cycles": self.failed,
            "external_stop_cycles": self.external_stops,
            "anomaly_first_cycles": self.anomaly_first,
            "exposure_unknown_cycles": self.exposure_unknown,
            "energy_cycles": self.energy_cycles,
            "total_wh": self.total_wh,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class AnalysisWindow:
    start: date
    end: date
    segments: tuple[tuple[date, date], ...] | None = None

    def periods(self) -> tuple[tuple[date, date], ...]:
        return self.segments or ((self.start, self.end),)

    def contains(self, day: date) -> bool:
        """The observed window. Safety accounting must see every day here."""
        return self.start <= day < self.end

    def in_primary_period(self, day: date) -> bool:
        """Whether the day belongs to a pre-registered comparison period."""
        return any(start <= day < stop for start, stop in self.periods())

    def enrolled(self, unit: Enrollment) -> int:
        return enrolled_days(unit, self.start, self.end)

    def payload(self) -> dict:
        if self.segments is not None:
            return {
                "starts_on": self.start.isoformat(),
                "ends_before": self.end.isoformat(),
                "primary_segments": [
                    {"starts_on": start.isoformat(), "ends_before": stop.isoformat()}
                    for start, stop in self.segments
                ],
            }
        return {"starts_on": self.start.isoformat(), "ends_before": self.end.isoformat()}


def apply_common_support(
    trial: Trial,
    roster: Roster,
    cycles: Iterable[Cycle],
    window: AnalysisWindow,
) -> None:
    """Require both arms to be exposed together in the same period and place.

    A run only enters the primary comparison when its own exposure is normal and
    its pre-registered period, site and cell have comparable runs in *both*
    arms. One-sided curtailment, unconfirmed execution or an unopposed arm
    leaves the run listed as restricted or uncontrolled instead of borrowing
    support from the other arm.
    """
    policy = trial.plan.policy
    minimum = Fraction(policy.minimum_common_coverage_bp, 10_000)
    grouped: dict[tuple, dict[str, list[Cycle]]] = defaultdict(
        lambda: {"standard": [], "assisted": []}
    )
    populated: dict[tuple, dict[str, set[str]]] = defaultdict(
        lambda: {"standard": set(), "assisted": set()}
    )
    sites: set[tuple[str, tuple[date, date], str, str]] = set()
    for cycle in cycles:
        cycle.common_support = False
        if cycle.day is None or not window.in_primary_period(cycle.day):
            continue
        unit = roster.units.get(cycle.unit_id)
        if unit is None:
            continue
        market, cell = roster.placement(unit)
        compound = (market, _period_key(window, cycle.day), unit.site_id, cell)
        sites.add(compound)
        if cycle.comparable:
            grouped[compound][unit.arm].append(cycle)
            populated[compound][unit.arm].add(unit.unit_id)

    supported = set()
    for compound in sites:
        market, period, site, cell = compound
        ok = True
        for arm in ARMS:
            if not grouped[compound][arm]:
                ok = False
                break
            active = _active_units(trial, roster, market, site, cell, period, arm)
            covered = len(populated[compound][arm])
            if active == 0 or Fraction(covered, active) < minimum:
                ok = False
                break
        if ok:
            supported.add(compound)
    for compound, arms in grouped.items():
        flag = compound in supported
        for arm in ARMS:
            for cycle in arms[arm]:
                cycle.common_support = flag


def _period_key(window: AnalysisWindow, day: date) -> tuple[date, date]:
    for start, stop in window.periods():
        if start <= day < stop:
            return start, stop
    return window.start, window.end


def _active_units(
    trial: Trial, roster: Roster, market: str, site: str, cell: str,
    period: tuple[date, date], arm: str,
) -> int:
    start, stop = period
    total = 0
    for unit in roster.units.values():
        if unit.site_id != site or unit.arm != arm:
            continue
        placed_market, placed_cell = roster.placement(unit)
        if placed_market != market or placed_cell != cell:
            continue
        if enrolled_days(unit, start, stop):
            total += 1
    return total


def summarize(
    trial: Trial, roster: Roster, cycles: Iterable[Cycle], window: AnalysisWindow
) -> dict[str, UnitSummary]:
    summaries = {}
    for unit in roster.units.values():
        days = window.enrolled(unit)
        if not days:
            continue
        market, cell = roster.placement(unit)
        summaries[unit.unit_id] = UnitSummary(
            unit.unit_id, unit.site_id, market, cell, unit.arm, days
        )
    for cycle in cycles:
        row = summaries.get(cycle.unit_id)
        if row is not None and (cycle.day is None or window.contains(cycle.day)):
            row.record(cycle)
    return summaries


@dataclass
class ArmSummary:
    recruited: list[UnitSummary]
    eligible: list[UnitSummary]

    @classmethod
    def assess(cls, units: list[UnitSummary], policy: Policy):
        return cls(units, [unit for unit in units if unit.eligible(policy)])

    @property
    def enrollment_coverage(self) -> Fraction:
        return Fraction(len(self.eligible), len(self.recruited)) if self.recruited else Fraction(0)

    @property
    def energy_coverage(self) -> Fraction | None:
        complete = sum(unit.primary_completed for unit in self.eligible)
        measured = sum(unit.energy_cycles for unit in self.eligible)
        return Fraction(measured, complete) if complete else None

    @property
    def measured_units(self) -> int:
        return sum(unit.energy_cycles > 0 for unit in self.eligible)

    def mean(self, name: str) -> Fraction | None:
        values = [value for unit in self.eligible if (value := unit.metric(name)) is not None]
        return sum(values, Fraction(0)) / len(values) if values else None

    def safety(self) -> dict:
        """Emergency counts over every recruited device, curated or not.

        These figures deliberately ignore primary-comparison eligibility: a
        genuine alarm or failure must stay visible to the on-call desks even
        when its run cannot support a market comparison.
        """
        usable = sum(unit.usable_cycles for unit in self.recruited)
        device = sum(unit.device_cycles for unit in self.recruited)
        alarms = sum(unit.alarms for unit in self.recruited)
        unresolved = sum(unit.unsafe_unresolved for unit in self.recruited)
        failures = sum(unit.failed for unit in self.recruited)
        return {
            "usable_cycles": usable,
            "device_resolved_cycles": device,
            "alarm_cycles": alarms,
            "unresolved_alarm_cycles": unresolved,
            "failure_cycles": failures,
            "anomaly_first_cycles": sum(unit.anomaly_first for unit in self.recruited),
            "external_stop_cycles": sum(unit.external_stops for unit in self.recruited),
            "exposure_unknown_cycles": sum(
                unit.exposure_unknown for unit in self.recruited
            ),
        }

    def payload(self) -> dict:
        return {
            "recruited_units": len(self.recruited),
            "eligible_units": len(self.eligible),
            "energy_units": self.measured_units,
            "usable_cycles": sum(unit.usable_cycles for unit in self.recruited),
            "comparable_cycles": sum(unit.comparable_cycles for unit in self.recruited),
            "primary_cycles": sum(unit.primary_cycles for unit in self.recruited),
            "eligible_completed_cycles": sum(unit.primary_completed for unit in self.eligible),
            "eligible_measured_cycles": sum(unit.energy_cycles for unit in self.eligible),
            "eligible_observed_cycles": sum(unit.usable_cycles for unit in self.eligible),
            "eligible_primary_cycles": sum(unit.primary_cycles for unit in self.eligible),
            "eligible_comparable_cycles": sum(
                unit.comparable_cycles for unit in self.eligible
            ),
            "restricted_cycles": sum(unit.restricted_cycles for unit in self.recruited),
            "external_stop_cycles": sum(unit.external_stops for unit in self.recruited),
            "anomaly_first_cycles": sum(unit.anomaly_first for unit in self.recruited),
            "exposure_unknown_cycles": sum(unit.exposure_unknown for unit in self.recruited),
            "unresolved_units": sum(bool(unit.integrity_issues)
                                     for unit in self.recruited),
            "unresolved_alarm_cycles": sum(unit.unsafe_unresolved for unit in self.recruited),
            "alarm_cycles_all": sum(unit.alarms for unit in self.recruited),
            "failure_cycles_all": sum(unit.failed for unit in self.recruited),
        }


def by_placement(units: Iterable[UnitSummary]) -> dict[tuple[str, str, str], list[UnitSummary]]:
    cells = defaultdict(list)
    for unit in units:
        cells[unit.market, unit.cell, unit.arm].append(unit)
    return cells


def opportunity_snapshot(units: Iterable[UnitSummary], policy: Policy) -> dict:
    """Availability and coverage that could move when a period is removed.

    The snapshot deliberately separates counts from eligibility: removing an
    external stop can change how many runs the *other* arm contributes to the
    same period and cell, and therefore whether its own coverage still holds.
    """
    summaries = list(units)
    by_cell = by_placement(summaries)
    cells = {}
    for (market, cell, arm), rows in sorted(by_cell.items()):
        eligible = [row for row in rows if row.eligible(policy)]
        coverage = (
            (Fraction(len(eligible), len(rows)) * 10_000) if rows else None
        )
        cells.setdefault(cell, {})[arm] = {
            "market_id": market,
            "recruited_units": len(rows),
            "eligible_units": len(eligible),
            "primary_cycles": sum(row.primary_cycles for row in rows),
            "usable_cycles": sum(row.usable_cycles for row in rows),
            "unit_coverage_bp": None if coverage is None else float(coverage),
        }
    return {"cells": cells}
