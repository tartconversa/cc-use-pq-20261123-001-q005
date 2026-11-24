from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from fieldproof.common import utc_text
from fieldproof.design.contracts import Policy, local_day
from fieldproof.design.registry import Roster
from fieldproof.signals.contracts import Event
from fieldproof.signals.ledger import cycle_inputs


@dataclass(frozen=True)
class Energy:
    wh: int | None
    problem: str | None = None
    meter_id: str | None = None
    event_ids: tuple[str, ...] = ()


@dataclass
class Cycle:
    unit_id: str
    cycle_id: str
    state: str
    issues: list[str]
    day: date | None = None
    start: Event | None = None
    close: Event | None = None
    manual: bool = False
    alarm: bool = False
    elapsed_seconds: int | None = None
    energy: Energy = field(default_factory=lambda: Energy(None, "cycle_unresolved"))
    sources: dict[str, int] = field(default_factory=dict)
    alarm_events: list[Event] = field(default_factory=list)
    manual_at: datetime | None = None
    alarm_at: datetime | None = None
    stop_at: datetime | None = None
    stop_reason: str | None = None
    exposure: str = "normal"
    exposure_reasons: list[str] = field(default_factory=list)
    notice_ids: list[str] = field(default_factory=list)
    attribution: str | None = None
    during_curtailment: bool = False
    common_support: bool = True
    executed_from: datetime | None = None
    executed_until: datetime | None = None

    @property
    def usable(self) -> bool:
        """Runs the device itself resolved, either normally or by aborting.

        An externally stopped run is tracked separately so it cannot be counted
        as a device failure or as a normal opportunity.
        """
        return self.state in {"complete", "aborted"}

    @property
    def device_resolved(self) -> bool:
        return self.usable

    @property
    def failed(self) -> bool:
        return self.state == "aborted"

    @property
    def restricted(self) -> bool:
        """Runs touched by a grid command or by unconfirmed execution."""
        return (
            self.state == "external_stop"
            or self.exposure in {"curtailed", "command_unexecuted", "unknown"}
        )

    @property
    def comparable(self) -> bool:
        """Whether this cycle can enter the pre-registered primary comparison.

        Periods with confirmed curtailment, conflicting coverage, an unconfirmed
        source, or an execution we could not verify are excluded rather than
        silently treated as ordinary operation for both arms.
        """
        return self.usable and self.exposure == "normal"

    @property
    def primary(self) -> bool:
        """Comparable and backed by common exposure in both arms."""
        return self.comparable and self.common_support

    def payload(self) -> dict:
        return {
            "unit_id": self.unit_id,
            "cycle_id": self.cycle_id,
            "state": self.state,
            "issues": self.issues,
            "local_date": self.day.isoformat() if self.day else None,
            "started_at": utc_text(self.start.occurred_at) if self.start else None,
            "closed_at": utc_text(self.close.occurred_at) if self.close else None,
            "stop_reason": self.stop_reason,
            "manual": self.manual,
            "manual_at": utc_text(self.manual_at) if self.manual_at else None,
            "alarm": self.alarm,
            "alarm_at": utc_text(self.alarm_at) if self.alarm_at else None,
            "alarm_reason": self.alarm_events[0].reason if self.alarm_events else None,
            "alarm_events": [
                {
                    "event_id": item.event_id,
                    "occurred_at": utc_text(item.occurred_at),
                    "reason": item.reason,
                    "revision": item.revision,
                }
                for item in self.alarm_events
            ],
            "elapsed_seconds": self.elapsed_seconds,
            "energy_wh": self.energy.wh,
            "energy_issue": self.energy.problem,
            "meter_id": self.energy.meter_id,
            "meter_events": list(self.energy.event_ids),
            "exposure": self.exposure,
            "exposure_reasons": self.exposure_reasons,
            "notice_ids": self.notice_ids,
            "attribution": self.attribution,
            "during_curtailment": self.during_curtailment,
            "common_support": self.common_support,
            "comparable": self.comparable,
            "primary": self.primary,
            "source_revisions": self.sources,
        }


def energy_used(meters: list[Event], start: datetime, close: datetime, cap: int) -> Energy:
    by_time: dict[datetime, list[Event]] = defaultdict(list)
    for meter in meters:
        if start <= meter.occurred_at <= close:
            by_time[meter.occurred_at].append(meter)
    if start not in by_time or close not in by_time:
        return Energy(None, "missing_endpoint")
    ordered = []
    for moment in sorted(by_time):
        candidates = by_time[moment]
        signatures = {(item.meter_id, item.meter_wh) for item in candidates}
        if len(signatures) != 1:
            return Energy(None, "conflicting_readings")
        ordered.append(min(candidates, key=lambda item: item.event_id))
    meters_used = {item.meter_id for item in ordered}
    if len(meters_used) != 1:
        return Energy(None, "meter_changed")
    readings = [item.meter_wh for item in ordered]
    if any(after < before for before, after in zip(readings, readings[1:])):
        return Energy(None, "counter_decreased")
    energy = readings[-1] - readings[0]
    if energy > cap:
        return Energy(None, "implausible_consumption")
    return Energy(energy, meter_id=ordered[0].meter_id,
                  event_ids=tuple(item.event_id for item in ordered))


def resolve(unit_id: str, cycle_id: str, events: list[Event], roster: Roster, policy: Policy) -> Cycle:
    groups = defaultdict(list)
    for event in events:
        groups[event.kind].append(event)
    starts = groups["start"]
    ends = groups["complete"] + groups["abort"]
    alarms = sorted(groups["alarm"], key=lambda event: (event.occurred_at, event.event_id))
    cycle = Cycle(
        unit_id,
        cycle_id,
        "invalid",
        [],
        alarm=bool(alarms),
        alarm_events=alarms,
        alarm_at=alarms[0].occurred_at if alarms else None,
        sources={event.event_id: event.revision for event in events},
    )
    if len(starts) != 1:
        if not starts:
            cycle.state = "orphan"
            cycle.issues.append("missing_start")
        else:
            cycle.issues.append("duplicate_start")
        return cycle
    cycle.start = start = starts[0]
    unit = roster.units[unit_id]
    cycle.day = local_day(start.occurred_at, roster.sites[unit.site_id])
    if not ends:
        cycle.state = "pending"
        cycle.issues.append("missing_terminal")
        return cycle
    if len(ends) != 1:
        cycle.issues.append("multiple_terminals")
        return cycle
    cycle.close = close = ends[0]
    cycle.stop_reason = close.reason
    cycle.stop_at = close.occurred_at
    elapsed = close.occurred_at - start.occurred_at
    if elapsed <= timedelta() or elapsed > timedelta(seconds=policy.max_cycle_seconds):
        cycle.issues.append("invalid_duration")
        return cycle
    actions = groups["manual"] + groups["alarm"]
    if any(not start.occurred_at <= event.occurred_at <= close.occurred_at for event in actions):
        cycle.issues.append("action_outside_cycle")
        return cycle
    cycle.elapsed_seconds = int(elapsed.total_seconds())
    cycle.manual = bool(groups["manual"])
    if cycle.manual:
        cycle.manual_at = min(
            (event.occurred_at for event in groups["manual"]), default=None
        )
    cycle.state = "aborted" if close.kind == "abort" else "complete"
    if cycle.state == "aborted":
        cycle.energy = Energy(None, "cycle_aborted")
    else:
        cycle.energy = energy_used(groups["meter"], start.occurred_at, close.occurred_at,
                                   policy.max_cycle_wh)
    return cycle


def resolve_cycles(events: Iterable[Event], roster: Roster, policy: Policy) -> list[Cycle]:
    cycles = [
        resolve(unit, cycle, group, roster, policy)
        for (unit, cycle), group in cycle_inputs(events)
    ]
    by_unit: dict[str, list[Cycle]] = defaultdict(list)
    for cycle in cycles:
        if cycle.device_resolved:
            by_unit[cycle.unit_id].append(cycle)
        # Later stages overlay curtailment even on cycles that are already
        # unusable, because the inspection queue has to explain why the cycle
        # was truncated before the deterministic analysis rules fired.
    for observed in by_unit.values():
        ordered = sorted(observed, key=lambda cycle: (cycle.start.occurred_at, cycle.cycle_id))
        if not ordered:
            continue
        last_ending = ordered[0]
        for candidate in ordered[1:]:
            if candidate.start.occurred_at < last_ending.close.occurred_at:
                for item in (last_ending, candidate):
                    if item.state != "invalid":
                        item.state = "invalid"
                        item.issues.append("overlapping_cycle")
                        item.energy = Energy(None, "cycle_unresolved")
            if candidate.close.occurred_at > last_ending.close.occurred_at:
                last_ending = candidate
    return cycles
