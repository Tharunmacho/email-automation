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
  UserCheck,
  Users,
  WalletCards,
  XCircle,
} from "lucide-react";

import {
  decideAttendancePermission,
  fetchAttendanceDay,
  fetchAttendanceMonth,
  fetchAttendancePermissions,
  listStaff,
  recordAttendancePunch,
  requestAttendancePermission,
  setAttendanceLeave,
  type AttendanceDay,
  type AttendanceMonth,
  type AttendancePermission,
  type AuthUser,
  type StaffMember,
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
  official_duty: "Official duty",
  work_from_home: "Work from home",
  paid_leave: "Paid leave",
  unpaid_leave: "Unpaid leave",
};

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

function monthSummary(month?: AttendanceMonth | null): AttendanceSummary {
  const days = month?.days ?? [];
  const scheduled = days.filter((day) => !NON_WORKING.has(day.status)).length;
  const attended = days.filter((day) => ATTENDED.has(day.status)).length;
  return {
    percentage: scheduled ? Math.round((attended / scheduled) * 1000) / 10 : 0,
    scheduled,
    attended,
    absent: days.filter((day) => day.status === "A" || day.status === "UL").length,
    late: days.filter((day) => day.status === "LT").length,
    missing: days.filter((day) => day.status === "MP").length,
  };
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
  const [leaveDate, setLeaveDate] = useState(today);
  const [leaveStatus, setLeaveStatus] = useState<"PL" | "UL" | "H">("PL");
  const [leaveReason, setLeaveReason] = useState("");

  const isAdmin = user.role === "admin" || user.role === "manager";
  const [yearText, monthText] = yearMonth.split("-");
  const year = Number(yearText);
  const monthNumber = Number(monthText);
  const selectedStaff = staff.find((person) => person.id === employeeId);

  useEffect(() => {
    if (!isAdmin) return;
    let active = true;
    listStaff(false)
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
  }, [isAdmin, onToast]);

  const load = useCallback(async () => {
    if (!year || !monthNumber || (isAdmin && staff.length === 0)) return;
    setLoading(true);
    try {
      if (isAdmin) {
        const [months, permissionResult] = await Promise.all([
          Promise.all(
            staff.map(async (person) => [
              person.id,
              await fetchAttendanceMonth(year, monthNumber, person.id),
            ] as const),
          ),
          fetchAttendancePermissions(year, monthNumber),
        ]);
        const byEmployee = Object.fromEntries(months);
        setAdminMonths(byEmployee);
        setPermissions(permissionResult.items ?? []);
        if (employeeId) setDay(await fetchAttendanceDay(today, employeeId));
      } else {
        const [daily, monthly, permissionResult] = await Promise.all([
          fetchAttendanceDay(today),
          fetchAttendanceMonth(year, monthNumber),
          fetchAttendancePermissions(year, monthNumber),
        ]);
        setDay(daily);
        setMonth(monthly);
        setPermissions(permissionResult.items ?? []);
      }
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not load attendance", "error");
    } finally {
      setLoading(false);
    }
  }, [employeeId, isAdmin, monthNumber, onToast, staff, today, year]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const punch = async (action: "check_in" | "check_out") => {
    setBusy(true);
    try {
      await recordAttendancePunch(action);
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
        requested_minutes: Number(minutes) || 0,
        reason: reason.trim(),
      });
      setReason("");
      onToast("Permission sent for approval", "success");
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

  const recordLeave = async () => {
    if (!employeeId || !leaveReason.trim()) return onToast("Select an employee and enter a leave reason", "info");
    setBusy(true);
    try {
      const result = await setAttendanceLeave({ employee_id: employeeId, attendance_date: leaveDate, status: leaveStatus, reason: leaveReason.trim() });
      setLeaveReason("");
      onToast(result.calendar_day.converted_from_paid_leave ? "Paid leave already used; recorded as unpaid leave" : "Leave recorded", "success");
      await load();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Leave could not be recorded", "error");
    } finally { setBusy(false); }
  };

  const visibleMonth = isAdmin ? adminMonths[employeeId] ?? null : month;
  const summary = monthSummary(visibleMonth);
  // Managers see the whole request inbox; the selected employee only controls
  // the detailed calendar below it.
  const selectedPermissions = permissions;
  const pendingCount = permissions.filter((permission) => permission.status === "pending").length;
  const rosterSummaries = staff.map((person) => ({
    person,
    month: adminMonths[person.id],
    summary: monthSummary(adminMonths[person.id]),
    permissions: permissions.filter((permission) => permission.employee_id === person.id),
  }));
  const organisationScheduled = rosterSummaries.reduce((sum, row) => sum + row.summary.scheduled, 0);
  const organisationAttended = rosterSummaries.reduce((sum, row) => sum + row.summary.attended, 0);
  const organisationPercentage = organisationScheduled
    ? Math.round((organisationAttended / organisationScheduled) * 1000) / 10
    : 0;
  const permissionUsed = visibleMonth?.totals.paid_permission_minutes ?? 0;

  return (
    <div className={`ds-page attendance-page ${isAdmin ? "is-manager-view" : "is-staff-view"}`}>
      <header className="ds-head attendance-hero">
        <div>
          <span className="attendance-kicker">{isAdmin ? "WORKFORCE OPERATIONS" : "WORKDAY"}</span>
          <h1 className="ds-head-title">{isAdmin ? "Staff attendance" : "My attendance"}</h1>
          <p className="ds-head-sub">
            {isAdmin
              ? "Monthly attendance, exceptions and permission requests for every active staff member"
              : "Your attendance percentage, punches and permission history"}
          </p>
        </div>
        <div className="attendance-toolbar">
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
        <span><Clock3 size={15} /><strong>60 min</strong> monthly grace</span>
        <span><CalendarDays size={15} /><strong>1 day</strong> paid leave</span>
        <span><ShieldCheck size={15} />Sunday + assigned rotation off</span>
        <span><WalletCards size={15} />Extra time deducted by minute</span>
      </div>

      {isAdmin ? (
        <>
          <div className="ds-stats attendance-stats attendance-admin-stats">
            <Stat label="Team attendance" value={`${organisationPercentage}%`} note={`${organisationAttended} of ${organisationScheduled} staff-days`} icon={<Percent size={16} />} />
            <Stat label="Active staff" value={String(staff.length)} note="Included in this month" icon={<Users size={16} />} />
            <Stat label="Pending permissions" value={String(pendingCount)} note="Awaiting an admin decision" icon={<ShieldCheck size={16} />} />
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
                <thead><tr><th>Staff</th><th>Attendance</th><th>Present</th><th>Absent</th><th>Late</th><th>Missing punch</th><th>Permissions</th><th>Unpaid</th><th /></tr></thead>
                <tbody>
                  {rosterSummaries.map(({ person, month: staffMonth, summary: staffSummary, permissions: staffPermissions }) => (
                    <tr key={person.id} className={employeeId === person.id ? "is-selected" : undefined}>
                      <td><span className="ds-who-text"><strong>{person.name}</strong><small>{person.staff_code || person.email}</small></span></td>
                      <td><div className="attendance-rate"><strong className="attendance-percentage">{staffSummary.percentage}%</strong><span><i style={{ width: `${Math.min(100, staffSummary.percentage)}%` }} /></span></div></td>
                      <td>{staffSummary.attended} / {staffSummary.scheduled}</td>
                      <td>{staffSummary.absent}</td>
                      <td>{staffSummary.late}</td>
                      <td>{staffSummary.missing}</td>
                      <td>{staffPermissions.length}</td>
                      <td>{staffMonth?.totals.unpaid_minutes ?? 0} min</td>
                      <td><button type="button" className="ds-ghost-btn" onClick={() => setEmployeeId(person.id)}>View</button></td>
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
          <Stat label="Today" value={STATUS[day?.status || ""] || "—"} note={today} icon={<CalendarDays size={16} />} />
          <Stat label="Check in" value={timeOf(day?.check_in)} note="Server-recorded time" icon={<LogIn size={16} />} />
          <Stat label="Permission used" value={`${permissionUsed} / 60 min`} note={`${month?.totals.permission_occasions ?? 0} of 2 occasions`} icon={<ShieldCheck size={16} />} />
          <Stat label="Unpaid" value={`${month?.totals.unpaid_minutes ?? 0} min`} note="Actual absence time only" icon={<WalletCards size={16} />} />
        </div>
      )}

      {!isAdmin && (
        <div className="attendance-grid">
          <section className="ds-panel">
            <div className="ds-panel-head"><div><h2 className="ds-panel-title">Today’s attendance</h2><p className="ds-panel-sub">Default shift 10:00 AM–7:00 PM unless another shift is assigned.</p></div><Clock3 size={20} /></div>
            <div className="attendance-minutes"><span><strong>{day?.late_minutes ?? 0}</strong> late minutes</span><span><strong>{day?.early_minutes ?? 0}</strong> early-exit minutes</span></div>
            <div className="attendance-actions">
              <button type="button" className="ds-primary-btn" disabled={busy || Boolean(day?.check_in)} onClick={() => void punch("check_in")}><LogIn size={15} /> Check in</button>
              <button type="button" className="ds-ghost-btn" disabled={busy || !day?.check_in || Boolean(day?.check_out)} onClick={() => void punch("check_out")}><LogOut size={15} /> Check out</button>
            </div>
          </section>

          <section className="ds-panel">
            <div className="ds-panel-head"><div><h2 className="ds-panel-title">Request permission</h2><p className="ds-panel-sub">Late and WFH requests must be submitted before the shift starts.</p></div></div>
            <div className="attendance-form">
              <label>Date<input type="date" value={permissionDate} onChange={(event) => setPermissionDate(event.target.value)} /></label>
              <label>Type<select value={kind} onChange={(event) => setKind(event.target.value as AttendancePermission["kind"])}><option value="late">Late arrival</option><option value="early_exit">Early leaving</option><option value="official_duty">Official duty</option><option value="work_from_home">Work from home</option><option value="paid_leave">Paid leave / holiday</option><option value="unpaid_leave">Unpaid leave</option></select></label>
              <label>Minutes<input type="number" min="0" max="1440" value={minutes} onChange={(event) => setMinutes(event.target.value)} disabled={!["late", "early_exit"].includes(kind)} /></label>
              <label className="is-wide">Reason<textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why is this permission needed?" /></label>
              <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void submitPermission()}>Send for approval</button>
            </div>
          </section>
        </div>
      )}

      {isAdmin && selectedStaff && (
        <section className="ds-panel attendance-selected-head">
          <div className="ds-panel-head">
            <div><h2 className="ds-panel-title">{selectedStaff.name}</h2><p className="ds-panel-sub">{selectedStaff.staff_code || selectedStaff.email} · {summary.percentage}% attendance</p></div>
            <UserCheck size={20} />
          </div>
          <div className="attendance-form attendance-leave-form">
            <label>Leave date<input type="date" value={leaveDate} onChange={(event) => setLeaveDate(event.target.value)} /></label>
            <label>Leave type<select value={leaveStatus} onChange={(event) => setLeaveStatus(event.target.value as "PL" | "UL" | "H")}><option value="PL">Paid leave (first each month)</option><option value="UL">Unpaid leave</option><option value="H">Company paid holiday</option></select></label>
            <label className="is-wide">Reason<input value={leaveReason} onChange={(event) => setLeaveReason(event.target.value)} placeholder="Reason for leave" /></label>
            <button type="button" className="ds-primary-btn" disabled={busy || !leaveReason.trim()} onClick={() => void recordLeave()}>Record leave</button>
          </div>
        </section>
      )}

      <PermissionTable
        permissions={selectedPermissions}
        staff={staff}
        admin={isAdmin}
        busy={busy}
        onDecision={decidePermission}
        months={adminMonths}
      />

      <MonthTable month={visibleMonth} title={isAdmin && selectedStaff ? `${selectedStaff.name} · daily attendance` : "This month"} />
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
        {ordered.length === 0 ? <div className="ds-empty-state"><ShieldCheck size={28} /><h3>No permission requests this month</h3><p>New employee requests will appear here.</p></div> : (
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

function MonthTable({ month, title }: { month: AttendanceMonth | null; title: string }) {
  return (
    <section className="ds-panel">
      <div className="ds-panel-head"><div><h2 className="ds-panel-title">{title}</h2><p className="ds-panel-sub">Daily status and exact-minute attendance records.</p></div></div>
      <div className="ds-table-wrap is-ruled"><table className="ds-table is-ruled"><thead><tr><th>Date</th><th>Status</th><th>In</th><th>Out</th><th>Late</th><th>Early</th><th>Paid permission</th><th>Unpaid</th></tr></thead><tbody>
        {[...(month?.days ?? [])].reverse().map((row) => <tr key={row.date}><td>{row.date}</td><td><span className={`ds-status ${row.status === "A" || row.status === "UL" ? "is-bad" : row.status === "MP" || row.status === "LT" || row.status === "EE" ? "is-warn" : "is-info"}`}><i />{STATUS[row.status]}</span></td><td>{timeOf(row.check_in)}</td><td>{timeOf(row.check_out)}</td><td>{row.late_minutes} min</td><td>{row.early_minutes} min</td><td>{row.paid_permission_minutes ?? 0} min</td><td>{row.unpaid_minutes} min</td></tr>)}
      </tbody></table></div>
    </section>
  );
}
