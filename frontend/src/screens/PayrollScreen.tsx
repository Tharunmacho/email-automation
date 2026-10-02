"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  Banknote,
  CalendarRange,
  Check,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Download,
  Gift,
  RefreshCw,
  RotateCcw,
  Search,
  Settings2,
  ShieldCheck,
  WalletCards,
} from "lucide-react";

import {
  fetchPayrollMonth,
  payslipUrl,
  updatePayrollPolicy,
  updatePayrollStatus,
  type AuthUser,
  type PayrollMonth,
  type PayrollRow,
} from "@/lib/api";
import BranchSwitch, { branchOptions, useBranches } from "@/components/ui/BranchSwitch";
import DatePicker from "@/components/ui/DatePicker";
import { IncentiveDialog, NetPayableLogs, NetPayableOverrideForm, PayslipDialog, ReimbursementsPanel } from "@/screens/PayrollExtras";

interface Props {
  user: AuthUser;
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

export default function PayrollScreen({ user, onToast }: Props) {
  const [period, setPeriod] = useState(currentMonth);
  const [loadedPayroll, setLoadedPayroll] = useState<{ period: string; data: PayrollMonth } | null>(null);
  const [loadError, setLoadError] = useState<{ period: string; message: string } | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [viewMode, setViewMode] = useState<"team" | "mine">("team");
  const [branch, setBranch] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "review" | "paid">("all");
  const [query, setQuery] = useState("");
  const [expandedId, setExpandedId] = useState("");
  const [logKey, setLogKey] = useState(0);
  const [payslipFor, setPayslipFor] = useState<PayrollRow | null>(null);
  const [incentiveFor, setIncentiveFor] = useState<PayrollRow | null>(null);
  const allBranches = useBranches();
  const [year, month] = period.split("-").map(Number);
  const canManage = user.role === "admin" || user.role === "manager";
  const personalView = !canManage || viewMode === "mine";
  const loadRun = useRef(0);
  // A previously loaded month's figures must never appear under a new month.
  const payroll = loadedPayroll?.period === period ? loadedPayroll.data : null;
  const error = loadError?.period === period ? loadError.message : null;
  const initialLoading = !payroll && (loading || !error);

  const load = useCallback(async () => {
    const run = ++loadRun.current;
    setLoading(true);
    setLoadError(null);
    try {
      const data = await fetchPayrollMonth(year, month, branch || undefined);
      if (run !== loadRun.current) return;
      setLoadedPayroll({ period, data });
    } catch (failure) {
      if (run !== loadRun.current) return;
      const message = failure instanceof Error ? failure.message : "Could not load payroll";
      setLoadError({ period, message });
      onToast(message, "error");
    } finally {
      if (run === loadRun.current) setLoading(false);
    }
  }, [branch, month, onToast, period, year]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => {
      window.clearTimeout(timer);
      loadRun.current += 1;
    };
  }, [load]);

  const visibleItems = useMemo(
    () => personalView ? (payroll?.items ?? []).filter((row) => row.employee_id === user.id) : payroll?.items ?? [],
    [payroll, personalView, user.id],
  );

  const totals = useMemo(
    () =>
      visibleItems.reduce(
        (sum, row) => ({
          gross: sum.gross + row.monthly_salary,
          deduction: sum.deduction + row.deduction,
          net: sum.net + row.total_payable,
          paid: sum.paid + Number(row.status === "paid"),
        }),
        { gross: 0, deduction: 0, net: 0, paid: 0 },
      ),
    [visibleItems],
  );

  const filteredItems = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return visibleItems.filter((row) => {
      if (statusFilter === "paid" && row.status !== "paid") return false;
      if (statusFilter === "review" && row.status === "paid") return false;
      if (!needle) return true;
      return [row.name, row.staff_code, row.branch].some((value) => value?.toLowerCase().includes(needle));
    });
  }, [query, statusFilter, visibleItems]);

  const savePolicy = async (row: PayrollRow, patch: Partial<PayrollRow>) => {
    setBusyId(row.employee_id);
    try {
      await updatePayrollPolicy(row.employee_id, {
        monthly_salary: Number(patch.monthly_salary ?? row.monthly_salary),
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
          <span className="payroll-kicker"><WalletCards size={14} /> {personalView ? "My payroll" : "Monthly payroll run"}</span>
          <h1>{monthLabel(period)}</h1>
          <p>{personalView ? "Your private salary, attendance allowance and deduction breakdown." : "Review attendance deductions, confirm net pay, then mark each employee as paid."}</p>
        </div>
        <div className="payroll-toolbar">
          {user.role === "manager" && <div className="scope-switch" aria-label="Payroll view">
            <button type="button" className={viewMode === "mine" ? "is-active" : ""} onClick={() => setViewMode("mine")}>My payroll</button>
            <button type="button" className={viewMode === "team" ? "is-active" : ""} onClick={() => setViewMode("team")}>Team payroll</button>
          </div>}
          <label>
            Pay period
            <DatePicker mode="month" value={period} onChange={setPeriod} ariaLabel="Pay period" clearable={false} />
          </label>
          {canManage && !personalView && payroll ? (
            <BranchSwitch
              value={branch}
              onChange={setBranch}
              // Admins always see both branches; a manager only what they run.
              branches={user.role === "admin" ? branchOptions([...allBranches, ...payroll.branches], false) : branchOptions(payroll.branches, false)}
              allowAll={user.role === "admin" || payroll.branches.length > 1}
              ariaLabel="Payroll branch"
            />
          ) : null}
          <button type="button" className="ds-ghost-btn" onClick={() => void load()} disabled={loading || initialLoading}>
            <RefreshCw size={15} className={loading ? "icon-spin" : ""} /> Refresh
          </button>
        </div>
      </header>

      {error && (
        <div className="db-card db-feedback is-error" role="alert">
          <span className="db-feedback-icon" aria-hidden="true"><AlertTriangle size={20} /></span>
          <div><strong>{payroll ? "Payroll could not be refreshed" : "Payroll could not be loaded"}</strong><span>{error}</span>{payroll && <span>Showing the last loaded values for this month. Refresh before confirming payments.</span>}</div>
          <button type="button" className="ds-ghost-btn" onClick={() => void load()} disabled={loading}><RefreshCw size={15} /> Retry</button>
        </div>
      )}

      {loading && payroll && (
        <div className="db-card db-feedback is-loading" role="status" aria-live="polite">
          <RefreshCw size={20} className="icon-spin" aria-hidden="true" />
          <div><strong>Refreshing payroll</strong><span>Showing the last loaded values while this month is updated.</span></div>
        </div>
      )}

      {payroll && <div className={`payroll-summary-grid ${personalView ? "is-personal" : ""}`}>
        <SummaryCard label={personalView ? "Gross salary" : "Gross payroll"} value={money.format(totals.gross)} note="Before deductions" icon={<Banknote />} tone="blue" />
        <SummaryCard label="Attendance deductions" value={money.format(totals.deduction)} note="LOP and uncovered time" icon={<Clock3 />} tone="amber" />
        <SummaryCard label="Net payable" value={money.format(totals.net)} note="Final amount for this month" icon={<WalletCards />} tone="green" />
        {!personalView && <SummaryCard label="Payment progress" value={`${totals.paid} of ${visibleItems.length}`} note="Employees marked paid" icon={<CheckCircle2 />} tone="violet" />}
      </div>}

      <section className="payroll-policy-strip">
        <span><CalendarRange size={17} /><strong>480-minute workday</strong> 10:00 AM–7:00 PM with a one-hour break, unless a custom work timing is set</span>
        <span><ShieldCheck size={17} /><strong>Monthly allowance</strong> 60-minute grace and one paid leave</span>
      </section>

      <section className="payroll-run">
        <div className="payroll-section-head">
          <div>
            <span className="payroll-section-icon"><Banknote size={18} /></span>
            <div><h2>{personalView ? "My salary breakdown" : "Employee pay breakdown"}</h2><p>Attendance allowance and uncovered time determine the final salary.</p></div>
          </div>
          {!personalView && payroll && <span className="payroll-count">{visibleItems.length} employees</span>}
        </div>

        {initialLoading ? (
          <div className="payroll-empty" role="status" aria-live="polite" aria-busy="true"><RefreshCw className="icon-spin" aria-hidden="true" /><strong>Preparing payroll</strong><span>Calculating attendance and salary details…</span></div>
        ) : !payroll ? (
          <div className="payroll-empty"><AlertTriangle aria-hidden="true" /><strong>Payroll is unavailable</strong><span>Retry the request above to load this month’s salary details.</span></div>
        ) : !visibleItems.length ? (
          <div className="payroll-empty"><WalletCards /><strong>No active employees</strong><span>Add staff before generating payroll.</span></div>
        ) : personalView ? (
          <div className="payroll-card-grid is-single">
            {visibleItems.map((row) => (
              <EmployeePayCard
                key={row.employee_id}
                row={row}
                busy={busyId === row.employee_id || loading}
                canManage={false}
                payslipHref={payroll.payslips_available && row.payslip_issued ? payslipUrl(year, month, row.employee_id) : undefined}
                onSave={savePolicy}
                onTogglePaid={togglePaid}
              />
            ))}
          </div>
        ) : (
          <>
            <div className="payroll-filter-bar">
              <div className="ds-tabs" role="tablist" aria-label="Filter by payment status">
                {([
                  ["all", "All", visibleItems.length],
                  ["review", "Awaiting payment", visibleItems.length - totals.paid],
                  ["paid", "Paid", totals.paid],
                ] as const).map(([id, label, count]) => (
                  <button key={id} type="button" role="tab" aria-selected={statusFilter === id} className={`ds-tab ${statusFilter === id ? "is-on" : ""}`} onClick={() => setStatusFilter(id)}>
                    {label}<span className="ds-tab-count">{count}</span>
                  </button>
                ))}
              </div>
              <label className="payroll-search">
                <Search size={15} aria-hidden="true" />
                <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search name, code or branch" aria-label="Search employees" />
              </label>
            </div>
            {!filteredItems.length ? (
              <div className="payroll-empty is-compact"><Search /><strong>No employees match</strong><span>Change the filter or search to see more.</span></div>
            ) : (
              <div className="payroll-table-wrap">
                <table className="payroll-table">
                  <thead>
                    <tr>
                      <th>Employee</th>
                      <th>Working days</th>
                      <th className="is-num">Gross</th>
                      <th className="is-num">Deductions</th>
                      <th className="is-num">Net payable</th>
                      <th>Status</th>
                      <th className="is-actions">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredItems.map((row) => {
                      const paid = row.status === "paid";
                      const open = expandedId === row.employee_id;
                      const busy = busyId === row.employee_id || loading;
                      return (
                        <Fragment key={row.employee_id}>
                          <tr className={open ? "is-open" : undefined}>
                            <td>
                              <span className="payroll-who">
                                <span className={`payroll-avatar ${paid ? "is-paid" : ""}`}>{initialsOf(row.name)}</span>
                                <span><strong>{row.name}</strong><small>{row.staff_code || "Employee"} · <span className={row.branch ? "" : "payroll-branch-unset"}>{row.branch || "Unassigned"}</span></small></span>
                              </span>
                            </td>
                            <td>{row.attendance_tracked === false ? <span className="payroll-extras-sub">Payroll only</span> : `${row.required_working_days} / ${row.calendar_days}`}</td>
                            <td className="is-num">{money.format(row.monthly_salary)}</td>
                            <td className={`is-num ${row.deduction > 0 ? "is-deduction" : ""}`}>{row.deduction > 0 ? `− ${money.format(row.deduction)}` : money.format(0)}</td>
                            <td className="is-num is-net">
                              {money.format(row.total_payable)}
                              {row.net_payable_overridden && <small className="payroll-extras-sub" title={row.override_remarks ?? ""}>Overridden</small>}
                            </td>
                            <td>
                              <span className={`payroll-state ${paid ? "is-paid" : "is-review"}`}>
                                {paid ? <CheckCircle2 size={13} /> : <Clock3 size={13} />}
                                {paid ? "Paid" : "To review"}
                              </span>
                            </td>
                            <td className="is-actions">
                              <div className="payroll-row-actions">
                                <button type="button" className={paid ? "payroll-reopen-btn" : "payroll-pay-btn"} disabled={busy} onClick={() => void togglePaid(row)}>
                                  {busy ? <RefreshCw size={14} className="icon-spin" /> : paid ? <RotateCcw size={14} /> : <Check size={14} />}
                                  {paid ? "Reopen" : "Mark paid"}
                                </button>
                                <button type="button" className="payroll-expand-btn" onClick={() => setIncentiveFor(row)} title="Add or view this month's incentives">
                                  <Gift size={15} /> Incentive{row.incentive_amount > 0 ? ` · ${money.format(row.incentive_amount)}` : ""}
                                </button>
                                {payroll.payslips_available && (
                                  <button type="button" className="payroll-expand-btn" onClick={() => setPayslipFor(row)} title="Enter the payment date and incentives, then download the payslip (PDF)">
                                    <Download size={15} /> Payslip
                                  </button>
                                )}
                                <button type="button" className="payroll-expand-btn" aria-expanded={open} onClick={() => setExpandedId(open ? "" : row.employee_id)}>
                                  Details <ChevronDown size={15} />
                                </button>
                              </div>
                            </td>
                          </tr>
                          {open && (
                            <tr className="payroll-detail-row">
                              <td colSpan={7}>
                                <PayrollBreakdown row={row} />
                                <SalarySetting key={`${row.employee_id}:${row.monthly_salary}`} row={row} busy={busy} onSave={savePolicy} />
                                {payroll.can_override && (
                                  <NetPayableOverrideForm
                                    key={`${row.employee_id}:${row.total_payable}`}
                                    row={row}
                                    year={year}
                                    month={month}
                                    onToast={onToast}
                                    onSaved={() => { setLogKey((key) => key + 1); void load(); }}
                                  />
                                )}
                              </td>
                            </tr>
                          )}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>

      {canManage && !personalView && payroll && <NetPayableLogs year={year} month={month} refreshKey={logKey} />}

      <ReimbursementsPanel currentUserId={user.id} onToast={onToast} onChanged={() => void load()} />

      {incentiveFor && (
        <IncentiveDialog
          row={incentiveFor}
          year={year}
          month={month}
          onClose={() => setIncentiveFor(null)}
          onChanged={() => { setLogKey((key) => key + 1); void load(); }}
          onToast={onToast}
        />
      )}

      {payslipFor && (
        <PayslipDialog
          row={payslipFor}
          year={year}
          month={month}
          onClose={() => setPayslipFor(null)}
          onSaved={() => void load()}
          onToast={onToast}
        />
      )}
    </div>
  );
}

function initialsOf(name: string): string {
  return name.split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase() || "ST";
}

/** The personal view: one employee, so the full card layout. */
function EmployeePayCard({ row, busy, canManage, payslipHref, onSave, onTogglePaid }: {
  row: PayrollRow;
  busy: boolean;
  canManage: boolean;
  /** Present from the last day of the month, when the payslip exists. */
  payslipHref?: string;
  onSave: (row: PayrollRow, patch: Partial<PayrollRow>) => Promise<void>;
  onTogglePaid: (row: PayrollRow) => Promise<void>;
}) {
  const paid = row.status === "paid";

  return (
    <article className={`payroll-card ${paid ? "is-paid" : ""}`}>
      <header className="payroll-card-head">
        <span className="payroll-avatar">{initialsOf(row.name)}</span>
        <div>
          <h3>{row.name}</h3>
          <p>
            {row.staff_code || "Employee"}
            {" · "}
            <span className={row.branch ? "" : "payroll-branch-unset"}>
              {row.branch || "Unassigned"}
            </span>
          </p>
        </div>
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
        <div className="is-net"><span>Net payable</span><strong>{money.format(row.total_payable)}</strong></div>
      </div>
      {(row.incentive_amount > 0 || row.reimbursement_amount > 0 || row.net_payable_overridden) && (
        <p className="payroll-extras-sub">
          {[
            row.incentive_amount > 0 ? `Includes incentives ${money.format(row.incentive_amount)}` : "",
            row.reimbursement_amount > 0 ? `reimbursements ${money.format(row.reimbursement_amount)}` : "",
            row.net_payable_overridden ? `adjusted by ${row.overridden_by_name || "an admin"}: ${row.override_remarks ?? ""}` : "",
          ].filter(Boolean).join(" · ")}
        </p>
      )}

      <PayrollBreakdown row={row} />

      {canManage && <SalarySetting key={`${row.employee_id}:${row.monthly_salary}`} row={row} busy={busy} onSave={onSave} />}

      <footer className="payroll-card-foot">
        {payslipHref && <a className="payroll-expand-btn" href={payslipHref}><Download size={15} /> Download payslip</a>}
        {!payslipHref && <span className="payroll-extras-sub">The payslip is available once the salary payment is recorded at month end.</span>}
        <span>{paid ? "Payment confirmed for this period" : canManage ? "Review the calculation before confirming payment" : "Payment is awaiting manager confirmation"}</span>
        {canManage && <button type="button" className={paid ? "payroll-reopen-btn" : "payroll-pay-btn"} disabled={busy} onClick={() => void onTogglePaid(row)}>
          {busy ? <RefreshCw size={15} className="icon-spin" /> : paid ? <RotateCcw size={15} /> : <Check size={15} />}
          {paid ? "Reopen payroll" : "Mark as paid"}
        </button>}
      </footer>
    </article>
  );
}

/** How the month's attendance turned into the deduction, grouped by topic. */
function PayrollBreakdown({ row }: { row: PayrollRow }) {
  return (
    <div className="payroll-breakdown">
      <div className="payroll-breakdown-group">
        <h4>Attendance</h4>
        {row.attendance_tracked === false ? (
          <Metric label="Attendance" value="Not tracked (payroll only)" />
        ) : (
          <>
            <Metric label="Working days" value={`${row.required_working_days} / ${row.calendar_days}`} />
            <Metric label="Paid leave" value={`${row.paid_leave_days} / 1 day`} />
            <Metric label="Grace used" value={`${row.grace_minutes} / 60 min`} />
          </>
        )}
      </div>
      <div className="payroll-breakdown-group">
        <h4>Overtime</h4>
        <Metric label="Approved extra OT" value={`${row.approved_ot_minutes} min`} />
        <Metric label="Set against late time" value={`${row.ot_offset_minutes} min`} />
        <Metric label="OT left over (unpaid)" value={`${row.paid_ot_minutes} min`} />
      </div>
      <div className="payroll-breakdown-group">
        <h4>Additions</h4>
        <Metric label="Incentives" value={money.format(row.incentive_amount)} />
        <Metric label="Reimbursements" value={money.format(row.reimbursement_amount)} />
        <Metric label="Calculated payable" value={money.format(row.computed_payable)} />
      </div>
      <div className="payroll-breakdown-group">
        <h4>Deductions</h4>
        <Metric label="Unpaid time" value={`${row.unpaid_minutes} min`} warn={row.unpaid_minutes > 0} />
        <Metric label="Daily LOP rate" value={money.format(row.daily_lop_rate)} />
        <Metric label="Total deducted" value={money.format(row.deduction)} warn={row.deduction > 0} />
      </div>
    </div>
  );
}

function SalarySetting({ row, busy, onSave }: {
  row: PayrollRow;
  busy: boolean;
  onSave: (row: PayrollRow, patch: Partial<PayrollRow>) => Promise<void>;
}) {
  const [salary, setSalary] = useState(String(row.monthly_salary));
  const changed = Number(salary) !== row.monthly_salary;

  return (
    <div className="payroll-settings">
      <div className="payroll-settings-title"><Settings2 size={15} /> Salary setting</div>
      <div className="payroll-settings-inline">
        <label>Monthly salary<input type="number" min="0" value={salary} disabled={busy} onChange={(event) => setSalary(event.target.value)} /></label>
        <span>{changed ? "Unsaved changes" : "Up to date"}</span>
        <button type="button" className="payroll-save-btn" disabled={busy || !changed || Number(salary) < 0} onClick={() => void onSave(row, { monthly_salary: Number(salary) || 0 })}>
          {busy ? <RefreshCw size={15} className="icon-spin" /> : <Check size={15} />} Save salary
        </button>
      </div>
    </div>
  );
}

function SummaryCard({ label, value, note, icon, tone }: { label: string; value: string; note: string; icon: ReactNode; tone: string }) {
  return <article className={`payroll-summary is-${tone}`}><span className="payroll-summary-icon">{icon}</span><div><span>{label}</span><strong>{value}</strong><small>{note}</small></div></article>;
}

function Metric({ label, value, warn = false }: { label: string; value: string; warn?: boolean }) {
  return <div className={warn ? "is-warn" : ""}><span>{label}</span><strong>{value}</strong></div>;
}
