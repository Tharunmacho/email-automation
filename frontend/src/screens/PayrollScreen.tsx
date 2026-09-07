"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { Banknote, Check, Clock3, RefreshCw, WalletCards } from "lucide-react";
import { fetchPayrollMonth, updatePayrollPolicy, updatePayrollStatus, type PayrollMonth, type PayrollRow } from "@/lib/api";

interface Props { onToast: (message: string, type?: "success" | "error" | "info") => void; }
const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });
const currentMonth = () => { const now = new Date(); return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`; };

export default function PayrollScreen({ onToast }: Props) {
  const [period, setPeriod] = useState(currentMonth);
  const [payroll, setPayroll] = useState<PayrollMonth | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [year, month] = period.split("-").map(Number);
  const load = useCallback(async () => { setLoading(true); try { setPayroll(await fetchPayrollMonth(year, month)); } catch (error) { onToast(error instanceof Error ? error.message : "Could not load payroll", "error"); } finally { setLoading(false); } }, [month, onToast, year]);
  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);
  const totals = useMemo(() => (payroll?.items ?? []).reduce((sum, row) => ({ gross: sum.gross + row.monthly_salary, deduction: sum.deduction + row.deduction, net: sum.net + row.net_salary, paid: sum.paid + Number(row.status === "paid") }), { gross: 0, deduction: 0, net: 0, paid: 0 }), [payroll]);

  const savePolicy = async (row: PayrollRow, patch: Partial<PayrollRow>) => { setBusyId(row.employee_id); try { await updatePayrollPolicy(row.employee_id, { monthly_salary: Number(patch.monthly_salary ?? row.monthly_salary), weekly_off_pattern: patch.weekly_off_pattern ?? row.weekly_off_pattern, alternate_friday_parity: Number(patch.alternate_friday_parity ?? row.alternate_friday_parity) }); await load(); onToast(`${row.name}'s payroll settings saved`, "success"); } catch (error) { onToast(error instanceof Error ? error.message : "Could not save payroll settings", "error"); } finally { setBusyId(""); } };
  const togglePaid = async (row: PayrollRow) => { setBusyId(row.employee_id); try { await updatePayrollStatus(year, month, row.employee_id, row.status !== "paid"); await load(); } catch (error) { onToast(error instanceof Error ? error.message : "Could not update payment status", "error"); } finally { setBusyId(""); } };

  return <div className="ds-page payroll-page">
    <div className="ds-page-head"><div><p className="ds-eyebrow">General</p><h1 className="ds-head-title">Payroll</h1><p className="ds-head-sub">30-day salary basis · Sundays off · 60 minutes monthly grace · one paid leave</p></div><div className="payroll-toolbar"><label>Payroll month<input type="month" value={period} onChange={(event) => setPeriod(event.target.value)} /></label><button type="button" className="ds-ghost-btn" onClick={() => void load()} disabled={loading}><RefreshCw size={14} /> Refresh</button></div></div>
    <div className="ds-stats payroll-stats"><Stat label="Gross payroll" value={money.format(totals.gross)} icon={<Banknote size={16} />} /><Stat label="Attendance deductions" value={money.format(totals.deduction)} icon={<Clock3 size={16} />} /><Stat label="Net payroll" value={money.format(totals.net)} icon={<WalletCards size={16} />} /><Stat label="Paid" value={`${totals.paid} / ${payroll?.items.length ?? 0}`} icon={<Check size={16} />} /></div>
    <section className="ds-panel"><div className="ds-panel-head"><div><h2 className="ds-panel-title">Employee payroll</h2><p className="ds-panel-sub">Set salary and the Friday rotation; deductions recalculate from attendance.</p></div></div>
      {loading ? <div className="ds-empty-state"><RefreshCw className="icon-spin" /> Loading payroll…</div> : !payroll?.items.length ? <div className="ds-empty-state">No active employees.</div> : <div className="ds-table-wrap is-ruled"><table className="ds-table is-ruled payroll-table"><thead><tr><th>Employee</th><th>Monthly salary</th><th>Weekly off</th><th>Grace used</th><th>Paid leave</th><th>Unpaid</th><th>Deduction</th><th>Net salary</th><th>Status</th></tr></thead><tbody>{payroll.items.map((row) => <tr key={row.employee_id}><td><strong>{row.name}</strong><small>{row.staff_code}</small></td><td><input className="payroll-salary" type="number" min="0" defaultValue={row.monthly_salary} disabled={busyId === row.employee_id} onBlur={(event) => { const value = Number(event.target.value); if (value !== row.monthly_salary) void savePolicy(row, { monthly_salary: value }); }} /></td><td><select value={row.weekly_off_pattern} disabled={busyId === row.employee_id} onChange={(event) => void savePolicy(row, { weekly_off_pattern: event.target.value as PayrollRow["weekly_off_pattern"] })}><option value="sunday">Every Sunday</option><option value="sunday_alternate_friday">Sunday + alternate Friday</option></select>{row.weekly_off_pattern === "sunday_alternate_friday" && <select value={row.alternate_friday_parity} disabled={busyId === row.employee_id} onChange={(event) => void savePolicy(row, { alternate_friday_parity: Number(event.target.value) })}><option value={0}>Friday rotation A</option><option value={1}>Friday rotation B</option></select>}</td><td>{row.grace_minutes} / 60 min</td><td>{row.paid_leave_days} / 1</td><td>{row.unpaid_minutes} min</td><td className="is-warn">{money.format(row.deduction)}</td><td><strong>{money.format(row.net_salary)}</strong></td><td><button type="button" className={`ds-status payroll-status is-${row.status === "paid" ? "ok" : "neutral"}`} disabled={busyId === row.employee_id} onClick={() => void togglePaid(row)}><i /> {row.status === "paid" ? "Paid" : "Draft"}</button></td></tr>)}</tbody></table></div>}
    </section>
  </div>;
}

function Stat({ label, value, icon }: { label: string; value: string; icon: ReactNode }) { return <article className="ds-stat"><div className="ds-stat-head"><span>{label}</span>{icon}</div><strong className="ds-stat-value">{value}</strong></article>; }
