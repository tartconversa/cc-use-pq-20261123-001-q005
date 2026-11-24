from __future__ import annotations

from datetime import UTC
from typing import Annotated, Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from fieldproof.design.contracts import Arm, Input, Key, ShortText, WholeSecond

SignalKind = Literal["start", "complete", "abort", "manual", "alarm", "meter"]
MeterValue = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]
Revision = Annotated[StrictInt, Field(ge=1, le=1_000_000)]
CellKey = Annotated[str, Field(pattern=r"^(cold|temperate|warm)/(light|heavy)$")]


class Event(Input):
    event_id: Key
    revision: Revision
    unit_id: Key
    cycle_id: Key
    occurred_at: WholeSecond
    kind: SignalKind
    void: bool = False
    meter_wh: MeterValue | None = None
    meter_id: Key | None = None
    reason: Key | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value):
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def signal_fields(self):
        if self.void:
            if self.revision == 1:
                raise ValueError("A retraction must revise an existing event")
            if any(v is not None for v in (self.meter_wh, self.meter_id, self.reason)):
                raise ValueError("A retraction cannot contain a measurement or reason")
            return self
        meter = self.kind == "meter"
        if meter != (self.meter_wh is not None) or meter != (self.meter_id is not None):
            raise ValueError("Only meter events carry both a meter ID and a Wh reading")
        if (self.kind in {"abort", "alarm"}) != (self.reason is not None):
            raise ValueError("Only abort and alarm events require reason codes")
        return self

    def identity(self) -> tuple[str, str, str]:
        return self.unit_id, self.cycle_id, self.kind


class Delivery(Input):
    delivery_id: Key
    events: list[Event] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_revisions(self):
        keys = {(event.event_id, event.revision) for event in self.events}
        if len(keys) != len(self.events):
            raise ValueError("A delivery cannot contain an event revision twice")
        return self


NoticeSource = Literal["grid-operator", "site-controller", "field-dispatch", "unconfirmed"]
AckStatus = Literal["affected", "unaffected", "unknown"]


class NoticeScope(Input):
    """The execution scope a site declares for a curtailment notice."""

    mode: Literal["site", "units", "selection"]
    unit_ids: list[Key] = Field(default_factory=list, max_length=500)
    cells: list[CellKey] = Field(default_factory=list, max_length=6)
    arms: list[Arm] = Field(default_factory=list, max_length=2)

    @model_validator(mode="after")
    def shape(self):
        if len(set(self.unit_ids)) != len(self.unit_ids):
            raise ValueError("A notice scope cannot repeat a unit")
        if len(set(self.cells)) != len(self.cells):
            raise ValueError("A notice scope cannot repeat a cell")
        if len(set(self.arms)) != len(self.arms):
            raise ValueError("A notice scope cannot repeat an arm")
        if self.mode == "site":
            if self.unit_ids or self.cells or self.arms:
                raise ValueError("A site-wide scope carries no selectors")
        elif self.mode == "units":
            if not self.unit_ids or self.cells or self.arms:
                raise ValueError("A unit scope requires unit identifiers only")
        elif not self.cells and not self.arms:
            raise ValueError("A selection scope requires a cell or arm selector")
        return self


class Revocation(Input):
    revoked_at: WholeSecond
    by: Key
    reason: ShortText

    @field_validator("revoked_at")
    @classmethod
    def normalize_time(cls, value):
        return value.astimezone(UTC)


class CurtailmentNotice(Input):
    notice_id: Key
    revision: Revision
    site_id: Key
    starts_at: WholeSecond
    ends_at: WholeSecond
    scope: NoticeScope
    source: NoticeSource
    reason: ShortText
    source_ref: ShortText | None = None
    revocation: Revocation | None = None
    void: bool = False

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_time(cls, value):
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def notice_fields(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("A curtailment notice ends after it starts")
        if self.void:
            if self.revision == 1:
                raise ValueError("A retraction must revise an existing notice")
            if self.revocation is not None:
                raise ValueError("A retraction cannot also record a revocation")
            return self
        if self.revision == 1 and self.revocation is not None:
            raise ValueError("A revocation must revise an existing notice")
        if self.source == "unconfirmed" and self.source_ref is not None:
            raise ValueError("An unconfirmed source cannot carry a source reference")
        return self

    def identity(self) -> tuple[str, str]:
        return self.site_id, self.notice_id

    def command_end(self):
        if self.revocation is not None and self.revocation.revoked_at < self.ends_at:
            return self.revocation.revoked_at
        return self.ends_at

    def active(self) -> bool:
        return not self.void and self.command_end() > self.starts_at


class CurtailmentAck(Input):
    ack_id: Key
    revision: Revision
    notice_id: Key
    unit_id: Key
    status: AckStatus
    observed_at: WholeSecond
    affected_from: WholeSecond | None = None
    affected_until: WholeSecond | None = None
    note: ShortText | None = None
    void: bool = False

    @field_validator("observed_at", "affected_from", "affected_until")
    @classmethod
    def normalize_time(cls, value):
        return None if value is None else value.astimezone(UTC)

    @model_validator(mode="after")
    def ack_fields(self):
        if self.void:
            if self.revision == 1:
                raise ValueError("A retraction must revise an existing acknowledgment")
            if self.affected_from is not None or self.affected_until is not None:
                raise ValueError("A retraction cannot carry an affected interval")
            return self
        if self.status == "affected":
            if self.affected_from is None or self.affected_until is None:
                raise ValueError("An affected acknowledgment needs the actual interval")
            if self.affected_until <= self.affected_from:
                raise ValueError("An affected interval ends after it starts")
        elif self.affected_from is not None or self.affected_until is not None:
            raise ValueError("Only an affected acknowledgment carries an interval")
        return self

    def identity(self) -> tuple[str, str]:
        return self.notice_id, self.unit_id


class NoticeBatch(Input):
    delivery_id: Key
    notices: list[CurtailmentNotice] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def unique_revisions(self):
        keys = {(item.notice_id, item.revision) for item in self.notices}
        if len(keys) != len(self.notices):
            raise ValueError("A notice delivery cannot repeat a notice revision")
        return self


class AckBatch(Input):
    delivery_id: Key
    acknowledgments: list[CurtailmentAck] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def unique_revisions(self):
        keys = {(item.ack_id, item.revision) for item in self.acknowledgments}
        if len(keys) != len(self.acknowledgments):
            raise ValueError("An acknowledgment delivery cannot repeat a revision")
        return self
