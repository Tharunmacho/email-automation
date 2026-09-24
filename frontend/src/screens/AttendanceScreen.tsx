"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  CalendarDays,
  CheckCircle2,
  Clock3,
  LogIn,
  LogOut,
  Percent,
  RefreshCw,
  ShieldCheck,
  Timer,
  Trash2,
  Users,
  WalletCards,
  XCircle,
} from "lucide-react";

import {
  createDutyPlan,
  decideAttendancePermission,
  decideExtraOt,
  deleteDutyPlan,
  fetchAttendanceDay,
  fetchAttendanceEmployees,
  fetchAttendanceMonth,
  fetchAttendancePermissions,
  fetchAttendanceWeeklyOff,
  fetchDutyPlans,
  fetchExtraOtRequests,
  recordAttendancePunch,
  requestAttendancePermission,
  requestExtraOt,
  updateAttendanceWeeklyOff,
  type AttendanceDay,
  type AttendanceMonth,
  type AttendancePermission,
  type AuthUser,
  type DutyPlan,
  type ExtraOtRequest,
  type StaffMember,
  type WeeklyOffPattern,
} from "@/lib/api";

interface Props {
  user: AuthUser;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
}

interface AttendanceSummary {
  percentage: number;
  scheduled: number;
  attended: number;
  absent: number;
  late: number;
  missing: number;
}

const STATUS: Record<string, string> = {
  P: "Present",
  LT: "Late",
  EE: "Early exit",
  PP: "Paid permission",
  OD: "Official duty",
  WFH: "Work from home",
  PL: "Paid leave",
  UL: "Unpaid leave",
  A: "Absent",
  MP: "Missing punch",
  WO: "Weekly off",
  H: "Holiday",
};

const PERMISSION_KIND: Record<AttendancePermission["kind"], string> = {
  late: "Late arrival",
  early_exit: "Early leaving",
  early_check_in: "Early check-in",
  official_duty: "Official duty",
  work_from_home: "Work from home",
  paid_leave: "Paid leave",
  unpaid_leave: "Unpaid leave",
};

/**
 * Permission kinds measured in minutes rather than taken as a whole day.
 *
 * `early_check_in` belongs here: the minutes are how early the employee expects
 * to arrive, and the backend credits exactly that much presence before the
 * shift start.
 */
const TIMED_KINDS: AttendancePermission["kind"][] = ["late", "early_exit", "early_check_in"];

/** Kinds the backend requires to be filed before the shift begins. */
const BEFORE_SHIFT_KINDS: AttendancePermission["kind"][] = [
  "late",
  "early_check_in",
  "work_from_home",
];

const ATTENDED = new Set(["P", "LT", "EE", "PP", "OD", "WFH", "PL"]);
const NON_WORKING = new Set(["WO", "H"]);

function kolkataDate(): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts();
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function timeOf(value?: string | null): string {
  if (!value) return "Not recorded";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

/**
 * How long the employee was present.
 *
 * `actual_covered_minutes` is the server's own figure and the one the coverage
 * equation was evaluated against, so it is used wherever it is available. The
 * subtraction below is only a fallback for a day record written before the
 * field existed — it is not a second implementation of the rule, and it must
 * not become one.
 */
function workedMinutes(day?: AttendanceDay | null): number {
  if (typeof day?.actual_covered_minutes === "number") return day.actual_covered_minutes;
  if (!day?.check_in || !day.check_out) return 0;
  return Math.max(0, Math.floor((new Date(day.check_out).getTime() - new Date(day.check_in).getTime()) / 60_000));
}

function durationLabel(minutes: number): string {
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return hours ? `${hours}h ${remainder}m` : `${remainder} min`;
}

function monthSummary(month?: AttendanceMonth | null): AttendanceSummary {
  const days = month?.days ?? [];
  const scheduled = days.filter((day) => !NON_WORKING.has(day.status)).length;
  const attended = days.filter(
    (day) => ATTENDED.has(day.status) || Boolean(day.provisional && day.check_in),
  ).length;
  return {
    percentage: scheduled ? Math.round((attended / scheduled) * 1000) / 10 : 0,
    scheduled,
    attended,
    absent: days.filter((day) => day.status === "A" || day.status === "UL").length,
    late: days.filter((day) => day.status === "LT").length,
    missing: days.filter((day) => day.status === "MP").length,
  };
}

function statusLabel(day?: AttendanceDay | null): string {
  if (day?.provisional && day.check_in && !day.check_out) return "Checked in";
  if (day?.status === "MP" && !day.check_in && !day.check_out) return "Awaiting attendance";
  if (day?.status === "MP" && !day.check_in && day.check_out) return "Missing check-in";
  return STATUS[day?.status || ""] || "—";
}

function statusTone(status: string): string {
  if (status === "approved") return "is-ok";
  if (status === "rejected") return "is-bad";
  return "is-warn";
}

export default function AttendanceScreen({ user, onToast }: Props) {
  const today = useMemo(() => kolkataDate(), []);
  const [yearMonth, setYearMonth] = useState(today.slice(0, 7));
  const [day, setDay] = useState<AttendanceDay | null>(null);
  const [month, setMonth] = useState<AttendanceMonth | null>(null);
  const [permissions, setPermissions] = useState<AttendancePermission[]>([]);
  const [staff, setStaff] = useState<StaffMember[]>([]);
  const [employeeId, setEmployeeId] = useState("");
  const [adminMonths, setAdminMonths] = useState<Record<string, AttendanceMonth>>({});
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<AttendancePermission["kind"]>("late");
  const [minutes, setMinutes] = useState("15");
  const [reason, setReason] = useState("");
  const [permissionDate, setPermissionDate] = useState(today);
  const [viewMode, setViewMode] = useState<"team" | "mine">("team");
  const [weeklyOff, setWeeklyOff] = useState<WeeklyOffPattern>("sunday");
  const [savedWeeklyOff, setSavedWeeklyOff] = useState<WeeklyOffPattern>("sunday");
  const [weeklyOffBusy, setWeeklyOffBusy] = useState(false);
  const [extraOt, setExtraOt] = useState<ExtraOtRequest[]>([]);
  const [dutyPlans, setDutyPlans] = useState<DutyPlan[]>([]);
  const [otDate, setOtDate] = useState(today);
  const [otMinutes, setOtMinutes] = useState("60");
  const [otReason, setOtReason] = useState("");
  const [dutyDate, setDutyDate] = useState(today);
  const [dutyStart, setDutyStart] = useState("10:00");
  const [dutyEnd, setDutyEnd] = useState("19:00");
  const [dutyBreak, setDutyBreak] = useState("60");
  const [dutyReason, setDutyReason] = useState("");

  const canManage = user.role === "admin" || user.role === "manager";
  const isTeamView = canManage && viewMode === "team";
  const [yearText, monthText] = yearMonth.split("-");
  const year = Number(yearText);
  const monthNumber = Number(monthText);
  const selectedStaff = staff.find((person) => person.id === employeeId);

  useEffect(() => {
    if (!canManage) return;
    let active = true;
    fetchAttendanceEmployees()
      .then(({ items }) => {
        if (!active) return;
        const roster = (items ?? []).filter((person) => person.active);
        setStaff(roster);
        setEmployeeId((current) => current || roster[0]?.id || "");
      })
      .catch(() => onToast("Could not load employees", "error"));
    return () => {
      active = false;
    };
  }, [canManage, onToast]);

  const load = useCallback(async (showLoading = true) => {
    if (!year || !monthNumber || (isTeamView && staff.length === 0)) return;
    if (showLoading) setLoading(true);
    try {
      if (isTeamView) {
        const [months, permissionResult, otResult, dutyResult] = await Promise.all([
          Promise.all(
            staff.map(async (person) => [
              person.id,
              await fetchAttendanceMonth(year, monthNumber, person.id),
            ] as const),
          ),
          fetchAttendancePermissions(year, monthNumber),
          fetchExtraOtRequests(year, monthNumber),
          fetchDutyPlans(year, monthNumber),
        ]);
        const byEmployee = Object.fromEntries(months);
        setAdminMonths(byEmployee);
        setPermissions(permissionResult.items ?? []);
        setExtraOt(otResult.items ?? []);
        setDutyPlans(dutyResult.items ?? []);
        if (employeeId) setDay(await fetchAttendanceDay(today, employeeId));
      } else {
        const scope = user.role === "manager" ? user.id : undefined;
        const [daily, monthly, permissionResult, otResult, dutyResult] = await Promise.all([
          fetchAttendanceDay(today),
          fetchAttendanceMonth(year, monthNumber),
          fetchAttendancePermissions(year, monthNumber, scope),
          fetchExtraOtRequests(year, monthNumber, scope),
          fetchDutyPlans(year, monthNumber, scope),
        ]);
        setDay(daily);
        setMonth(monthly);
        setPermissions(permissionResult.items ?? []);
        setExtraOt(otResult.items ?? []);
        setDutyPlans(dutyResult.items ?? []);
      }
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not load attendance", "error");
    } finally {
      if (showLoading) setLoading(false);
    }
  }, [employeeId, isTeamView, monthNumber, onToast, staff, today, user.id, user.role, year]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  useEffect(() => {
    const refresh = () => void load(false);
    const timer = window.setInterval(refresh, 30_000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, [load]);

  useEffect(() => {
    if (user.role === "admin") return;
    fetchAttendanceWeeklyOff()
      .then((policy) => {
        setWeeklyOff(policy.weekly_off_pattern);
        setSavedWeeklyOff(policy.weekly_off_pattern);
      })
      .catch(() => onToast("Could not load weekly-off preference", "error"));
  }, [onToast, user.role]);

  const saveWeeklyOff = async () => {
    setWeeklyOffBusy(true);
    try {
      const result = await updateAttendanceWeeklyOff(weeklyOff);
      setSavedWeeklyOff(result.weekly_off_pattern);
      onToast("Weekly off saved", "success");
      await load(false);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Weekly off could not be saved", "error");
    } finally {
      setWeeklyOffBusy(false);
    }
  };

  const punch = async (action: "check_in" | "check_out") => {
    setBusy(true);
    try {
      const result = await recordAttendancePunch(action);
      setDay(result.attendance);
      onToast(action === "check_in" ? "Check-in recorded" : "Check-out recorded", "success");
      await load();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Punch could not be recorded", "error");
    } finally {
      setBusy(false);
    }
  };

  const submitPermission = async () => {
    if (!reason.trim()) return onToast("Enter a reason for the permission", "info");
    setBusy(true);
    try {
      await requestAttendancePermission({
        attendance_date: permissionDate,
        kind,
        requested_minutes: TIMED_KINDS.includes(kind) ? Number(minutes) || 0 : 0,
        reason: reason.trim(),
      });
      setReason("");
      onToast(user.role === "manager" ? "Permission sent to the super admin" : "Permission sent to your manager", "success");
      await load();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Permission could not be submitted", "error");
    } finally {
      setBusy(false);
    }
  };

  const decidePermission = async (permission: AttendancePermission, approved: boolean) => {
    setBusy(true);
    try {
      await decideAttendancePermission(
        permission.id,
        approved,
        approved ? "Approved by administrator" : "Rejected by administrator",
      );
      onToast(approved ? "Permission approved" : "Permission rejected", "success");
      await load();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Decision could not be saved", "error");
    } finally {
      setBusy(false);
    }
  };

  const submitExtraOt = async () => {
    if (!otReason.trim()) return onToast("Enter a reason for the extra hours", "info");
    const requested = Number(otMinutes);
    if (!Number.isFinite(requested) || requested < 1) {
      return onToast("Enter how many extra minutes were worked", "info");
    }
    setBusy(true);
    try {
      await requestExtraOt({
        attendance_date: otDate,
        requested_minutes: Math.round(requested),
        reason: otReason.trim(),
      });
      setOtReason("");
      onToast(
        user.role === "manager"
          ? "Extra OT sent to the administrator"
          : "Extra OT sent to your manager",
        "success",
      );
      await load(false);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Extra OT could not be submitted", "error");
    } finally {
      setBusy(false);
    }
  };

  const decideOt = async (row: ExtraOtRequest, approved: boolean) => {
    setBusy(true);
    try {
      await decideExtraOt(
        row.id,
        approved,
        approved ? "Approved for payroll" : "Rejected",
      );
      onToast(approved ? "Extra OT approved" : "Extra OT rejected", "success");
      await load(false);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Decision could not be saved", "error");
    } finally {
      setBusy(false);
    }
  };

  const saveDutyPlan = async () => {
    if (!employeeId) return onToast("Select an employee first", "info");
    if (!dutyReason.trim()) return onToast("Enter why this day is being worked", "info");
    setBusy(true);
    try {
      await createDutyPlan({
        employee_id: employeeId,
        attendance_date: dutyDate,
        shift: {
          start: `${dutyStart}:00`,
          end: `${dutyEnd}:00`,
          break_minutes: Number(dutyBreak) || 0,
        },
        reason: dutyReason.trim(),
      });
      setDutyReason("");
      onToast("Duty planned. This date now counts as a working day.", "success");
      await load(false);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Duty plan could not be saved", "error");
    } finally {
      setBusy(false);
    }
  };

  const removeDutyPlan = async (plan: DutyPlan) => {
    setBusy(true);
    try {
      await deleteDutyPlan(plan.id);
      onToast("Duty plan removed. The date returns to the weekly-off pattern.", "success");
      await load(false);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Duty plan could not be removed", "error");
    } finally {
      setBusy(false);
    }
  };

  const visibleMonth = isTeamView ? adminMonths[employeeId] ?? null : month;
  const summary = monthSummary(visibleMonth);
  // Managers see the whole request inbox; the selected employee only controls
  // the detailed calendar below it.
  const selectedPermissions = permissions;
  const pendingCount = permissions.filter((permission) => permission.status === "pending").length;
  const pendingOt = extraOt.filter((row) => row.status === "pending").length;
  const approvedOtMinutes = extraOt
    .filter((row) => row.status === "approved")
    .reduce((sum, row) => sum + row.requested_minutes, 0);
  const employeeDutyPlans = isTeamView
    ? dutyPlans.filter((plan) => plan.employee_id === employeeId)
    : dutyPlans;
  const rosterSummaries = staff.map((person) => ({
    person,
    month: adminMonths[person.id],
    summary: monthSummary(adminMonths[person.id]),
    permissions: permissions.filter((permission) => permission.employee_id === person.id),
  }));
  const permissionUsed = visibleMonth?.totals.paid_permission_minutes ?? 0;
  const currentDay = visibleMonth?.days.find((row) => row.date === today) ?? day;

  const openEmployeeDetails = (selectedEmployeeId: string) => {
    setEmployeeId(selectedEmployeeId);
    window.setTimeout(() => {
      document.getElementById("attendance-employee-details")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }, 0);
  };

  return (
    <div className={`ds-page attendance-page ${isTeamView ? "is-manager-view" : "is-staff-view"}`}>
      <header className="ds-head attendance-hero">
        <div>
          <span className="attendance-kicker">{isTeamView ? "WORKFORCE OPERATIONS" : "WORKDAY"}</span>
          <h1 className="ds-head-title">{isTeamView ? "Staff attendance" : "My attendance"}</h1>
          <p className="ds-head-sub">
            {isTeamView
              ? "Monthly attendance, exceptions and permission requests for every active staff member"
              : "Your attendance percentage, punches and permission history"}
          </p>
        </div>
        <div className="attendance-toolbar">
          {user.role === "manager" && <div className="scope-switch" aria-label="Attendance view">
            <button type="button" className={viewMode === "mine" ? "is-active" : ""} onClick={() => setViewMode("mine")}>My attendance</button>
            <button type="button" className={viewMode === "team" ? "is-active" : ""} onClick={() => setViewMode("team")}>Team attendance</button>
          </div>}
          <label className="attendance-select">
            Month
            <input type="month" value={yearMonth} onChange={(event) => setYearMonth(event.target.value)} />
          </label>
          <button className="ds-ghost-btn" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={14} className={loading ? "icon-spin" : ""} /> Refresh
          </button>
        </div>
      </header>

      <div className="attendance-policy-strip" aria-label="Attendance policy summary">
        <span><Clock3 size={15} /><strong>8 hours</strong> payable work + 1-hour break</span>
        <span><CalendarDays size={15} /><strong>1 day</strong> paid leave</span>
        <span><ShieldCheck size={15} />Sunday or alternate-Friday weekly off</span>
        <span><WalletCards size={15} />Extra time deducted by minute</span>
      </div>

      {!isTeamView && user.role !== "admin" && <section className="attendance-weekly-off">
        <div><CalendarDays size={18} /><span><strong>Choose your weekly off</strong><small>This choice is used automatically in attendance and payroll.</small></span></div>
        <label>Weekly off<select value={weeklyOff} disabled={weeklyOffBusy} onChange={(event) => setWeeklyOff(event.target.value as WeeklyOffPattern)}><option value="sunday">Sunday</option><option value="alternate_friday">Alternate Friday (Sunday is working)</option></select></label>
        <button type="button" className="attendance-save-off" disabled={weeklyOffBusy || weeklyOff === savedWeeklyOff} onClick={() => void saveWeeklyOff()}>{weeklyOffBusy ? <RefreshCw size={14} className="icon-spin" /> : <CheckCircle2 size={14} />} Save weekly off</button>
      </section>}

      {isTeamView ? (
        <>
          <div className="ds-stats attendance-stats attendance-admin-stats">
            <Stat label="Active staff" value={String(staff.length)} note="Included in this month" icon={<Users size={16} />} />
            <Stat label="Pending permissions" value={String(pendingCount)} note="Awaiting an admin decision" icon={<ShieldCheck size={16} />} />
            <Stat label="Pending extra OT" value={String(pendingOt)} note="Only approved OT reaches payroll" icon={<Timer size={16} />} />
            <Stat label="Unpaid minutes" value={String(rosterSummaries.reduce((sum, row) => sum + (row.month?.totals.unpaid_minutes ?? 0), 0))} note="Across the active roster" icon={<WalletCards size={16} />} />
          </div>

          <section className="ds-panel attendance-roster">
            <div className="ds-panel-head">
              <div>
                <h2 className="ds-panel-title">Monthly staff overview</h2>
                <p className="ds-panel-sub">Select a staff member to inspect their daily record and permissions.</p>
              </div>
            </div>
            <div className="ds-table-wrap is-ruled">
              <table className="ds-table is-ruled">
                <thead><tr><th>Employee</th><th>Attendance</th><th>Working days</th><th>Exceptions</th><th>Salary-impact time</th><th /></tr></thead>
                <tbody>
                  {rosterSummaries.map(({ person, month: staffMonth, summary: staffSummary, permissions: staffPermissions }) => (
                    <tr key={person.id} className={employeeId === person.id ? "is-selected" : undefined}>
                      <td><span className="ds-who-text"><strong>{person.name}</strong><small>{person.staff_code || person.email}</small></span></td>
                      <td><div className="attendance-rate"><strong className="attendance-percentage">{staffSummary.percentage}%</strong><span><i style={{ width: `${Math.min(100, staffSummary.percentage)}%` }} /></span></div></td>
                      <td><strong>{staffSummary.attended} of {staffSummary.scheduled}</strong><small className="attendance-cell-note">Tracked from {staffMonth?.days[0]?.date || "first punch"}</small></td>
                      <td><div className="attendance-exception-list"><span>{staffSummary.absent} absent</span><span>{staffSummary.late} late</span><span>{staffSummary.missing} incomplete records</span><span>{staffPermissions.length} requests</span></div></td>
                      <td><strong className={(staffMonth?.totals.unpaid_minutes ?? 0) > 0 ? "attendance-unpaid" : ""}>{durationLabel(staffMonth?.totals.unpaid_minutes ?? 0)}</strong><small className="attendance-cell-note">After paid allowance</small></td>
                      <td><button type="button" className="attendance-view-btn" onClick={() => openEmployeeDetails(person.id)}>Open details</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : (
        <div className="ds-stats attendance-stats attendance-self-stats">
          <Stat label="Attendance" value={`${summary.percentage}%`} note={`${summary.attended} of ${summary.scheduled} working days`} icon={<Percent size={16} />} />
          <Stat label="Today" value={statusLabel(currentDay)} note={today} icon={<CalendarDays size={16} />} />
          <Stat label="Check in" value={timeOf(currentDay?.check_in)} note="Server-recorded time" icon={<LogIn size={16} />} />
          <Stat label="Permission used" value={`${permissionUsed} / 60 min`} note={`${month?.totals.permission_occasions ?? 0} of 2 occasions`} icon={<ShieldCheck size={16} />} />
          <Stat label="Unpaid" value={`${month?.totals.unpaid_minutes ?? 0} min`} note="Actual absence time only" icon={<WalletCards size={16} />} />
        </div>
      )}

      {!isTeamView && (
        <div className="attendance-grid">
          <section className="ds-panel">
            <div className="ds-panel-head"><div><h2 className="ds-panel-title">Today’s attendance</h2><p className="ds-panel-sub">Default shift 10:00 AM–7:00 PM, including a one-hour break: 480 payable minutes.</p></div><Clock3 size={20} /></div>
            <div className="attendance-minutes"><span><strong>{durationLabel(workedMinutes(currentDay))}</strong> worked today</span><span><strong>{durationLabel(currentDay?.unpaid_minutes ?? 0)}</strong> salary-impact time</span></div>
            <div className="attendance-actions">
              <button type="button" className="ds-primary-btn" disabled={busy || Boolean(currentDay?.check_in)} onClick={() => void punch("check_in")}><LogIn size={15} /> Check in</button>
              <button type="button" className="ds-ghost-btn" disabled={busy || !currentDay?.check_in || Boolean(currentDay?.check_out)} onClick={() => void punch("check_out")}><LogOut size={15} /> Check out</button>
            </div>
          </section>

          <section className="ds-panel">
            <div className="ds-panel-head"><div><h2 className="ds-panel-title">Request permission</h2><p className="ds-panel-sub">{user.role === "manager" ? "Your request will go to the super admin for approval." : "Your request will go to your manager for approval."}</p></div></div>
            <div className="attendance-form">
              <label>Date<input type="date" value={permissionDate} onChange={(event) => setPermissionDate(event.target.value)} /></label>
              <label>Type<select value={kind} onChange={(event) => setKind(event.target.value as AttendancePermission["kind"])}><option value="late">Late arrival</option><option value="early_check_in">Early check-in</option><option value="early_exit">Early leaving</option><option value="official_duty">Official duty</option><option value="work_from_home">Work from home</option><option value="paid_leave">Paid leave / holiday</option><option value="unpaid_leave">Unpaid leave</option></select></label>
              {TIMED_KINDS.includes(kind) && <label>{kind === "early_check_in" ? "Minutes early" : "Minutes"}<input type="number" min="1" max="1440" value={minutes} onChange={(event) => setMinutes(event.target.value)} /></label>}
              {BEFORE_SHIFT_KINDS.includes(kind) && (
                <p className="attendance-form-note">
                  {kind === "early_check_in"
                    ? "Send this before your shift starts. Without an approved early check-in the system will not accept an early punch."
                    : "This must be requested before your shift starts."}
                </p>
              )}
              <label className="is-wide">Reason<textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why is this permission needed?" /></label>
              <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void submitPermission()}>Send for approval</button>
            </div>
          </section>

          <section className="ds-panel">
            <div className="ds-panel-head">
              <div>
                <h2 className="ds-panel-title">Request extra OT</h2>
                <p className="ds-panel-sub">
                  {user.role === "manager"
                    ? "Hours worked beyond your shift. Approved by an administrator."
                    : "Hours worked beyond your shift. Approved by your manager."}
                </p>
              </div>
              <Timer size={20} />
            </div>
            <div className="attendance-form">
              <label>Date<input type="date" value={otDate} onChange={(event) => setOtDate(event.target.value)} /></label>
              <label>Extra minutes<input type="number" min="1" max="1440" value={otMinutes} onChange={(event) => setOtMinutes(event.target.value)} /></label>
              <label className="is-wide">Reason<textarea rows={3} value={otReason} onChange={(event) => setOtReason(event.target.value)} placeholder="What was worked beyond the normal shift?" /></label>
              <p className="attendance-form-note">
                Only approved extra OT is paid. Pending and rejected requests do not affect payroll.
              </p>
              <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void submitExtraOt()}>Send for approval</button>
            </div>
          </section>
        </div>
      )}

      <PermissionTable
        permissions={selectedPermissions}
        staff={staff}
        admin={isTeamView}
        busy={busy}
        onDecision={decidePermission}
        months={adminMonths}
      />

      <ExtraOtSection
        requests={extraOt}
        staff={staff}
        admin={isTeamView}
        busy={busy}
        approvedMinutes={approvedOtMinutes}
        onDecision={decideOt}
      />

      {isTeamView && (
        <DutyPlanSection
          plans={employeeDutyPlans}
          employeeName={selectedStaff?.name}
          busy={busy}
          date={dutyDate}
          start={dutyStart}
          end={dutyEnd}
          breakMinutes={dutyBreak}
          reason={dutyReason}
          onDateChange={setDutyDate}
          onStartChange={setDutyStart}
          onEndChange={setDutyEnd}
          onBreakChange={setDutyBreak}
          onReasonChange={setDutyReason}
          onSave={saveDutyPlan}
          onRemove={removeDutyPlan}
        />
      )}

      <MonthTable month={visibleMonth} title={isTeamView && selectedStaff ? `${selectedStaff.name} · daily attendance` : "This month"} sectionId={isTeamView ? "attendance-employee-details" : undefined} />
    </div>
  );
}

function Stat({ label, value, note, icon }: { label: string; value: string; note: string; icon: ReactNode }) {
  return <div className="ds-stat is-static"><span className="ds-stat-top"><span className="ds-stat-label">{label}</span>{icon}</span><span className="ds-stat-value">{value}</span><span className="ds-stat-foot">{note}</span></div>;
}

function PermissionTable({ permissions, staff, admin, busy, onDecision, months }: {
  permissions: AttendancePermission[];
  staff: StaffMember[];
  admin: boolean;
  busy: boolean;
  onDecision: (permission: AttendancePermission, approved: boolean) => Promise<void>;
  months: Record<string, AttendanceMonth>;
}) {
  const nameOf = (employeeId: string) => staff.find((person) => person.id === employeeId)?.name || employeeId;
  const ordered = [...permissions].sort((left, right) => {
    if (left.status === right.status) return right.attendance_date.localeCompare(left.attendance_date);
    return left.status === "pending" ? -1 : 1;
  });

  if (admin) {
    const pending = permissions.filter((permission) => permission.status === "pending").length;
    return (
      <section className="ds-panel attendance-permissions attendance-request-center">
        <div className="ds-panel-head attendance-request-head">
          <div>
            <span className="attendance-section-kicker">MANAGER ACTION CENTRE</span>
            <h2 className="ds-panel-title">Permission requests</h2>
            <p className="ds-panel-sub">Review monthly leave and hour usage before deciding.</p>
          </div>
          <span className={`attendance-request-count ${pending ? "has-pending" : ""}`}>{pending} awaiting review</span>
        </div>
        {ordered.length === 0 ? <div className="attendance-requests-clear"><CheckCircle2 size={18} /><span><strong>All caught up</strong>No permission requests need a decision.</span></div> : (
          <div className="attendance-request-grid">
            {ordered.map((permission) => {
              const employeeMonth = months[permission.employee_id];
              const leaveDays = employeeMonth?.days.filter((day) => day.status === "PL" || day.status === "UL").length ?? 0;
              const permissionMinutes = employeeMonth?.totals.approved_permission_minutes ?? 0;
              return (
                <article className={`attendance-request-card is-${permission.status}`} key={permission.id}>
                  <header>
                    <div className="attendance-request-person">
                      <span className="attendance-avatar">{nameOf(permission.employee_id).slice(0, 1).toUpperCase()}</span>
                      <div><strong>{nameOf(permission.employee_id)}</strong><small>{permission.attendance_date} · {PERMISSION_KIND[permission.kind]}</small></div>
                    </div>
                    <span className={`ds-status ${statusTone(permission.status)}`}><i />{permission.status}</span>
                  </header>
                  <p className="attendance-request-reason">{permission.reason}</p>
                  <div className="attendance-request-usage">
                    <div><span>Requested</span><strong>{permission.requested_minutes ? `${permission.requested_minutes} min` : "Full day"}</strong></div>
                    <div><span>Leave this month</span><strong>{leaveDays} day(s)</strong></div>
                    <div><span>Hours approved</span><strong>{permissionMinutes} min</strong></div>
                  </div>
                  {permission.decision_reason && <p className="attendance-decision-reason">Decision note: {permission.decision_reason}</p>}
                  <footer>{permission.status === "pending" ? <div className="attendance-decision-actions"><button type="button" className="attendance-approve-btn" disabled={busy} onClick={() => void onDecision(permission, true)}><CheckCircle2 size={15} /> Approve request</button><button type="button" className="attendance-reject-btn" disabled={busy} onClick={() => void onDecision(permission, false)}><XCircle size={15} /> Reject</button></div> : <span>Reviewed request</span>}</footer>
                </article>
              );
            })}
          </div>
        )}
      </section>
    );
  }

  return (
    <section className="ds-panel attendance-permissions">
      <div className="ds-panel-head"><div><h2 className="ds-panel-title">My permissions</h2><p className="ds-panel-sub">Request date, approval status and the recorded reason.</p></div></div>
      {permissions.length === 0 ? <div className="ds-empty-state"><ShieldCheck size={28} /><h3>No permission requests this month</h3></div> : (
        <div className="ds-table-wrap is-ruled"><table className="ds-table is-ruled"><thead><tr><th>Date</th><th>Type</th><th>Minutes</th><th>Reason</th><th>Status</th></tr></thead><tbody>
          {permissions.map((permission) => <tr key={permission.id}>
            <td>{permission.attendance_date}</td><td>{PERMISSION_KIND[permission.kind]}</td><td>{permission.requested_minutes || "Full day"}</td>
            <td><span>{permission.reason}</span>{permission.decision_reason && <small className="attendance-decision-reason">{permission.decision_reason}</small>}</td>
            <td><span className={`ds-status ${statusTone(permission.status)}`}><i />{permission.status}</span></td>
          </tr>)}
        </tbody></table></div>
      )}
    </section>
  );
}

/**
 * Extra OT, both halves of it.
 *
 * Managers get the approval queue; everybody sees their own history with the
 * requested duration, the decision and who made it. The approved total is shown
 * beside it because that — and only that — is what payroll pays.
 */
function ExtraOtSection({ requests, staff, admin, busy, approvedMinutes, onDecision }: {
  requests: ExtraOtRequest[];
  staff: StaffMember[];
  admin: boolean;
  busy: boolean;
  approvedMinutes: number;
  onDecision: (row: ExtraOtRequest, approved: boolean) => Promise<void>;
}) {
  const nameOf = (employeeId: string) => staff.find((person) => person.id === employeeId)?.name || employeeId;
  const ordered = [...requests].sort((left, right) => {
    if (left.status === right.status) return right.attendance_date.localeCompare(left.attendance_date);
    return left.status === "pending" ? -1 : 1;
  });
  const pending = requests.filter((row) => row.status === "pending").length;

  return (
    <section className="ds-panel attendance-permissions">
      <div className="ds-panel-head">
        <div>
          {admin && <span className="attendance-section-kicker">MANAGER ACTION CENTRE</span>}
          <h2 className="ds-panel-title">{admin ? "Extra OT requests" : "My extra OT"}</h2>
          <p className="ds-panel-sub">
            {durationLabel(approvedMinutes)} approved this month — the only extra OT payroll pays.
          </p>
        </div>
        {admin && (
          <span className={`attendance-request-count ${pending ? "has-pending" : ""}`}>
            {pending} awaiting review
          </span>
        )}
      </div>

      {ordered.length === 0 ? (
        <div className="ds-empty-state">
          <Timer size={28} />
          <h3>No extra OT this month</h3>
          <p>Hours worked beyond a normal shift appear here once requested.</p>
        </div>
      ) : (
        <div className="ds-table-wrap is-ruled">
          <table className="ds-table is-ruled">
            <thead>
              <tr>
                <th>Date</th>
                {admin && <th>Requester</th>}
                <th>Requested</th>
                <th>Approved</th>
                <th>Reason</th>
                <th>Status</th>
                <th>Decision</th>
                {admin && <th />}
              </tr>
            </thead>
            <tbody>
              {ordered.map((row) => (
                <tr key={row.id}>
                  <td><strong>{row.attendance_date}</strong></td>
                  {admin && <td>{nameOf(row.employee_id)}</td>}
                  <td>{durationLabel(row.requested_minutes)}</td>
                  {/* Approved duration is the requested duration once approved,
                      and nothing at all until then — a pending request must
                      never read as though it were already counted. */}
                  <td>
                    <strong className={row.status === "approved" ? "" : "attendance-no-impact"}>
                      {row.status === "approved" ? durationLabel(row.requested_minutes) : "—"}
                    </strong>
                  </td>
                  <td>{row.reason}</td>
                  <td><span className={`ds-status ${statusTone(row.status)}`}><i />{row.status}</span></td>
                  <td>
                    {row.decided_at ? (
                      <span className="ds-who-text">
                        <strong>{row.decision_reason || "Reviewed"}</strong>
                        <small>{new Date(row.decided_at).toLocaleString("en-IN")}</small>
                      </span>
                    ) : (
                      <small className="attendance-cell-note">Awaiting a decision</small>
                    )}
                  </td>
                  {admin && (
                    <td>
                      {row.status === "pending" ? (
                        <div className="attendance-decision-actions">
                          <button type="button" className="attendance-approve-btn" disabled={busy} onClick={() => void onDecision(row, true)}>
                            <CheckCircle2 size={15} /> Approve
                          </button>
                          <button type="button" className="attendance-reject-btn" disabled={busy} onClick={() => void onDecision(row, false)}>
                            <XCircle size={15} /> Reject
                          </button>
                        </div>
                      ) : (
                        <small className="attendance-cell-note">Reviewed</small>
                      )}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/**
 * Planned duty: rostering one date as a working day.
 *
 * This is what makes a Sunday count. It is deliberately per-date — a rolling
 * shift change sets an employee's hours from a date onwards and says nothing
 * about which days are worked, and conflating the two used to turn every Sunday
 * after the first rostered one into an absence.
 */
function DutyPlanSection({
  plans, employeeName, busy, date, start, end, breakMinutes, reason,
  onDateChange, onStartChange, onEndChange, onBreakChange, onReasonChange, onSave, onRemove,
}: {
  plans: DutyPlan[];
  employeeName?: string;
  busy: boolean;
  date: string;
  start: string;
  end: string;
  breakMinutes: string;
  reason: string;
  onDateChange: (value: string) => void;
  onStartChange: (value: string) => void;
  onEndChange: (value: string) => void;
  onBreakChange: (value: string) => void;
  onReasonChange: (value: string) => void;
  onSave: () => Promise<void>;
  onRemove: (plan: DutyPlan) => Promise<void>;
}) {
  const weekday = (value: string) =>
    new Date(`${value}T00:00:00`).toLocaleDateString("en-IN", { weekday: "long" });

  return (
    <section className="ds-panel attendance-permissions">
      <div className="ds-panel-head">
        <div>
          <span className="attendance-section-kicker">ROSTER</span>
          <h2 className="ds-panel-title">
            Planned duty{employeeName ? ` · ${employeeName}` : ""}
          </h2>
          <p className="ds-panel-sub">
            Roster a weekly off as a working day. The employee can then check in and the day is
            calculated and paid normally.
          </p>
        </div>
        <CalendarDays size={20} />
      </div>

      <div className="attendance-form">
        <label>Date<input type="date" value={date} onChange={(event) => onDateChange(event.target.value)} /></label>
        <label>Shift start<input type="time" value={start} onChange={(event) => onStartChange(event.target.value)} /></label>
        <label>Shift end<input type="time" value={end} onChange={(event) => onEndChange(event.target.value)} /></label>
        <label>Break (minutes)<input type="number" min="0" max="480" value={breakMinutes} onChange={(event) => onBreakChange(event.target.value)} /></label>
        <label className="is-wide">Reason<textarea rows={2} value={reason} onChange={(event) => onReasonChange(event.target.value)} placeholder="Why is this day being worked?" /></label>
        <p className="attendance-form-note">
          {date ? `${weekday(date)} — ` : ""}only this date is affected. Other weekly offs are unchanged.
        </p>
        <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void onSave()}>
          <CalendarDays size={15} /> Plan this duty
        </button>
      </div>

      {plans.length === 0 ? (
        <div className="ds-empty-state">
          <CalendarDays size={28} />
          <h3>No planned duty this month</h3>
          <p>Weekly offs are following the employee&rsquo;s normal pattern.</p>
        </div>
      ) : (
        <div className="ds-table-wrap is-ruled">
          <table className="ds-table is-ruled">
            <thead><tr><th>Date</th><th>Day</th><th>Shift</th><th>Reason</th><th /></tr></thead>
            <tbody>
              {plans.map((plan) => (
                <tr key={plan.id}>
                  <td><strong>{plan.effective_from}</strong></td>
                  <td>{weekday(plan.effective_from)}</td>
                  <td>
                    {String(plan.shift?.start ?? "").slice(0, 5)}–{String(plan.shift?.end ?? "").slice(0, 5)}
                    <small className="attendance-cell-note">{plan.shift?.break_minutes ?? 0} min break</small>
                  </td>
                  <td>{plan.reason}</td>
                  <td>
                    <button type="button" className="attendance-reject-btn" disabled={busy} onClick={() => void onRemove(plan)}>
                      <Trash2 size={15} /> Remove
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function MonthTable({ month, title, sectionId }: { month: AttendanceMonth | null; title: string; sectionId?: string }) {
  return (
    <section className="ds-panel attendance-ledger" id={sectionId}>
      <div className="ds-panel-head"><div><span className="attendance-section-kicker">DAILY CALCULATION</span><h2 className="ds-panel-title">{title}</h2><p className="ds-panel-sub">Required duty, time actually covered, and what is left uncovered after permission and grace.</p></div></div>
      {!month?.days.length ? <div className="ds-empty-state"><CalendarDays size={28} /><h3>Attendance tracking has not started</h3><p>The calendar begins from this employee’s first punch, leave or permission.</p></div> : <div className="ds-table-wrap is-ruled"><table className="ds-table is-ruled"><thead><tr><th>Date</th><th>Result</th><th>Work session</th><th>Required</th><th>Uncovered</th><th>Paid allowance</th><th>Salary impact</th></tr></thead><tbody>
        {[...month.days].reverse().map((row) => {
          const work = workedMinutes(row);
          const unpaid = row.unpaid_minutes ?? 0;
          // Every figure below comes from the server's own calculation. The
          // browser displays the equation; it does not evaluate it.
          const required = row.required_shift_minutes ?? 0;
          const uncovered = row.uncovered_minutes ?? 0;
          const fullDayAbsence = row.status === "A" || row.status === "UL";
          return <tr key={row.date}>
            <td><strong>{row.date}</strong></td>
            <td><span className={`ds-status ${row.status === "A" || row.status === "UL" ? "is-bad" : (row.status === "MP" && !(row.provisional && row.check_in)) || row.status === "LT" || row.status === "EE" ? "is-warn" : "is-info"}`}><i />{statusLabel(row)}</span></td>
            <td><strong>{row.check_in && !row.check_out ? "In progress" : row.check_in ? durationLabel(work) : "No work session"}</strong><small className="attendance-cell-note">{row.check_in ? `${timeOf(row.check_in)} → ${row.check_out ? timeOf(row.check_out) : "still checked in"}` : "No punches recorded"}</small></td>
            <td><strong>{required ? durationLabel(required) : "—"}</strong><small className="attendance-cell-note">{required ? "Shift less break" : "No duty"}</small></td>
            {/* Zero here is the normal, correct answer for a day that was
                fully covered — including one where a late start was made up by
                a late finish. It is stated rather than left blank. */}
            <td><strong className={uncovered ? "attendance-unpaid" : "attendance-no-impact"}>{uncovered ? durationLabel(uncovered) : "0 min"}</strong><small className="attendance-cell-note">{uncovered ? "Short of the required duty" : "Requirement met"}</small></td>
            <td><strong>{durationLabel(row.paid_permission_minutes ?? 0)}</strong><small className="attendance-cell-note">Grace or approved permission</small></td>
            <td><strong className={unpaid ? "attendance-unpaid" : "attendance-no-impact"}>{unpaid ? durationLabel(unpaid) : "No deduction"}</strong><small className="attendance-cell-note">{unpaid ? (fullDayAbsence ? "Full-day absence" : "Uncovered shift time") : "Fully covered"}</small></td>
          </tr>;
        })}
      </tbody></table></div>}
    </section>
  );
}
