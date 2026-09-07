"""Typed contracts for the attendance API and rules engine."""
from __future__ import annotations

from datetime import date, datetime, time
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class AttendanceStatus(StrEnum):
    PRESENT = "P"
    LATE = "LT"
    EARLY_EXIT = "EE"
    PAID_PERMISSION = "PP"
    OFFICIAL_DUTY = "OD"
    WORK_FROM_HOME = "WFH"
    PAID_LEAVE = "PL"
    UNPAID_LEAVE = "UL"
    ABSENT = "A"
    MISSING_PUNCH = "MP"
    WEEKLY_OFF = "WO"
    HOLIDAY = "H"


class Shift(BaseModel):
    start: time = time(10, 0)
    end: time = time(19, 0)
    break_minutes: int = Field(default=0, ge=0, le=480)

    @field_validator("end")
    @classmethod
    def end_must_differ(cls, value: time, info):
        if info.data.get("start") == value:
            raise ValueError("shift start and end cannot be equal")
        return value


class Evidence(BaseModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    media_id: str | None = None
    note: str | None = Field(default=None, max_length=1000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PunchRequest(BaseModel):
    action: Literal["check_in", "check_out"]
    idempotency_key: str = Field(min_length=1, max_length=200)
    employee_id: str | None = None
    occurred_at: datetime | None = None
    source: Literal["web", "whatsapp", "admin", "device"] = "web"
    evidence: Evidence = Field(default_factory=Evidence)


class PermissionRequest(BaseModel):
    employee_id: str | None = None
    attendance_date: date
    kind: Literal["late", "early_exit", "official_duty", "work_from_home", "paid_leave", "unpaid_leave"]
    requested_minutes: int = Field(default=0, ge=0, le=1440)
    reason: str = Field(min_length=1, max_length=1000)


class PermissionDecision(BaseModel):
    approved: bool
    reason: str = Field(min_length=1, max_length=1000)
    emergency_override: bool = False


class AdjustmentRequest(BaseModel):
    employee_id: str
    attendance_date: date
    kind: Literal["add_punch", "time_recovery", "status_override"]
    reason: str = Field(min_length=1, max_length=1000)
    approved_by: str | None = None
    action: Literal["check_in", "check_out"] | None = None
    occurred_at: datetime | None = None
    recovered_minutes: int = Field(default=0, ge=0, le=1440)
    status: AttendanceStatus | None = None


class ShiftAssignmentRequest(BaseModel):
    employee_id: str
    effective_from: date
    shift: Shift = Field(default_factory=Shift)
    reason: str = Field(min_length=1, max_length=1000)


class CalendarDayRequest(BaseModel):
    employee_id: str
    attendance_date: date
    status: Literal["WO", "H", "PL", "UL"]
    reason: str = Field(min_length=1, max_length=1000)
