from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from fieldproof.analysis.contrast import MarketEstimate, display
from fieldproof.design.contracts import Policy


@dataclass(frozen=True)
class Gate:
    name: str
    state: str
    observed: str | None = None
    boundary: int | None = None
    direction: str | None = None

    def payload(self) -> dict:
        return {
            "gate": self.name,
            "state": self.state,
            "observed": self.observed,
            "boundary": self.boundary,
            "direction": self.direction,
        }


def threshold(name: str, observed: Fraction | None, boundary: int, direction: str) -> Gate:
    if observed is None:
        return Gate(name, "unavailable", boundary=boundary, direction=direction)
    passed = observed <= boundary if direction == "at_most" else observed >= boundary
    return Gate(name, "pass" if passed else "fail", display(observed), boundary, direction)


def in_basis_points(number: Fraction | None) -> Fraction | None:
    return number * 10_000 if number is not None else None


def energy_gate(estimate: MarketEstimate, policy: Policy) -> Gate:
    standard = estimate.standard["energy_wh"]
    assisted = estimate.assisted["energy_wh"]
    allowed = policy.max_energy_increase_bp
    if standard is None or assisted is None:
        return Gate("energy_change_bp", "unavailable", boundary=allowed, direction="at_most")
    if standard == 0:
        return Gate(
            "zero_baseline_energy",
            "pass" if assisted == 0 else "fail",
            display(assisted),
            0,
            "at_most",
        )
    return threshold(
        "energy_change_bp", (assisted - standard) / standard * 10_000, allowed, "at_most"
    )


def emergency_gate(estimate: MarketEstimate) -> Gate:
    """A resolved emergency event blocks expansion regardless of the sample.

    A resolved alarm is evidence that the assisted program exposed a real
    safety event, so it forces a hold even when the comparison looks clean.
    An alarm on a run that could not be resolved is reported as unavailable:
    the market cannot advance on incomplete safety evidence, but the system
    does not pretend it has confirmed a failure it cannot yet analyse. Both
    remain visible in the inspection queue and the safety counts.
    """
    safety = estimate.safety()["totals"]
    if not safety:
        return Gate("emergency_events", "unavailable")
    confirmed = safety.get("alarm_cycles", 0)
    unresolved = safety.get("unresolved_alarm_cycles", 0)
    if confirmed:
        return Gate("emergency_events", "fail", str(confirmed), 0, "at_most")
    if unresolved:
        return Gate("emergency_events", "unavailable", str(unresolved), 0, "at_most")
    return Gate("emergency_events", "pass", "0", 0, "at_most")


def sample_gate(estimate: MarketEstimate) -> Gate:
    """The primary comparison must rest on supported, curated runs."""
    gaps = [gap for cell in estimate.cells for gap in cell.gaps]
    if gaps:
        return Gate("comparable_sample", "unavailable", None, None, None)
    total = sum(
        cell.arms[arm].payload()["eligible_primary_cycles"]
        for cell in estimate.cells for arm in cell.arms
    )
    if not total:
        return Gate("comparable_sample", "unavailable", "0", 1, "at_least")
    return Gate("comparable_sample", "pass", str(total), 1, "at_least")


def assess(estimate: MarketEstimate, policy: Policy) -> dict:
    integrity = estimate.integrity()
    manual = estimate.delta("manual")
    unresolved = integrity["unresolved_units"]
    coverage = not any(cell.gaps for cell in estimate.cells)
    gates = [
        Gate("planned_cell_coverage", "pass" if coverage else "unavailable"),
        threshold("units_with_unresolved_cycles", unresolved, 0, "at_most"),
        emergency_gate(estimate),
        sample_gate(estimate),
        threshold("alarm_increase_bp", in_basis_points(estimate.delta("alarm")),
                  policy.max_alarm_increase_bp, "at_most"),
        threshold("failure_increase_bp", in_basis_points(estimate.delta("failure")),
                  policy.max_failure_increase_bp, "at_most"),
        threshold("manual_saving_bp", in_basis_points(-manual if manual is not None else None),
                  policy.min_manual_saving_bp, "at_least"),
        energy_gate(estimate, policy),
    ]
    blocking = {"emergency_events"}
    if any(gate.state == "fail" and gate.name != "units_with_unresolved_cycles"
           for gate in gates):
        recommendation = "hold"
    elif any(gate.state != "pass" for gate in gates):
        recommendation = "insufficient"
    else:
        recommendation = "advance"
    return {
        "recommendation": recommendation,
        "gates": [gate.payload() for gate in gates],
        "blocked_by": [gate.name for gate in gates if gate.state != "pass"],
        "emergency_blocked": any(
            gate.name in blocking and gate.state == "fail" for gate in gates
        ),
    }