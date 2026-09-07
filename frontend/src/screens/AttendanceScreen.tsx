"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CalendarDays, Clock3, LogIn, LogOut, RefreshCw, ShieldCheck, WalletCards } from "lucide-react";

import {
  fetchAttendanceDay,
  fetchAttendanceMonth,
  listStaff,
  recordAttendancePunch,
  requestAttendancePermission,
  type AttendanceDay,
  type AttendanceMonth,
  type AuthUser,
  type StaffMember,
} from "@/lib/api";

interface Props {
  user: AuthUser;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
}

const STATUS: Record<string, string> = {
  P: "Present", LT: "Late", EE: "Early exit", PP: "Paid permission", OD: "Official duty",
  WFH: "Work from home", PL: "Paid leave", UL: "Unpaid leave", A: "Absent",
  MP: "Missing punch", WO: "Weekly off", H: "Holiday",
};

function kolkataDate(): string {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts();
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${value.year}-${value.month}-${value.day}`;
}

function timeOf(value?: string | null): string {
  if (!value) return "Not recorded";
  return new Intl.DateTimeFormat("en-IN", { timeZone: "Asia/Kolkata", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export default function AttendanceScreen({ user, onToast }: Props) {
  const today = useMemo(() => kolkataDate(), []);
  const [day, setDay] = useState<AttendanceDay | null>(null);
  const [month, setMonth] = useState<AttendanceMonth | null>(null);
  const [staff, setStaff] = useState<StaffMember[]>([]);
  const [employeeId, setEmployeeId] = useState("");
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState<"late" | "early_exit" | "official_duty" | "work_from_home">("late");
  const [minutes, setMinutes] = useState("15");
  const [reason, setReason] = useState("");
  const selectedEmployee = user.role === "admin" ? employeeId || undefined : undefined;

  const load = useCallback(async () => {
    if (user.role === "admin" && !employeeId) return;
    try {
      const [daily, monthly] = await Promise.all([
        fetchAttendanceDay(today, selectedEmployee),
        fetchAttendanceMonth(Number(today.slice(0, 4)), Number(today.slice(5, 7)), selectedEmployee),
      ]);
      setDay(daily);
      setMonth(monthly);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not load attendance", "error");
    }
  }, [employeeId, onToast, selectedEmployee, today, user.role]);

  useEffect(() => {
    if (user.role !== "admin") return;
    void listStaff(false).then(({ items }) => {
      setStaff(items);
      setEmployeeId((current) => current || items[0]?.id || "");
    }).catch(() => onToast("Could not load employees", "error"));
  }, [onToast, user.role]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const punch = async (action: "check_in" | "check_out") => {
    setBusy(true);
    try {
      await recordAttendancePunch(action, selectedEmployee);
      onToast(action === "check_in" ? "Check-in recorded" : "Check-out recorded", "success");
      await load();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Punch could not be recorded", "error");
    } finally { setBusy(false); }
  };

  const submitPermission = async () => {
    if (!reason.trim()) return onToast("Enter a reason for the permission", "info");
    setBusy(true);
    try {
      await requestAttendancePermission({ employee_id: selectedEmployee, attendance_date: today, kind, requested_minutes: Number(minutes) || 0, reason: reason.trim() });
      setReason("");
      onToast("Permission sent for approval", "success");
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Permission could not be submitted", "error");
    } finally { setBusy(false); }
  };

  const used = month?.days.at(-1)?.permission_minutes_used ?? 0;
  const occasions = month?.days.at(-1)?.permission_occasions_used ?? 0;

  return (
    <div className="ds-page attendance-page">
      <div className="attendance-toolbar">
        {user.role === "admin" && (
          <label className="attendance-select">Employee
            <select value={employeeId} onChange={(event) => setEmployeeId(event.target.value)}>
              {staff.map((person) => <option key={person.id} value={person.id}>{person.name} · {person.staff_code || person.email}</option>)}
            </select>
          </label>
        )}
        <button className="ds-ghost-btn" type="button" onClick={() => void load()}><RefreshCw size={14} /> Refresh</button>
      </div>

      <div className="ds-stats is-five attendance-stats">
        <div className="ds-stat is-static"><span className="ds-stat-top"><span className="ds-stat-label">Today</span><CalendarDays size={16} /></span><span className="ds-stat-value">{STATUS[day?.status || ""] || "—"}</span><span className="ds-stat-foot">{today}</span></div>
        <div className="ds-stat is-static"><span className="ds-stat-top"><span className="ds-stat-label">Check in</span><LogIn size={16} /></span><span className="ds-stat-value">{timeOf(day?.check_in)}</span><span className="ds-stat-foot">Server-recorded time</span></div>
        <div className="ds-stat is-static"><span className="ds-stat-top"><span className="ds-stat-label">Check out</span><LogOut size={16} /></span><span className="ds-stat-value">{timeOf(day?.check_out)}</span><span className="ds-stat-foot">Server-recorded time</span></div>
        <div className="ds-stat is-static"><span className="ds-stat-top"><span className="ds-stat-label">Permission</span><ShieldCheck size={16} /></span><span className="ds-stat-value">{used} / 60 min</span><span className="ds-stat-foot">{occasions} of 2 occasions</span></div>
        <div className="ds-stat is-static"><span className="ds-stat-top"><span className="ds-stat-label">Provisional unpaid</span><WalletCards size={16} /></span><span className="ds-stat-value">{month?.totals.unpaid_minutes ?? 0} min</span><span className="ds-stat-foot">Actual absence time only</span></div>
      </div>

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
            <label>Type<select value={kind} onChange={(event) => setKind(event.target.value as typeof kind)}><option value="late">Late arrival</option><option value="early_exit">Early leaving</option><option value="official_duty">Official duty</option><option value="work_from_home">Work from home</option></select></label>
            <label>Minutes<input type="number" min="0" max="1440" value={minutes} onChange={(event) => setMinutes(event.target.value)} disabled={kind === "official_duty" || kind === "work_from_home"} /></label>
            <label className="is-wide">Reason<textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why is this permission needed?" /></label>
            <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void submitPermission()}>Send for approval</button>
          </div>
        </section>
      </div>

      <section className="ds-panel">
        <div className="ds-panel-head"><div><h2 className="ds-panel-title">This month</h2><p className="ds-panel-sub">Daily status and exact-minute deductions. Records remain available after the monthly balance resets.</p></div></div>
        <div className="ds-table-wrap is-ruled"><table className="ds-table is-ruled"><thead><tr><th>Date</th><th>Status</th><th>In</th><th>Out</th><th>Late</th><th>Early</th><th>Paid permission</th><th>Unpaid</th></tr></thead><tbody>
          {[...(month?.days ?? [])].reverse().map((row) => <tr key={row.date}><td>{row.date}</td><td><span className={`ds-status ${row.status === "A" || row.status === "UL" ? "is-bad" : row.status === "MP" || row.status === "LT" || row.status === "EE" ? "is-warn" : "is-info"}`}><i />{STATUS[row.status]}</span></td><td>{timeOf(row.check_in)}</td><td>{timeOf(row.check_out)}</td><td>{row.late_minutes} min</td><td>{row.early_minutes} min</td><td>{row.paid_permission_minutes ?? 0} min</td><td>{row.unpaid_minutes} min</td></tr>)}
        </tbody></table></div>
      </section>
    </div>
  );
}
