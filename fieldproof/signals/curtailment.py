from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from sqlite3 import Connection
from typing import Iterable

from fieldproof.common import conflict, encoded, fingerprint, invalid, missing
from fieldproof.design.registry import Roster, Trial, require_phase
from fieldproof.signals.contracts import (
    CurtailmentAck,
    CurtailmentNotice,
    NoticeScope,
)

# A notice is only usable for attribution when it names a site that exists in
# the roster and states where the command actually ran. Anything less is kept
# but reported as unconfirmed instead of being counted as curtailment.
CONFIRMED_SOURCES = frozenset({"grid-operator", "site-controller", "field-dispatch"})


def _revision_key(item) -> tuple[str, str, int]:
    if isinstance(item, CurtailmentNotice):
        return item.site_id, item.notice_id, item.revision
    return item.notice_id, item.unit_id, item.revision


def _store(
    db: Connection,
    trial: Trial,
    delivery_id: str,
    kind: str,
    rows: list[tuple[str, str, int, dict, str, str, str]],
) -> dict:
    """Insert one atomic batch of curtailment facts.

    ``rows`` holds (table, id_column, revision, document, digest, notice_id,
    subject) per fact. The digest of the whole batch guards retries; the
    per-fact digest guards conflicting revisions, exactly like signals. A
    replayed delivery is answered before the phase check, so a retry of an
    accepted batch keeps working after the trial closes.
    """
    trial_id = trial.trial_id
    documents = [row[3]["raw"] for row in rows]
    digest = fingerprint(documents)
    previous = db.execute(
        "SELECT * FROM delivery WHERE trial_id=? AND delivery_id=?",
        (trial_id, delivery_id),
    ).fetchone()
    if previous is not None:
        if previous["digest"] != digest:
            raise conflict("delivery_changed", "Delivery identifier was reused with other content")
        return _receipt(db, trial_id, previous, replayed=True)
    require_phase(trial, "open")
    result = db.execute(
        "INSERT INTO delivery(trial_id, delivery_id, kind, digest) VALUES (?,?,?,?)",
        (trial_id, delivery_id, kind, digest),
    )
    seq = result.lastrowid
    accepted = 0
    for table, id_column, revision, document, fact_digest, notice_id, subject in rows:
        old = db.execute(
            f"SELECT digest FROM {table} WHERE trial_id=? AND {id_column}=? AND revision=?",
            (trial_id, document["id"], revision),
        ).fetchone()
        if old is not None:
            if old["digest"] != fact_digest:
                raise conflict(
                    "revision_changed",
                    "Revision has different content",
                    details={"id": document["id"], "revision": revision},
                )
            continue
        latest = db.execute(
            f"""SELECT {id_column},revision
                FROM {table} WHERE trial_id=? AND {id_column}=?
                ORDER BY revision DESC LIMIT 1""",
            (trial_id, document["id"]),
        ).fetchone()
        expected = 1 if latest is None else latest["revision"] + 1
        if revision != expected:
            raise conflict(
                "revision_gap",
                f"Missing predecessor for {document['id']}",
                details={"expected_revision": expected},
            )
        if latest is not None:
            _check_identity(db, table, trial_id, document["id"], latest["revision"],
                            document["raw"])
        if table == "curtailment_notice":
            db.execute(
                """INSERT INTO curtailment_notice
                   (trial_id,notice_id,revision,delivery_seq,notice,digest)
                   VALUES (?,?,?,?,?,?)""",
                (trial_id, document["id"], revision, seq,
                 encoded(document["raw"]), fact_digest),
            )
        else:
            db.execute(
                """INSERT INTO curtailment_ack
                   (trial_id,ack_id,revision,notice_id,unit_id,delivery_seq,ack,digest)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    trial_id, document["id"], revision, notice_id, subject, seq,
                    encoded(document["raw"]), fact_digest,
                ),
            )
        accepted += 1
    db.execute(
        "UPDATE delivery SET accepted=?, repeated=? WHERE seq=?",
        (accepted, len(rows) - accepted, seq),
    )
    row = db.execute("SELECT * FROM delivery WHERE seq=?", (seq,)).fetchone()
    return _receipt(db, trial_id, row, replayed=False)


def _check_identity(db: Connection, table: str, trial_id: str, fact_id: str,
                    revision: int, raw: dict) -> None:
    """Keep a fact's site/device identity fixed across revisions.

    A revision may correct the interval, source or revocation, but it may not
    silently move a notice to another site or an acknowledgment to another
    device; that would rewrite what a previous cut had already explained.
    """
    if table == "curtailment_notice":
        row = db.execute(
            """SELECT notice FROM curtailment_notice
               WHERE trial_id=? AND notice_id=? AND revision=?""",
            (trial_id, fact_id, revision),
        ).fetchone()
        if row is None:
            return
        previous = CurtailmentNotice.model_validate_json(row["notice"])
        if previous.site_id != raw["site_id"]:
            raise conflict(
                "revision_reassigned",
                f"Notice {fact_id} changed its site",
                details={"expected_site_id": previous.site_id},
            )
        return
    row = db.execute(
        """SELECT notice_id,unit_id FROM curtailment_ack
           WHERE trial_id=? AND ack_id=? AND revision=?""",
        (trial_id, fact_id, revision),
    ).fetchone()
    if row is None:
        return
    if row["notice_id"] != raw["notice_id"] or row["unit_id"] != raw["unit_id"]:
        raise conflict(
            "revision_reassigned",
            f"Acknowledgment {fact_id} changed identity",
            details={"notice_id": row["notice_id"], "unit_id": row["unit_id"]},
        )


def _receipt(db: Connection, trial_id: str, row, replayed: bool) -> dict:
    from fieldproof.signals.ledger import current_cut

    return {
        "delivery_id": row["delivery_id"],
        "delivery_sequence": row["seq"],
        "accepted": row["accepted"],
        "repeated": row["repeated"],
        "current_cut": current_cut(db, trial_id),
        "replayed": replayed,
    }


def _fact_rows(notices: list[CurtailmentNotice]) -> list[tuple]:
    rows = []
    for notice in sorted(notices, key=lambda item: (item.site_id, item.notice_id, item.revision)):
        document = notice.model_dump(mode="json")
        rows.append((
            "curtailment_notice", "notice_id", notice.revision,
            {"id": notice.notice_id, "raw": document},
            fingerprint(document), notice.notice_id, notice.site_id,
        ))
    return rows


def record_notices(
    db: Connection,
    trial: Trial,
    roster: Roster,
    delivery_id: str,
    notices: list[CurtailmentNotice],
) -> dict:
    for notice in notices:
        if notice.site_id not in roster.sites:
            raise missing("site", notice.site_id)
        _check_scope(notice.scope, roster, notice.site_id)
    return _store(db, trial, delivery_id, "curtailment", _fact_rows(notices))


def _check_scope(scope: NoticeScope, roster: Roster, site_id: str):
    if scope.mode != "units":
        return
    for unit_id in scope.unit_ids:
        unit = roster.units.get(unit_id)
        if unit is None:
            raise missing("unit", unit_id)
        if unit.site_id != site_id:
            raise invalid(
                "notice_scope",
                f"Unit {unit_id} is not enrolled at site {site_id}",
                details={"unit_id": unit_id, "site_id": site_id},
            )


def record_acks(
    db: Connection,
    trial: Trial,
    roster: Roster,
    delivery_id: str,
    acknowledgments: list[CurtailmentAck],
) -> dict:
    for ack in acknowledgments:
        if ack.unit_id not in roster.units:
            raise missing("unit", ack.unit_id)
    rows = []
    for ack in sorted(acknowledgments, key=lambda item: (item.notice_id, item.unit_id, item.revision)):
        document = ack.model_dump(mode="json")
        rows.append((
            "curtailment_ack", "ack_id", ack.revision,
            {"id": ack.ack_id, "raw": document},
            fingerprint(document), ack.notice_id, ack.unit_id,
        ))
    return _store(db, trial, delivery_id, "curtailment", rows)


# ---------------------------------------------------------------------------
# As-of reading


def notices_at(db: Connection, trial_id: str, cut: int) -> list[CurtailmentNotice]:
    rows = db.execute(
        """WITH latest AS (
               SELECT notice_id,MAX(revision) AS revision
               FROM curtailment_notice WHERE trial_id=? AND delivery_seq<=?
               GROUP BY notice_id
           )
           SELECT curtailment_notice.notice
           FROM curtailment_notice JOIN latest USING(notice_id,revision)
           WHERE curtailment_notice.trial_id=?
           ORDER BY notice_id""",
        (trial_id, cut, trial_id),
    )
    return [
        CurtailmentNotice.model_validate_json(row["notice"]) for row in rows
        if not CurtailmentNotice.model_validate_json(row["notice"]).void
    ]


def acks_at(db: Connection, trial_id: str, cut: int) -> list[CurtailmentAck]:
    rows = db.execute(
        """WITH latest AS (
               SELECT ack_id,MAX(revision) AS revision
               FROM curtailment_ack WHERE trial_id=? AND delivery_seq<=?
               GROUP BY ack_id
           )
           SELECT curtailment_ack.ack
           FROM curtailment_ack JOIN latest USING(ack_id,revision)
           WHERE curtailment_ack.trial_id=?
           ORDER BY notice_id,unit_id""",
        (trial_id, cut, trial_id),
    )
    return [
        CurtailmentAck.model_validate_json(row["ack"]) for row in rows
        if not CurtailmentAck.model_validate_json(row["ack"]).void
    ]


def notice_history(db: Connection, trial_id: str, notice_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT notice,delivery_seq FROM curtailment_notice
           WHERE trial_id=? AND notice_id=? ORDER BY revision""",
        (trial_id, notice_id),
    )
    items = [
        {
            "notice": CurtailmentNotice.model_validate_json(row["notice"]).model_dump(mode="json"),
            "delivery_sequence": row["delivery_seq"],
        }
        for row in rows
    ]
    if not items:
        raise missing("notice", notice_id)
    return items


def notice_ledger(db: Connection, trial_id: str, cut: int, site: str | None) -> dict:
    """The notice commands and revocations visible at a cut.

    Revoked, retracted and still-active notices are all shown. A reviewer can
    then tell a command that never ran from one that was withdrawn, instead of
    seeing both as an empty exposure.
    """
    latest: dict[str, dict] = {}
    for row in db.execute(
        """SELECT notice_id,revision,delivery_seq,notice
           FROM curtailment_notice WHERE trial_id=? AND delivery_seq<=?
           ORDER BY notice_id,revision""",
        (trial_id, cut),
    ):
        latest[row["notice_id"]] = {
            "revision": row["revision"],
            "delivery_sequence": row["delivery_seq"],
            **CurtailmentNotice.model_validate_json(row["notice"]).model_dump(mode="json"),
        }
    items = []
    for notice in latest.values():
        if site is not None and notice["site_id"] != site:
            continue
        items.append({
            "notice_id": notice["notice_id"],
            "revision": notice["revision"],
            "site_id": notice["site_id"],
            "state": _notice_state(notice),
            "starts_at": notice["starts_at"],
            "ends_at": notice["ends_at"],
            "effective_ends_at": _effective_end(notice),
            "scope": notice["scope"],
            "source": notice["source"],
            "source_ref": notice["source_ref"],
            "reason": notice["reason"],
            "revocation": notice["revocation"],
            "delivery_sequence": notice["delivery_sequence"],
        })
    items.sort(key=lambda entry: (entry["site_id"], entry["starts_at"], entry["notice_id"]))
    return {
        "cut": cut,
        "site": site,
        "notices": items,
        "counts": {
            "active": sum(item["state"] == "active" for item in items),
            "revoked": sum(item["state"] == "revoked" for item in items),
            "retracted": sum(item["state"] == "retracted" for item in items),
            "unconfirmed_source": sum(item["source"] == "unconfirmed" for item in items),
        },
    }


def _notice_state(notice: dict) -> str:
    if notice["void"]:
        return "retracted"
    if notice["revocation"] is not None:
        return "revoked"
    return "active"


def _effective_end(notice: dict) -> str:
    if notice["void"]:
        return notice["starts_at"]
    revocation = notice["revocation"]
    if revocation is not None and revocation["revoked_at"] < notice["ends_at"]:
        return revocation["revoked_at"]
    return notice["ends_at"]


def overlap_diagnostics(db: Connection, trial_id: str, cut: int, site: str | None) -> dict:
    """Group notices whose command intervals overlap on the same site."""
    notices = [
        notice for notice in notices_at(db, trial_id, cut)
        if site is None or notice.site_id == site
    ]
    groups = []
    for index, left in enumerate(notices):
        for right in notices[index + 1:]:
            if left.site_id != right.site_id:
                continue
            left_span, right_span = _window(left), _window(right)
            if left_span.intersects(right_span.start, right_span.end):
                groups.append({
                    "site_id": left.site_id,
                    "notice_ids": sorted({left.notice_id, right.notice_id}),
                    "overlap_starts_at": max(left_span.start, right_span.start).isoformat(),
                    "overlap_ends_at": min(left_span.end, right_span.end).isoformat(),
                    "scopes": {
                        left.notice_id: left.scope.model_dump(mode="json"),
                        right.notice_id: right.scope.model_dump(mode="json"),
                    },
                })
    return {"cut": cut, "site": site, "overlaps": groups}


def ack_history(db: Connection, trial_id: str, ack_id: str) -> list[dict]:
    rows = db.execute(
        """SELECT ack,delivery_seq FROM curtailment_ack
           WHERE trial_id=? AND ack_id=? ORDER BY revision""",
        (trial_id, ack_id),
    )
    items = [
        {
            "acknowledgment": CurtailmentAck.model_validate_json(row["ack"]).model_dump(mode="json"),
            "delivery_sequence": row["delivery_seq"],
        }
        for row in rows
    ]
    if not items:
        raise missing("acknowledgment", ack_id)
    return items


# ---------------------------------------------------------------------------
# Unit exposure


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def intersects(self, start: datetime, end: datetime) -> bool:
        return start < self.end and self.start < end


@dataclass
class UnitExposure:
    """The curtailment facts one device carries at one fact cut."""

    unit_id: str
    site_id: str
    command: list[Interval] = field(default_factory=list)
    command_notices: list[tuple[str, Interval]] = field(default_factory=list)
    executed: list[Interval] = field(default_factory=list)
    executed_notices: list[tuple[str, Interval]] = field(default_factory=list)
    notice_ids: list[str] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)
    unconfirmed: bool = False
    issues: list[str] = field(default_factory=list)

    @property
    def affected(self) -> bool:
        return bool(self.executed)

    @property
    def has_unknown(self) -> bool:
        return bool(self.unknown) or self.unconfirmed

    def covered(self, start: datetime, end: datetime) -> bool:
        return any(item.intersects(start, end) for item in self.executed)

    def command_covered(self, start: datetime, end: datetime) -> bool:
        return any(item.intersects(start, end) for item in self.command)

    def fully_covered(self, start: datetime, end: datetime) -> bool:
        return any(item.start <= start and end <= item.end for item in self.executed)


def _scope_units(notice: CurtailmentNotice, roster: Roster, site_units: list[str]) -> list[str]:
    scope = notice.scope
    if scope.mode == "site":
        return list(site_units)
    if scope.mode == "units":
        return list(scope.unit_ids)
    selected = []
    for unit_id in site_units:
        unit = roster.units[unit_id]
        site = roster.sites[unit.site_id]
        cell = f"{site.climate}/{unit.load}"
        if scope.cells and cell not in scope.cells:
            continue
        if scope.arms and unit.arm not in scope.arms:
            continue
        selected.append(unit_id)
    return selected


def _window(notice: CurtailmentNotice) -> Interval:
    return Interval(notice.starts_at, notice.command_end())


def build_exposure(
    roster: Roster,
    notices: Iterable[CurtailmentNotice],
    acks: Iterable[CurtailmentAck],
) -> dict[str, UnitExposure]:
    by_site: dict[str, list[str]] = {}
    for unit_id, unit in roster.units.items():
        by_site.setdefault(unit.site_id, []).append(unit_id)
    exposure = {
        unit_id: UnitExposure(unit_id, unit.site_id)
        for unit_id, unit in roster.units.items()
    }
    live: dict[str, CurtailmentNotice] = {}
    site_wide_unknown: dict[str, list[dict]] = {}
    for notice in notices:
        if not notice.active():
            continue
        live[notice.notice_id] = notice
        selected = _scope_units(notice, roster, by_site.get(notice.site_id, []))
        interval = _window(notice)
        confirmed = notice.source in CONFIRMED_SOURCES
        if not confirmed:
            # An unconfirmed source is reported per selected unit and, for a
            # site-wide command, for the whole site: the site cannot be assumed
            # ordinary for either arm in this window.
            site_wide_unknown.setdefault(notice.site_id, []).append({
                "notice_id": notice.notice_id,
                "reason": "notice_source_unconfirmed",
                "source": notice.source,
                "mode": notice.scope.mode,
                "starts_at": interval.start.isoformat(),
                "ends_at": interval.end.isoformat(),
            })
        for unit_id in selected:
            item = exposure[unit_id]
            item.notice_ids.append(notice.notice_id)
            if confirmed:
                item.command.append(interval)
                item.command_notices.append((notice.notice_id, interval))
            else:
                item.unconfirmed = True
                item.issues.append("notice_source_unconfirmed")
                item.unknown.append({
                    "notice_id": notice.notice_id,
                    "unit_id": unit_id,
                    "reason": "notice_source_unconfirmed",
                    "source": notice.source,
                    "starts_at": interval.start.isoformat(),
                    "ends_at": interval.end.isoformat(),
                })
    acknowledged: dict[tuple[str, str], bool] = {}
    for ack in acks:
        if ack.void or ack.unit_id not in exposure:
            continue
        acknowledged[(ack.unit_id, ack.notice_id)] = True
        item = exposure[ack.unit_id]
        notice = live.get(ack.notice_id)
        if ack.status == "affected" and ack.affected_from and ack.affected_until:
            interval = Interval(ack.affected_from, ack.affected_until)
            item.executed.append(interval)
            item.executed_notices.append((ack.notice_id, interval))
        elif ack.status == "unknown":
            window = _window(notice) if notice is not None else None
            item.issues.append("acknowledgment_unknown")
            item.unknown.append({
                "notice_id": ack.notice_id,
                "unit_id": ack.unit_id,
                "ack_id": ack.ack_id,
                "reason": "acknowledgment_unknown",
                "observed_at": ack.observed_at.isoformat(),
                "starts_at": window.start.isoformat() if window else None,
                "ends_at": window.end.isoformat() if window else None,
            })
            if notice is not None:
                site_wide_unknown.setdefault(notice.site_id, []).append({
                    "notice_id": ack.notice_id,
                    "reason": "acknowledgment_unknown",
                    "mode": notice.scope.mode,
                    "starts_at": window.start.isoformat(),
                    "ends_at": window.end.isoformat(),
                })
        elif ack.status == "unaffected":
            item.issues.append("acknowledgment_unaffected")
    # A notice that was issued for a unit but never acknowledged leaves the
    # execution unknown. A notice delivered but never executed is not treated
    # as ordinary operation for either arm.
    for unit_id, item in exposure.items():
        for notice_id in dict.fromkeys(item.notice_ids):
            if (unit_id, notice_id) in acknowledged:
                continue
            notice = live.get(notice_id)
            window = _window(notice) if notice is not None else None
            item.unknown.append({
                "notice_id": notice_id,
                "unit_id": unit_id,
                "reason": "acknowledgment_missing",
                "starts_at": window.start.isoformat() if window else None,
                "ends_at": window.end.isoformat() if window else None,
            })
            if "acknowledgment_missing" not in item.issues:
                item.issues.append("acknowledgment_missing")
            if notice is not None:
                site_wide_unknown.setdefault(notice.site_id, []).append({
                    "notice_id": notice_id,
                    "reason": "acknowledgment_missing",
                    "mode": notice.scope.mode,
                    "starts_at": window.start.isoformat(),
                    "ends_at": window.end.isoformat(),
                })
    _propagate_site_unknown(exposure, by_site, site_wide_unknown)
    return exposure


def _propagate_site_unknown(
    exposure: dict[str, UnitExposure],
    by_site: dict[str, list[str]],
    site_unknown: dict[str, list[dict]],
) -> None:
    """Spread an unresolved site-wide command to every unit at that site.

    A site-wide command is issued to the site, not to a single device. When its
    source or execution cannot be confirmed, the other devices at that site
    during the same window must not be booked as ordinary operation. Scoped
    notices stay limited to their declared units, because those units are
    explicitly outside the command and were never expected to be affected.
    """
    for site_id, entries in site_unknown.items():
        for entry in entries:
            window = _unknown_interval(entry)
            if window is None or entry.get("mode") not in (None, "site"):
                continue
            for unit_id in by_site.get(site_id, []):
                item = exposure[unit_id]
                already = any(
                    existing["notice_id"] == entry["notice_id"]
                    and existing.get("starts_at") == entry["starts_at"]
                    for existing in item.unknown
                )
                if already:
                    continue
                item.unknown.append({
                    "notice_id": entry["notice_id"],
                    "unit_id": unit_id,
                    "reason": "site_exposure_unconfirmed",
                    "origin": entry["reason"],
                    "starts_at": entry["starts_at"],
                    "ends_at": entry["ends_at"],
                })
                if "site_exposure_unconfirmed" not in item.issues:
                    item.issues.append("site_exposure_unconfirmed")


def _unknown_interval(entry: dict) -> Interval | None:
    start = entry.get("starts_at")
    end = entry.get("ends_at")
    if not start or not end:
        return None
    return Interval(datetime.fromisoformat(start), datetime.fromisoformat(end))


def classify(cycle, exposure: UnitExposure | None) -> None:
    """Attach the curtailment facts a cycle carries.

    The classification uses the actual timestamps of the command, the runtime
    acknowledgment, the alarm and the terminal event. It never infers a cause
    from the existence of a notice alone, nor from two events merely sharing a
    minute.
    """
    if cycle.start is None:
        return
    start = cycle.start.occurred_at
    end = cycle.close.occurred_at if cycle.close is not None else datetime.max.replace(tzinfo=UTC)
    if exposure is None:
        exposure = UnitExposure(cycle.unit_id, "")
    covered_unknown = [
        entry for entry in exposure.unknown
        if (window := _unknown_interval(entry)) is not None
        and window.intersects(start, end)
    ]
    overlapping = _overlapping_notices(exposure, start, end)
    if covered_unknown:
        cycle.exposure = "unknown"
        cycle.exposure_reasons = sorted({entry["reason"] for entry in covered_unknown})
        cycle.notice_ids = sorted({entry["notice_id"] for entry in covered_unknown})
        cycle.during_curtailment = True
    elif exposure.executed and exposure.covered(start, end):
        cycle.exposure = "curtailed"
        cycle.exposure_reasons = ["executed_curtailment"]
        cycle.notice_ids = sorted(dict.fromkeys(exposure.notice_ids))
        cycle.during_curtailment = True
        window = _covering(exposure, start, end)
        if window is not None:
            cycle.executed_from = window.start
            cycle.executed_until = window.end
    elif exposure.command and exposure.command_covered(start, end):
        cycle.exposure = "command_unexecuted"
        cycle.exposure_reasons = ["notice_without_execution"]
        cycle.notice_ids = sorted(dict.fromkeys(exposure.notice_ids))
        cycle.during_curtailment = True
    else:
        cycle.exposure = "normal"
        cycle.exposure_reasons = []
        cycle.notice_ids = []
        cycle.during_curtailment = False
    if overlapping:
        # Two commands touching the same run are reported explicitly. The
        # earliest execution boundary still drives the stop classification, but
        # a reviewer sees that coverage was ambiguous.
        cycle.exposure_reasons = sorted({*cycle.exposure_reasons, "overlapping_notices"})
        cycle.notice_ids = sorted({*cycle.notice_ids, *overlapping})
    anomaly_first = (
        cycle.state == "pending"
        and cycle.alarm_at is not None
        and cycle.executed_from is not None
        and cycle.alarm_at <= cycle.executed_from
    )
    if cycle.state == "pending" and cycle.exposure == "curtailed" and not anomaly_first:
        # A missing terminal while an executed command was running is the
        # external stop itself, not a device that failed to report. If a device
        # anomaly was already reported before the command window started, the
        # run stays unresolved and the anomaly keeps priority.
        cycle.state = "external_stop"
        cycle.issues = [issue for issue in cycle.issues if issue != "missing_terminal"]
        cycle.issues.append("stopped_by_external_command")
    cycle.attribution = attribution(cycle)


def _covering(exposure: UnitExposure, start: datetime, end: datetime) -> Interval | None:
    for span in exposure.executed:
        if span.intersects(start, end):
            return span
    return None


def _overlapping_notices(exposure: UnitExposure, start: datetime, end: datetime) -> set[str]:
    """Notice identifiers whose command intervals overlap within this run."""
    touching = [
        (notice_id, span) for notice_id, span in exposure.command_notices
        if span.intersects(start, end)
    ]
    overlapping: set[str] = set()
    for index, (left_id, left) in enumerate(touching):
        for right_id, right in touching[index + 1:]:
            if left.intersects(right.start, right.end):
                overlapping.update({left_id, right_id})
    return overlapping


def attribution(cycle) -> str:
    """Classify why a cycle ended, keeping command and anomaly order intact."""
    if cycle.state == "external_stop":
        return "external_stop"
    if cycle.state == "pending" and cycle.alarm_at is not None and cycle.during_curtailment:
        # A device reported an anomaly but never a terminal, inside a command
        # window: the anomaly is the better-supported cause.
        return "equipment_anomaly_first"
    if cycle.state == "aborted":
        # A device anomaly timestamped at or before the terminal event happened
        # first, even when the abort overlaps a grid command. An abort the
        # device itself reported is a device outcome, not an external stop,
        # unless the device never produced a terminal at all.
        if cycle.alarm_at is not None and cycle.close is not None:
            if cycle.alarm_at <= cycle.close.occurred_at:
                return "equipment_anomaly_first"
        return "equipment_failure"
    if cycle.state == "pending" and cycle.exposure == "unknown":
        return "exposure_unknown"
    if cycle.state == "complete" and cycle.alarm:
        return "completed_with_alarm"
    if cycle.state == "complete" and cycle.during_curtailment:
        return "curtailed_exposure_completed"
    return "normal"


def exposure_report(db: Connection, trial: Trial, material, site: str | None) -> dict:
    """Which device was affected when, and why a reviewer cannot tell.

    Every device appears, including ones with only unconfirmed sources or a
    notice that was never executed, so the report explains the gap instead of
    hiding it behind an empty list.
    """
    units = sorted(material.exposure.values(), key=lambda item: (item.site_id, item.unit_id))
    if site is not None:
        if site not in {unit.site_id for unit in units}:
            raise missing("site", site)
        units = [unit for unit in units if unit.site_id == site]
    cycles_by_unit: dict[str, list] = {}
    for cycle in material.cycles:
        cycles_by_unit.setdefault(cycle.unit_id, []).append(cycle)
    rows = []
    for item in units:
        unit_cycles = _ordered_cycles(cycles_by_unit.get(item.unit_id, []))
        rows.append({
            "unit_id": item.unit_id,
            "site_id": item.site_id,
            "arm": material.units[item.unit_id].arm if item.unit_id in material.units else None,
            "notice_ids": sorted(dict.fromkeys(item.notice_ids)),
            "command_intervals": [_interval_payload(span) for span in item.command],
            "executed_intervals": [_interval_payload(span) for span in item.executed],
            "unknown": item.unknown,
            "issues": sorted(set(item.issues)),
            "cycles": [
                {
                    "cycle_id": cycle.cycle_id,
                    "state": cycle.state,
                    "attribution": cycle.attribution,
                    "exposure": cycle.exposure,
                    "exposure_reasons": cycle.exposure_reasons,
                    "notice_ids": cycle.notice_ids,
                    "started_at": cycle.start.occurred_at.isoformat() if cycle.start else None,
                    "closed_at": (
                        cycle.close.occurred_at.isoformat()
                        if cycle.close
                        else cycle.stop_at.isoformat() if cycle.stop_at else None
                    ),
                    "comparable": cycle.comparable,
                    "primary": cycle.primary,
                }
                for cycle in unit_cycles
            ],
        })
    return {
        "cut": material.cut,
        "window": material.window.payload(),
        "site": site,
        "units": rows,
    }


def _ordered_cycles(cycles: list) -> list:
    return sorted(
        cycles,
        key=lambda cycle: (
            cycle.start.occurred_at if cycle.start is not None
            else datetime.min.replace(tzinfo=UTC),
            cycle.cycle_id,
        ),
    )


def _interval_payload(span: Interval) -> dict:
    return {"starts_at": span.start.isoformat(), "ends_at": span.end.isoformat()}
