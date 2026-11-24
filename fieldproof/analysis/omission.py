from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from fieldproof.analysis.cohorts import (
    ARMS,
    UnitSummary,
    by_placement,
    opportunity_snapshot,
    summarize,
)
from fieldproof.analysis.contrast import METRICS, MarketEstimate, assess_cells, display
from fieldproof.analysis.evidence import Material
from fieldproof.common import missing
from fieldproof.release.gates import assess

RATE_SCALE = 10_000


@dataclass(frozen=True)
class Removal:
    unit_id: str
    site_id: str
    cell: str
    arm: str
    cycle_ids: tuple[str, ...]


def removals(material: Material, market: str) -> list[Removal]:
    """Group each externally stopped unit's runs into a single scenario.

    Removing one stop must be evaluated without silently renormalising the
    other arm: the scenario re-runs the whole market assessment with those runs
    restored to unattributed operation, so any change to the other arm's
    eligibility or coverage shows up next to it.
    """
    grouped: dict[str, list] = {}
    for cycle in material.cycles:
        item = material.units.get(cycle.unit_id)
        if item is None or item.market != market:
            continue
        if cycle.attribution == "external_stop":
            grouped.setdefault(cycle.unit_id, []).append(cycle)
    result = []
    for unit_id, cycles in sorted(grouped.items()):
        item = material.units[unit_id]
        result.append(Removal(
            unit_id=unit_id,
            site_id=item.site_id,
            cell=item.cell,
            arm=item.arm,
            cycle_ids=tuple(sorted(cycle.cycle_id for cycle in cycles)),
        ))
    return result


def _rebuild(material: Material, trial, restored: set[tuple[str, str]]) -> dict[str, UnitSummary]:
    """Re-summarise with chosen external stops no longer attributed to the grid.

    Restored runs keep their actual device facts. A run without a terminal
    event stays pending, because "what if the grid had not commanded a stop"
    must not invent a completion the device never reported. The scenario shows
    whether the *other* arm's opportunity and coverage move, not a fabricated
    counterfactual measurement.
    """
    from fieldproof.signals.curtailment import attribution

    cycles = deepcopy(material.cycles)
    for cycle in cycles:
        if (cycle.unit_id, cycle.cycle_id) not in restored:
            continue
        if cycle.state != "external_stop":
            continue
        cycle.state = "pending"
        if "missing_terminal" not in cycle.issues:
            cycle.issues.append("missing_terminal")
        cycle.exposure = "normal"
        cycle.exposure_reasons = []
        cycle.notice_ids = []
        cycle.during_curtailment = False
        cycle.attribution = attribution(cycle)
    return summarize(trial, material.roster, cycles, material.window)


def describe(full: MarketEstimate, other: MarketEstimate, metric: str) -> dict:
    baseline = full.delta(metric)
    changed = other.delta(metric)
    scale = RATE_SCALE if metric in {"manual", "alarm", "failure"} else 1
    return {
        "full_effect": display(baseline, scale),
        "scenario_effect": display(changed, scale),
        "shift": display(changed - baseline, scale)
        if baseline is not None and changed is not None else None,
        "unit": {"energy_wh": "Wh", "duration_seconds": "seconds"}.get(
            metric, "basis_points"
        ),
    }


def compare_gates(full: dict, other: dict) -> list[dict]:
    before = {item["gate"]: item["state"] for item in full["gates"]}
    after = {item["gate"]: item["state"] for item in other["gates"]}
    return [
        {"gate": key, "before": before.get(key), "after": after.get(key)}
        for key in sorted(before.keys() | after.keys())
        if before.get(key) != after.get(key)
    ]


def _cell_rows(snapshot: dict, removal: Removal) -> dict:
    return {
        arm: snapshot["cells"].get(removal.cell, {}).get(arm, {}) for arm in ARMS
    }

def external_stop_readout(trial, material: Material, market_id: str) -> dict:
    market = trial.plan.market(market_id)
    if market is None:
        raise missing("market", market_id)
    policy = trial.plan.policy
    full_units = [item for item in material.units.values() if item.market == market_id]
    full = MarketEstimate.build(market, assess_cells(market, by_placement(full_units), policy))
    full_gates = assess(full, policy)
    baseline = opportunity_snapshot(material.units.values(), policy)
    scenarios = []
    for removal in removals(material, market_id):
        units = _rebuild(
            material, trial,
            {(removal.unit_id, cycle_id) for cycle_id in removal.cycle_ids},
        )
        scoped = [item for item in units.values() if item.market == market_id]
        estimate = MarketEstimate.build(
            market, assess_cells(market, by_placement(scoped), policy)
        )
        gates = assess(estimate, policy)
        after = opportunity_snapshot(units.values(), policy)
        scenarios.append({
            "unit_id": removal.unit_id,
            "site_id": removal.site_id,
            "cell": removal.cell,
            "arm": removal.arm,
            "restored_cycles": list(removal.cycle_ids),
            "scenario_recommendation": gates["recommendation"],
            "metrics": {name: describe(full, estimate, name) for name in METRICS},
            "opportunity": {
                "before": _cell_rows(baseline, removal),
                "after": _cell_rows(after, removal),
                "other_arm_changed": _changed(
                    _cell_rows(baseline, removal), _cell_rows(after, removal),
                    removal.arm,
                ),
            },
            "gate_changes": compare_gates(full_gates, gates),
        })
    return {
        "market_id": market_id,
        "cut": material.cut,
        "window": material.window.payload(),
        "full_recommendation": full_gates["recommendation"],
        "scenarios": scenarios,
    }


def _changed(before: dict, after: dict, excluded_arm: str) -> dict:
    """Rows in the untouched arm whose availability or coverage moved."""
    changes = {}
    for arm in ARMS:
        if arm == excluded_arm:
            continue
        if before.get(arm) != after.get(arm):
            changes[arm] = {"before": before.get(arm), "after": after.get(arm)}
    return changes
