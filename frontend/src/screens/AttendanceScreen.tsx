"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  ClipboardList,
  Clock3,
  History,
  LayoutGrid,
  LogIn,
  Percent,
  RefreshCw,
  Send,
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
  type WeeklyOffDay,
  type WeeklyOffWeek,
  fetchCoverColleagues,
  fetchCoverRequests,
  nominateLeaveCover,
  respondToCoverRequest,
  type CoverColleague,
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
  weekly_off: "Weekly off (rotational)",
};

/**
 * Permission kinds measured in minutes rather than taken as a whole day.
 *
 * `early_check_in` belongs here: the minutes are how early the employee expects
 * to arrive, and the backend credits exactly that much presence before the
 * shift start.
 */
const TIMED_KINDS: AttendancePermission["kind"][] = ["late", "early_exit", "early_check_in"];
/** Whole-day leave, which may name a colleague to cover the day's work. */
const LEAVE_KINDS: AttendancePermission["kind"][] = ["paid_leave", "unpaid_leave"];

const COVER_STATUS: Record<string, string> = {
  requested: "Waiting for reply",
  accepted: "Accepted",
  declined: "Declined",
};

function coverOutcome(permission: AttendancePermission): string | null {
  const handover = permission.cover_handover;
  if (!handover) return null;
  if (handover.status === "active") return `${handover.handed_over ?? 0} candidate(s) with the cover today`;
  if (handover.status === "settled") {
    return `${handover.completed ?? 0} completed by the cover · ${handover.returned ?? 0} returned`;
  }
  return handover.reason || "Handover skipped";
}

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

function shortDate(isoDate: string): string {
  return new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short" }).format(new Date(`${isoDate}T00:00:00`));
}

function deadlineLabel(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit", timeZone: "Asia/Kolkata",
  }).format(new Date(iso));
}

function requestStatusLabel(status: string): string {
  return status === "awaiting_cover" ? "waiting for cover" : status;
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
  const [coverColleagues, setCoverColleagues] = useState<CoverColleague[]>([]);
  const [coverId, setCoverId] = useState("");
  const [coverRequests, setCoverRequests] = useState<AttendancePermission[]>([]);
  const [viewMode, setViewMode] = useState<"team" | "mine">("team");
  const [weeklyOffWeeks, setWeeklyOffWeeks] = useState<WeeklyOffWeek[]>([]);
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
  const [tab, setTab] = useState("overview");
  const [requestType, setRequestType] = useState<"permission" | "ot">("permission");

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
    let active = true;
    fetchCoverColleagues()
      .then(({ items }) => { if (active) setCoverColleagues(items ?? []); })
      .catch(() => { if (active) setCoverColleagues([]); });
    return () => {
      active = false;
    };
  }, [user.role]);

  const loadCoverRequests = useCallback(async () => {
    if (user.role === "admin") return;
    try {
      const { items } = await fetchCoverRequests();
      setCoverRequests(items ?? []);
    } catch {
      // Not fatal: the rest of attendance still works without this panel.
    }
  }, [user.role]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadCoverRequests(), 0);
    const interval = window.setInterval(() => void loadCoverRequests(), 60_000);
    return () => {
      window.clearTimeout(timer);
      window.clearInterval(interval);
    };
  }, [loadCoverRequests]);

  const answerCover = async (permission: AttendancePermission, accepted: boolean) => {
    setBusy(true);
    try {
      await respondToCoverRequest(permission.id, accepted);
      onToast(accepted ? `You will cover ${permission.employee_name || "your colleague"}'s work` : "Cover request declined", accepted ? "success" : "info");
      await loadCoverRequests();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not answer the cover request", "error");
    } finally {
      setBusy(false);
    }
  };

  const askAnotherCover = async (permission: AttendancePermission, colleagueId: string) => {
    if (!colleagueId) return;
    setBusy(true);
    try {
      await nominateLeaveCover(permission.id, colleagueId);
      onToast("Cover request sent", "success");
      await load(false);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not send the cover request", "error");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (user.role === "admin") return;
    fetchAttendanceWeeklyOff()
      .then((result) => setWeeklyOffWeeks(result.weeks ?? []))
      .catch(() => onToast("Could not load weekly off", "error"));
  }, [onToast, user.role]);

  const saveWeeklyOff = async (week: WeeklyOffWeek, choice: WeeklyOffDay) => {
    if (week.locked || week.day === choice) return;
    setWeeklyOffBusy(true);
    try {
      const result = await updateAttendanceWeeklyOff(week.week_start, choice);
      setWeeklyOffWeeks((weeks) => weeks.map((row) => (row.week_start === week.week_start ? result.week : row)));
      onToast(
        choice === "friday"
          ? `Friday ${shortDate(week.friday)} is your weekly off; Sunday ${shortDate(week.sunday)} is a working day`
          : `Sunday ${shortDate(week.sunday)} is your weekly off`,
        "success",
      );
      await load(false);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Weekly off could not be saved", "error");
    } finally {
      setWeeklyOffBusy(false);
    }
  };

  const submitPermission = async () => {
    if (!reason.trim()) return onToast("Enter a reason for the permission", "info");
    const needsCover = LEAVE_KINDS.includes(kind) && coverColleagues.length > 0;
    if (needsCover && !coverId) return onToast("Choose a colleague to handle your work that day", "info");
    const coverName = coverColleagues.find((colleague) => colleague.id === coverId)?.name;
    setBusy(true);
    try {
      await requestAttendancePermission({
        attendance_date: permissionDate,
        kind,
        requested_minutes: TIMED_KINDS.includes(kind) ? Number(minutes) || 0 : 0,
        reason: reason.trim(),
        cover_employee_id: LEAVE_KINDS.includes(kind) && coverId ? coverId : undefined,
      });
      setReason("");
      setCoverId("");
      const approver = user.role === "manager" ? "the super admin" : "your manager";
      onToast(
        needsCover
          ? `Sent to ${coverName || "your colleague"} first. It goes to ${approver} once they accept.`
          : `Permission sent to ${approver}`,
        "success",
      );
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
  // A leave still waiting on its cover has not reached the approver yet.
  const selectedPermissions = useMemo(
    () => isTeamView ? permissions.filter((permission) => permission.status !== "awaiting_cover") : permissions,
    [isTeamView, permissions],
  );
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
    setTab("employee");
  };

  const tabs: PageTab[] = isTeamView
    ? [
      { id: "overview", label: "Team overview", icon: <Users size={15} /> },
      { id: "approvals", label: "Approvals", icon: <ShieldCheck size={15} />, count: pendingCount + pendingOt, alert: pendingCount + pendingOt > 0 },
      { id: "employee", label: "Employee record", icon: <CalendarDays size={15} /> },
      { id: "roster", label: "Duty roster", icon: <ClipboardList size={15} />, count: dutyPlans.length || undefined },
    ]
    : [
      { id: "overview", label: "Overview", icon: <LayoutGrid size={15} /> },
      { id: "request", label: "New request", icon: <Send size={15} /> },
      { id: "history", label: "My requests", icon: <History size={15} />, count: (permissions.length + extraOt.length) || undefined },
      ...(user.role !== "admin" ? [{ id: "weekly", label: "Weekly off", icon: <CalendarDays size={15} /> }] : []),
      ...(coverRequests.length > 0 ? [{ id: "cover", label: "Cover requests", icon: <Users size={15} />, count: coverRequests.length, alert: coverRequests.some((request) => request.cover_status === "requested") }] : []),
    ];
  const activeTab = tabs.some((item) => item.id === tab) ? tab : "overview";
  const selectedUnpaid = visibleMonth?.totals.unpaid_minutes ?? 0;

  const employeePicker = (
    <label className="attendance-select attendance-employee-picker">
      Employee
      <select value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}>
        {staff.map((person) => <option key={person.id} value={person.id}>{person.name}{person.staff_code ? ` · ${person.staff_code}` : ""}</option>)}
      </select>
    </label>
  );

  return (
    <div className={`ds-page attendance-page ${isTeamView ? "is-manager-view" : "is-staff-view"}`}>
      <header className="ds-head attendance-hero">
        <div>
          <span className="attendance-kicker">{isTeamView ? "WORKFORCE OPERATIONS" : "WORKDAY"}</span>
          <h1 className="ds-head-title">{isTeamView ? "Staff attendance" : "My attendance"}</h1>
          <p className="ds-head-sub">
            {isTeamView
              ? "Monthly attendance, exceptions and approvals for every active staff member"
              : "Your attendance, requests and daily record for the month"}
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

      {isTeamView ? (
        <div className="ds-stats attendance-stats attendance-admin-stats">
          <Stat label="Active staff" value={String(staff.length)} note="Included in this month" icon={<Users size={16} />} />
          <Stat label="Pending permissions" value={String(pendingCount)} note="Awaiting a decision" icon={<ShieldCheck size={16} />} />
          <Stat label="Pending extra OT" value={String(pendingOt)} note="Approved OT only offsets late time" icon={<Timer size={16} />} />
          <Stat label="Unpaid time" value={durationLabel(rosterSummaries.reduce((sum, row) => sum + (row.month?.totals.unpaid_minutes ?? 0), 0))} note="Across the active roster" icon={<WalletCards size={16} />} />
        </div>
      ) : (
        <div className="ds-stats attendance-stats attendance-self-stats">
          <Stat label="Attendance" value={`${summary.percentage}%`} note={`${summary.attended} of ${summary.scheduled} working days`} icon={<Percent size={16} />} />
          <Stat label="Today" value={statusLabel(currentDay)} note={today} icon={<CalendarDays size={16} />} />
          <Stat label="Check in" value={timeOf(currentDay?.check_in)} note="Server-recorded time" icon={<LogIn size={16} />} />
          <Stat label="Permission used" value={`${permissionUsed} / 60 min`} note={`${month?.totals.permission_occasions ?? 0} of 2 occasions`} icon={<ShieldCheck size={16} />} />
          <Stat label="Unpaid" value={durationLabel(month?.totals.unpaid_minutes ?? 0)} note="Actual absence time only" icon={<WalletCards size={16} />} />
        </div>
      )}

      <PageTabs tabs={tabs} active={activeTab} onChange={setTab} label="Attendance sections" />

      <div className="attendance-tab-panel" role="tabpanel">
        {isTeamView && activeTab === "overview" && (
          <>
            <PolicyStrip />
            <section className="ds-panel attendance-roster">
              <div className="ds-panel-head">
                <div>
                  <h2 className="ds-panel-title">Monthly staff overview</h2>
                  <p className="ds-panel-sub">Open an employee to see their daily record.</p>
                </div>
                <span className="attendance-request-count">{staff.length} staff</span>
              </div>
              <div className="ds-table-wrap is-ruled">
                <table className="ds-table is-ruled">
                  <thead><tr><th>Employee</th><th>Attendance</th><th>Working days</th><th>Exceptions</th><th>Unpaid time</th><th /></tr></thead>
                  <tbody>
                    {rosterSummaries.map(({ person, month: staffMonth, summary: staffSummary, permissions: staffPermissions }) => (
                      <tr key={person.id} className={employeeId === person.id ? "is-selected" : undefined}>
                        <td><span className="ds-who-text"><strong>{person.name}</strong><small>{person.staff_code || person.email}</small></span></td>
                        <td><div className="attendance-rate"><strong className="attendance-percentage">{staffSummary.percentage}%</strong><span><i style={{ width: `${Math.min(100, staffSummary.percentage)}%` }} /></span></div></td>
                        <td><strong>{staffSummary.attended} of {staffSummary.scheduled}</strong><small className="attendance-cell-note">Tracked from {staffMonth?.days[0]?.date || "first punch"}</small></td>
                        <td><div className="attendance-exception-list"><span>{staffSummary.absent} absent</span><span>{staffSummary.late} late</span><span>{staffSummary.missing} incomplete</span><span>{staffPermissions.length} requests</span></div></td>
                        <td><strong className={(staffMonth?.totals.unpaid_minutes ?? 0) > 0 ? "attendance-unpaid" : ""}>{durationLabel(staffMonth?.totals.unpaid_minutes ?? 0)}</strong><small className="attendance-cell-note">After paid allowance</small></td>
                        <td><button type="button" className="attendance-view-btn" onClick={() => openEmployeeDetails(person.id)}>View record <ChevronRight size={14} /></button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
          </>
        )}

        {isTeamView && activeTab === "approvals" && (
          <>
            <PermissionTable
              permissions={selectedPermissions}
              staff={staff}
              admin
              busy={busy}
              onDecision={decidePermission}
              months={adminMonths}
            />
            <ExtraOtSection
              requests={extraOt}
              staff={staff}
              admin
              busy={busy}
              approvedMinutes={approvedOtMinutes}
              onDecision={decideOt}
            />
          </>
        )}

        {isTeamView && activeTab === "employee" && (
          <>
            <section className="ds-panel attendance-selected-head">
              <div className="ds-panel-head">
                <div>
                  <h2 className="ds-panel-title">{selectedStaff?.name || "Select an employee"}</h2>
                  <p className="ds-panel-sub">{selectedStaff?.staff_code || selectedStaff?.email || "Choose whose month to inspect."}</p>
                </div>
                {employeePicker}
              </div>
              <div className="attendance-selected-summary">
                <span><small>Attendance</small><strong>{summary.percentage}%</strong></span>
                <span><small>Days attended</small><strong>{summary.attended} of {summary.scheduled}</strong></span>
                <span><small>Absent · late · incomplete</small><strong>{summary.absent} · {summary.late} · {summary.missing}</strong></span>
                <span><small>Unpaid time</small><strong className={selectedUnpaid ? "attendance-unpaid" : ""}>{durationLabel(selectedUnpaid)}</strong></span>
              </div>
            </section>
            <MonthTable month={visibleMonth} title="Daily attendance" />
          </>
        )}

        {isTeamView && activeTab === "roster" && (
          <DutyPlanSection
            plans={employeeDutyPlans}
            employeeName={selectedStaff?.name}
            picker={employeePicker}
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

        {!isTeamView && activeTab === "overview" && (
          <MonthTable month={visibleMonth} title="This month" />
        )}

        {!isTeamView && activeTab === "request" && (
          <div className="attendance-request-layout">
            <section className="ds-panel">
              <div className="ds-panel-head">
                <div>
                  <h2 className="ds-panel-title">{requestType === "permission" ? "Request permission" : "Request extra OT"}</h2>
                  <p className="ds-panel-sub">
                    Goes to {user.role === "manager" ? "the super admin" : "your manager"} for approval.
                  </p>
                </div>
                <div className="scope-switch" role="group" aria-label="Request type">
                  <button type="button" className={requestType === "permission" ? "is-active" : ""} aria-pressed={requestType === "permission"} onClick={() => setRequestType("permission")}>Permission / leave</button>
                  <button type="button" className={requestType === "ot" ? "is-active" : ""} aria-pressed={requestType === "ot"} onClick={() => setRequestType("ot")}>Extra OT</button>
                </div>
              </div>

              {requestType === "permission" ? (
                <div className="attendance-form">
                  <label>Date<input type="date" value={permissionDate} onChange={(event) => setPermissionDate(event.target.value)} /></label>
                  <label>Type<select value={kind} onChange={(event) => setKind(event.target.value as AttendancePermission["kind"])}><option value="late">Late arrival</option><option value="early_check_in">Early check-in</option><option value="early_exit">Early leaving</option><option value="official_duty">Official duty</option><option value="work_from_home">Work from home</option><option value="paid_leave">Paid leave / holiday</option><option value="unpaid_leave">Unpaid leave</option><option value="weekly_off">Weekly off (rotational)</option></select></label>
                  {TIMED_KINDS.includes(kind) && <label>{kind === "early_check_in" ? "Minutes early" : "Minutes"}<input type="number" min="1" max="1440" value={minutes} onChange={(event) => setMinutes(event.target.value)} /></label>}
                  {BEFORE_SHIFT_KINDS.includes(kind) && (
                    <p className="attendance-form-note">
                      {kind === "early_check_in"
                        ? "Send this before your shift starts. Without an approved early check-in the system will not accept an early punch."
                        : "This must be requested before your shift starts."}
                    </p>
                  )}
                  {kind === "weekly_off" && (
                    <p className="attendance-form-note">
                      Take this day as your weekly off instead of Sunday. Once{" "}
                      {user.role === "manager" ? "the super admin" : "your manager"} approves, that week&rsquo;s Sunday
                      becomes a working day. No leave is used and no colleague needs to cover.
                    </p>
                  )}
                  {LEAVE_KINDS.includes(kind) && (
                    <>
                      <label className="is-wide">
                        Who will handle your work that day?
                        <select value={coverId} onChange={(event) => setCoverId(event.target.value)} required>
                          <option value="">{coverColleagues.length ? "Select a colleague" : "No colleague available"}</option>
                          {coverColleagues.map((colleague) => (
                            <option key={colleague.id} value={colleague.id}>{colleague.name}</option>
                          ))}
                        </select>
                      </label>
                      <p className="attendance-form-note">
                        Your colleague is asked first. Only after they accept does the request go to{" "}
                        {user.role === "manager" ? "the super admin" : "your manager"} for approval. On your leave day your
                        unfinished candidates move to them; whatever they complete stays done, and anything still pending
                        comes back to you the next day.
                      </p>
                    </>
                  )}
                  <label className="is-wide">Reason<textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why is this permission needed?" /></label>
                  <div className="attendance-form-actions">
                    <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void submitPermission()}><Send size={15} /> Send for approval</button>
                  </div>
                </div>
              ) : (
                <div className="attendance-form">
                  <label>Date<input type="date" value={otDate} onChange={(event) => setOtDate(event.target.value)} /></label>
                  <label>Extra minutes<input type="number" min="1" max="1440" value={otMinutes} onChange={(event) => setOtMinutes(event.target.value)} /></label>
                  <label className="is-wide">Reason<textarea rows={3} value={otReason} onChange={(event) => setOtReason(event.target.value)} placeholder="What was worked beyond the normal shift?" /></label>
                  <p className="attendance-form-note">
                    Extra OT is not paid. Approved OT only offsets late or short time in the same month; pending and rejected requests do not count.
                  </p>
                  <div className="attendance-form-actions">
                    <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void submitExtraOt()}><Send size={15} /> Send for approval</button>
                  </div>
                </div>
              )}
            </section>

            <aside className="ds-panel attendance-rules">
              <h2 className="ds-panel-title">How it works</h2>
              <ul>
                <li><Clock3 size={15} /><span><strong>8 payable hours</strong>{day?.shift_start && day?.shift_end ? `${timeOf(day.shift_start)}–${timeOf(day.shift_end)}` : "10:00 AM–7:00 PM"} with a one-hour break</span></li>
                <li><ShieldCheck size={15} /><span><strong>60 min permission</strong>Up to 2 occasions a month, paid</span></li>
                <li><CalendarDays size={15} /><span><strong>1 paid leave</strong>Per month</span></li>
                <li><Timer size={15} /><span><strong>Extra OT</strong>Not paid; approved OT offsets late time</span></li>
                <li><WalletCards size={15} /><span><strong>Deductions</strong>Uncovered time is deducted by the minute</span></li>
              </ul>
            </aside>
          </div>
        )}

        {!isTeamView && activeTab === "history" && (
          <>
            <PermissionTable
              permissions={selectedPermissions}
              staff={staff}
              admin={false}
              busy={busy}
              onDecision={decidePermission}
              months={adminMonths}
              colleagues={coverColleagues}
              onAskCover={askAnotherCover}
            />
            <ExtraOtSection
              requests={extraOt}
              staff={staff}
              admin={false}
              busy={busy}
              approvedMinutes={approvedOtMinutes}
              onDecision={decideOt}
            />
          </>
        )}

        {!isTeamView && activeTab === "weekly" && (
          <section className="ds-panel">
            <div className="ds-panel-head">
              <div>
                <h2 className="ds-panel-title">Choose your weekly off</h2>
                <p className="ds-panel-sub">Sunday by default. To take Friday off instead, choose it by Thursday 11:59 PM; that week&rsquo;s Sunday then becomes a working day. For any other day, send a &ldquo;Weekly off (rotational)&rdquo; request for approval.</p>
              </div>
            </div>
            {weeklyOffWeeks.length === 0 ? (
              <div className="ds-empty-state"><CalendarDays size={28} /><h3>No upcoming weeks to choose</h3></div>
            ) : (
              <div className="weekly-off-weeks">
                {weeklyOffWeeks.map((week) => (
                  <div key={week.week_start} className={`weekly-off-week ${week.locked ? "is-locked" : ""}`}>
                    <span className="weekly-off-week-label">Week of {shortDate(week.week_start)}</span>
                    {week.approved ? (
                      <span className="weekly-off-approved">
                        {week.day.charAt(0).toUpperCase() + week.day.slice(1, 3)} {shortDate(week.off_date ?? week.sunday)} · approved
                      </span>
                    ) : (
                    <div className="scope-switch" role="group" aria-label={`Weekly off for the week of ${shortDate(week.week_start)}`}>
                      {(["sunday", "friday"] as const).map((choice) => (
                        <button
                          key={choice}
                          type="button"
                          className={week.day === choice ? "is-active" : ""}
                          aria-pressed={week.day === choice}
                          disabled={weeklyOffBusy || week.locked}
                          onClick={() => void saveWeeklyOff(week, choice)}
                        >
                          {choice === "sunday" ? `Sun ${shortDate(week.sunday)}` : `Fri ${shortDate(week.friday)}`}
                        </button>
                      ))}
                    </div>
                    )}
                    <small>{week.approved ? "Approved weekly off; Sunday is a working day" : week.locked ? "Locked" : `Change by ${deadlineLabel(week.deadline)}`}</small>
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {!isTeamView && activeTab === "cover" && (
          <CoverRequestsSection requests={coverRequests} busy={busy} onAnswer={answerCover} />
        )}
      </div>
    </div>
  );
}

interface PageTab {
  id: string;
  label: string;
  icon: ReactNode;
  count?: number;
  alert?: boolean;
}

function PageTabs({ tabs, active, onChange, label }: { tabs: PageTab[]; active: string; onChange: (id: string) => void; label: string }) {
  return (
    <nav className="page-tabs" role="tablist" aria-label={label}>
      {tabs.map((item) => (
        <button
          key={item.id}
          type="button"
          role="tab"
          aria-selected={active === item.id}
          className={`page-tab ${active === item.id ? "is-on" : ""}`}
          onClick={() => onChange(item.id)}
        >
          {item.icon}
          {item.label}
          {item.count !== undefined && <span className={`page-tab-count ${item.alert ? "is-alert" : ""}`}>{item.count}</span>}
        </button>
      ))}
    </nav>
  );
}

function PolicyStrip() {
  return (
    <div className="attendance-policy-strip" aria-label="Attendance policy summary">
      <span><Clock3 size={15} /><strong>8 hours</strong> payable work + 1-hour break</span>
      <span><CalendarDays size={15} /><strong>1 day</strong> paid leave</span>
      <span><ShieldCheck size={15} />Sunday weekly off, or Friday if chosen by Thursday</span>
      <span><WalletCards size={15} />Extra time deducted by minute</span>
    </div>
  );
}

function Stat({ label, value, note, icon }: { label: string; value: string; note: string; icon: ReactNode }) {
  return <div className="ds-stat is-static"><span className="ds-stat-top"><span className="ds-stat-label">{label}</span>{icon}</span><span className="ds-stat-value">{value}</span><span className="ds-stat-foot">{note}</span></div>;
}

/** Leave days colleagues have asked this user to cover. */
function CoverRequestsSection({ requests, busy, onAnswer }: {
  requests: AttendancePermission[];
  busy: boolean;
  onAnswer: (permission: AttendancePermission, accepted: boolean) => Promise<void>;
}) {
  return (
    <section className="ds-panel attendance-permissions">
      <div className="ds-panel-head">
        <div>
          <h2 className="ds-panel-title">Leave cover requests</h2>
          <p className="ds-panel-sub">
            Colleagues asking you to handle their work on their leave day. What you complete stays with you;
            anything left pending goes back to them the next day.
          </p>
        </div>
      </div>
      <div className="ds-table-wrap is-ruled"><table className="ds-table is-ruled"><thead><tr><th>Date</th><th>Colleague</th><th>Leave</th><th>Status</th><th className="is-actions">Action</th></tr></thead><tbody>
        {requests.map((request) => (
          <tr key={request.id}>
            <td>{request.attendance_date}</td>
            <td>{request.employee_name || request.employee_id}</td>
            <td>{PERMISSION_KIND[request.kind]} · <small>{requestStatusLabel(request.status)}</small></td>
            <td>
              <span className={`ds-status ${request.cover_status === "accepted" ? "is-ok" : request.cover_status === "declined" ? "is-bad" : "is-warn"}`}>
                <i />{COVER_STATUS[request.cover_status || "requested"]}
              </span>
              {coverOutcome(request) && <small className="attendance-decision-reason">{coverOutcome(request)}</small>}
            </td>
            <td className="is-actions">
              {request.cover_status === "requested" && request.status === "awaiting_cover" ? (
                <div className="attendance-decision-actions">
                  <button type="button" className="attendance-approve-btn" disabled={busy} onClick={() => void onAnswer(request, true)}><CheckCircle2 size={15} /> I will cover</button>
                  <button type="button" className="attendance-reject-btn" disabled={busy} onClick={() => void onAnswer(request, false)}><XCircle size={15} /> Decline</button>
                </div>
              ) : <span>—</span>}
            </td>
          </tr>
        ))}
      </tbody></table></div>
    </section>
  );
}

function CoverCell({ permission, colleagues, busy, onAskCover }: {
  permission: AttendancePermission;
  colleagues: CoverColleague[];
  busy: boolean;
  onAskCover?: (permission: AttendancePermission, colleagueId: string) => Promise<void>;
}) {
  if (!LEAVE_KINDS.includes(permission.kind)) return <span>—</span>;
  const outcome = coverOutcome(permission);
  const canAsk = onAskCover && permission.status === "awaiting_cover" && permission.cover_status !== "accepted";
  return (
    <span className="attendance-cover-cell">
      {permission.cover_employee_name ? (
        <span>
          {permission.cover_employee_name}{" "}
          <small>({COVER_STATUS[permission.cover_status || "requested"]})</small>
        </span>
      ) : <span>No cover</span>}
      {outcome && <small className="attendance-decision-reason">{outcome}</small>}
      {canAsk && (permission.cover_status === "declined" || !permission.cover_employee_id) && (
        <select
          aria-label="Ask a colleague to cover"
          value=""
          disabled={busy}
          onChange={(event) => void onAskCover(permission, event.target.value)}
        >
          <option value="">{permission.cover_employee_id ? "Ask someone else…" : "Ask a colleague…"}</option>
          {colleagues.filter((c) => c.id !== permission.cover_employee_id).map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
      )}
    </span>
  );
}

function PermissionTable({ permissions, staff, admin, busy, onDecision, months, colleagues = [], onAskCover }: {
  permissions: AttendancePermission[];
  staff: StaffMember[];
  admin: boolean;
  busy: boolean;
  onDecision: (permission: AttendancePermission, approved: boolean) => Promise<void>;
  months: Record<string, AttendanceMonth>;
  colleagues?: CoverColleague[];
  onAskCover?: (permission: AttendancePermission, colleagueId: string) => Promise<void>;
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
                    <span className={`ds-status ${statusTone(permission.status)}`}><i />{requestStatusLabel(permission.status)}</span>
                  </header>
                  <p className="attendance-request-reason">{permission.reason}</p>
                  <div className="attendance-request-usage">
                    <div><span>Requested</span><strong>{permission.requested_minutes ? `${permission.requested_minutes} min` : "Full day"}</strong></div>
                    <div><span>Leave this month</span><strong>{leaveDays} day(s)</strong></div>
                    <div><span>Hours approved</span><strong>{permissionMinutes} min</strong></div>
                  </div>
                  {LEAVE_KINDS.includes(permission.kind) && (
                    <p className="attendance-decision-reason">
                      Cover: {permission.cover_employee_name
                        ? `${permission.cover_employee_name} (${COVER_STATUS[permission.cover_status || "requested"]})`
                        : "none named"}
                      {coverOutcome(permission) ? ` · ${coverOutcome(permission)}` : ""}
                    </p>
                  )}
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
        <div className="ds-table-wrap is-ruled"><table className="ds-table is-ruled"><thead><tr><th>Date</th><th>Type</th><th>Minutes</th><th>Reason</th><th>Cover</th><th>Status</th></tr></thead><tbody>
          {permissions.map((permission) => <tr key={permission.id}>
            <td>{permission.attendance_date}</td><td>{PERMISSION_KIND[permission.kind]}</td><td>{permission.requested_minutes || "Full day"}</td>
            <td><span>{permission.reason}</span>{permission.decision_reason && <small className="attendance-decision-reason">{permission.decision_reason}</small>}</td>
            <td><CoverCell permission={permission} colleagues={colleagues} busy={busy} onAskCover={onAskCover} /></td>
            <td><span className={`ds-status ${statusTone(permission.status)}`}><i />{requestStatusLabel(permission.status)}</span></td>
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
            {durationLabel(approvedMinutes)} approved this month. OT is not paid; it only offsets late time.
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
  plans, employeeName, picker, busy, date, start, end, breakMinutes, reason,
  onDateChange, onStartChange, onEndChange, onBreakChange, onReasonChange, onSave, onRemove,
}: {
  plans: DutyPlan[];
  employeeName?: string;
  picker?: ReactNode;
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
        {picker}
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
