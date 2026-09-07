"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Banknote,
  CalendarRange,
  Check,
  CheckCircle2,
  Clock3,
  RefreshCw,
  RotateCcw,
  Settings2,
  ShieldCheck,
  WalletCards,
} from "lucide-react";

import {
  fetchPayrollMonth,
  updatePayrollPolicy,
  updatePayrollStatus,
  type PayrollMonth,
  type PayrollRow,
} from "@/lib/api";

interface Props {
  onToast: (message: string, type?: "success" | "error" | "info") => void;
}

const money = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

function currentMonth(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}

function monthLabel(period: string): string {
  const [year, month] = period.split("-").map(Number);
  return new Intl.DateTimeFormat("en-IN", { month: "long", year: "numeric" }).format(
    new Date(year, month - 1, 1),
  );
}

export default function PayrollScreen({ onToast }: Props) {
  const [period, setPeriod] = useState(currentMonth);
  const [payroll, setPayroll] = useState<PayrollMonth | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [year, month] = period.split("-").map(Number);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPayroll(await fetchPayrollMonth(year, month));
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not load payroll", "error");
    } finally {
      setLoading(false);
    }
  }, [month, onToast, year]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const totals = useMemo(
    () =>
      (payroll?.items ?? []).reduce(
        (sum, row) => ({
          gross: sum.gross + row.monthly_salary,
          deduction: sum.deduction + row.deduction,
          net: sum.net + row.net_salary,
          paid: sum.paid + Number(row.status === "paid"),
        }),
        { gross: 0, deduction: 0, net: 0, paid: 0 },
      ),
    [payroll],
  );

  const savePolicy = async (row: PayrollRow, patch: Partial<PayrollRow>) => {
    setBusyId(row.employee_id);
    try {
      await updatePayrollPolicy(row.employee_id, {
        monthly_salary: Number(patch.monthly_salary ?? row.monthly_salary),
        weekly_off_pattern: patch.weekly_off_pattern ?? row.weekly_off_pattern,
        alternate_friday_parity: Number(
          patch.alternate_friday_parity ?? row.alternate_friday_parity,
        ),
      });
      await load();
      onToast(`${row.name}'s payroll settings saved`, "success");
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not save payroll settings", "error");
    } finally {
      setBusyId("");
    }
  };

  const togglePaid = async (row: PayrollRow) => {
    setBusyId(row.employee_id);
    try {
      const paid = row.status !== "paid";
      await updatePayrollStatus(year, month, row.employee_id, paid);
      await load();
      onToast(`${row.name}'s payroll ${paid ? "marked as paid" : "reopened"}`, "success");
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Could not update payment status", "error");
    } finally {
      setBusyId("");
    }
  };

  return (
    <div className="ds-page payroll-page">
      <header className="payroll-hero">
        <div>
          <span className="payroll-kicker"><WalletCards size={14} /> Monthly payroll run</span>
          <h1>{monthLabel(period)}</h1>
          <p>Review attendance deductions, confirm net pay, then mark each employee as paid.</p>
        </div>
        <div className="payroll-toolbar">
          <label>
            Pay period
            <input type="month" value={period} onChange={(event) => setPeriod(event.target.value)} />
          </label>
          <button type="button" className="ds-ghost-btn" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={15} className={loading ? "icon-spin" : ""} /> Refresh
          </button>
        </div>
      </header>

      <div className="payroll-summary-grid">
        <SummaryCard label="Gross payroll" value={money.format(totals.gross)} note="Before deductions" icon={<Banknote />} tone="blue" />
        <SummaryCard label="Attendance deductions" value={money.format(totals.deduction)} note="LOP and uncovered time" icon={<Clock3 />} tone="amber" />
        <SummaryCard label="Net payable" value={money.format(totals.net)} note="Final amount for this run" icon={<WalletCards />} tone="green" />
        <SummaryCard label="Payment progress" value={`${totals.paid} of ${payroll?.items.length ?? 0}`} note="Employees marked paid" icon={<CheckCircle2 />} tone="violet" />
      </div>

      <section className="payroll-policy-strip">
        <span><CalendarRange size={17} /><strong>Working-day basis</strong> Calendar days minus weekly offs and company holidays</span>
        <span><ShieldCheck size={17} /><strong>Monthly allowance</strong> 60-minute grace and one paid leave</span>
      </section>

      <section className="payroll-run">
        <div className="payroll-section-head">
          <div>
            <span className="payroll-section-icon"><Banknote size={18} /></span>
            <div><h2>Employee pay breakdown</h2><p>Each card shows how attendance becomes the final salary.</p></div>
          </div>
          <span className="payroll-count">{payroll?.items.length ?? 0} employees</span>
        </div>

        {loading ? (
          <div className="payroll-empty"><RefreshCw className="icon-spin" /><strong>Preparing payroll</strong><span>Calculating attendance and salary details…</span></div>
        ) : !payroll?.items.length ? (
          <div className="payroll-empty"><WalletCards /><strong>No active employees</strong><span>Add staff before generating payroll.</span></div>
        ) : (
          <div className="payroll-card-grid">
            {payroll.items.map((row) => (
              <EmployeePayCard
                key={row.employee_id}
                row={row}
                busy={busyId === row.employee_id}
                onSave={savePolicy}
                onTogglePaid={togglePaid}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function EmployeePayCard({ row, busy, onSave, onTogglePaid }: {
  row: PayrollRow;
  busy: boolean;
  onSave: (row: PayrollRow, patch: Partial<PayrollRow>) => Promise<void>;
  onTogglePaid: (row: PayrollRow) => Promise<void>;
}) {
  const paid = row.status === "paid";
  const initials = row.name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase();

  return (
    <article className={`payroll-card ${paid ? "is-paid" : ""}`}>
      <header className="payroll-card-head">
        <span className="payroll-avatar">{initials || "ST"}</span>
        <div><h3>{row.name}</h3><p>{row.staff_code || "Employee"}</p></div>
        <span className={`payroll-state ${paid ? "is-paid" : "is-review"}`}>
          {paid ? <CheckCircle2 size={14} /> : <Clock3 size={14} />}
          {paid ? "Paid" : "Ready for review"}
        </span>
      </header>

      <div className="payroll-money-flow">
        <div><span>Gross salary</span><strong>{money.format(row.monthly_salary)}</strong></div>
        <span className="payroll-flow-sign">−</span>
        <div className="is-deduction"><span>Deductions</span><strong>{money.format(row.deduction)}</strong></div>
        <span className="payroll-flow-sign">=</span>
        <div className="is-net"><span>Net payable</span><strong>{money.format(row.net_salary)}</strong></div>
      </div>

      <div className="payroll-metrics">
        <Metric label="Working days" value={`${row.required_working_days} / ${row.calendar_days}`} />
        <Metric label="Daily LOP" value={money.format(row.daily_lop_rate)} />
        <Metric label="Grace used" value={`${row.grace_minutes} / 60 min`} />
        <Metric label="Paid leave" value={`${row.paid_leave_days} / 1 day`} />
        <Metric label="Unpaid time" value={`${row.unpaid_minutes} min`} warn={row.unpaid_minutes > 0} />
      </div>

      <div className="payroll-settings">
        <div className="payroll-settings-title"><Settings2 size={15} /> Payroll settings</div>
        <div className="payroll-settings-grid">
          <label>Monthly salary<input type="number" min="0" defaultValue={row.monthly_salary} disabled={busy} onBlur={(event) => { const value = Number(event.target.value); if (value !== row.monthly_salary) void onSave(row, { monthly_salary: value }); }} /></label>
          <label>Weekly off<select value={row.weekly_off_pattern} disabled={busy} onChange={(event) => void onSave(row, { weekly_off_pattern: event.target.value as PayrollRow["weekly_off_pattern"] })}><option value="sunday">Every Sunday</option><option value="sunday_alternate_friday">Sunday + alternate Friday</option></select></label>
          {row.weekly_off_pattern === "sunday_alternate_friday" && <label>Friday group<select value={row.alternate_friday_parity} disabled={busy} onChange={(event) => void onSave(row, { alternate_friday_parity: Number(event.target.value) })}><option value={0}>Rotation A</option><option value={1}>Rotation B</option></select></label>}
        </div>
      </div>

      <footer className="payroll-card-foot">
        <span>{paid ? "Payment confirmed for this period" : "Review the calculation before confirming payment"}</span>
        <button type="button" className={paid ? "payroll-reopen-btn" : "payroll-pay-btn"} disabled={busy} onClick={() => void onTogglePaid(row)}>
          {busy ? <RefreshCw size={15} className="icon-spin" /> : paid ? <RotateCcw size={15} /> : <Check size={15} />}
          {paid ? "Reopen payroll" : "Mark as paid"}
        </button>
      </footer>
    </article>
  );
}

function SummaryCard({ label, value, note, icon, tone }: { label: string; value: string; note: string; icon: ReactNode; tone: string }) {
  return <article className={`payroll-summary is-${tone}`}><span className="payroll-summary-icon">{icon}</span><div><span>{label}</span><strong>{value}</strong><small>{note}</small></div></article>;
}

function Metric({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return <div className={warn ? "is-warn" : ""}><span>{label}</span><strong>{value}</strong></div>;
}
