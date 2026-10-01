"use client";

/**
 * Payroll additions around the monthly run: staff reimbursement claims (decided
 * by the super admin), the admin net-payable override with its remarks, and
 * the log of every override.
 */

import { useCallback, useEffect, useState } from "react";
import { Check, Download, FileText, Gift, History, Paperclip, PencilLine, Plus, Receipt, RefreshCw, RotateCcw, Send, Trash2, X } from "lucide-react";

import {
  addIncentive,
  decideReimbursement,
  deleteIncentive,
  fetchIncentives,
  fetchNetPayableLogs,
  fetchReimbursements,
  overrideNetPayable,
  payslipUrl,
  reimbursementAttachmentUrl,
  savePayslipDetails,
  submitReimbursement,
  type NetPayableLog,
  type PayrollIncentive,
  type PayrollRow,
  type Reimbursement,
} from "@/lib/api";

const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });

function todayIso(): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
}

function when(iso?: string): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-IN", { dateStyle: "medium", timeStyle: "short", timeZone: "Asia/Kolkata" }).format(new Date(iso));
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// --------------------------------------------------------------------------- //
//  Reimbursements
// --------------------------------------------------------------------------- //
export function ReimbursementsPanel({ currentUserId, onToast, onChanged }: {
  currentUserId: string;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
  /** Called after a decision, so the payroll figures can be reloaded. */
  onChanged: () => void;
}) {
  const [items, setItems] = useState<Reimbursement[]>([]);
  const [canDecide, setCanDecide] = useState(false);
  const [loading, setLoading] = useState(true);
  const [amount, setAmount] = useState("");
  const [expenseDate, setExpenseDate] = useState(todayIso());
  const [description, setDescription] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [fileKey, setFileKey] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [deciding, setDeciding] = useState("");
  const [notes, setNotes] = useState<Record<string, string>>({});

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await fetchReimbursements();
      setItems(result.items);
      setCanDecide(result.can_decide);
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not load reimbursements", "error");
    } finally {
      setLoading(false);
    }
  }, [onToast]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const valid = Number(amount) > 0 && description.trim().length > 0 && Boolean(expenseDate);

  const submit = async () => {
    if (!valid || submitting) return;
    setSubmitting(true);
    try {
      await submitReimbursement({ amount: Number(amount), description: description.trim(), expense_date: expenseDate, attachment: file });
      onToast("Reimbursement sent to Yoosuf for approval", "success");
      setAmount("");
      setDescription("");
      setFile(null);
      setFileKey((key) => key + 1);
      await load();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not submit the reimbursement", "error");
    } finally {
      setSubmitting(false);
    }
  };

  const decide = async (claim: Reimbursement, approved: boolean) => {
    setDeciding(claim.id);
    try {
      await decideReimbursement(claim.id, approved, notes[claim.id] ?? "");
      onToast(approved ? `Approved — ${money.format(claim.amount)} added to ${claim.employee_name}'s net payable` : "Reimbursement rejected", "success");
      await load();
      onChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not record the decision", "error");
    } finally {
      setDeciding("");
    }
  };

  const pending = items.filter((claim) => claim.status === "pending").length;

  return (
    <section className="payroll-run payroll-extras">
      <div className="payroll-section-head">
        <div>
          <span className="payroll-section-icon"><Receipt size={18} /></span>
          <div>
            <h2>Reimbursements</h2>
            <p>Claim an expense with an optional bill photo or document. Once Yoosuf approves, the amount is added to that month&rsquo;s net payable salary.</p>
          </div>
        </div>
        {pending > 0 && <span className="payroll-count">{pending} pending</span>}
      </div>

      <div className="payroll-settings">
        <div className="payroll-settings-title"><Send size={15} /> New claim</div>
        <div className="payroll-extras-form">
          <label>Amount (₹)<input type="number" min="1" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} disabled={submitting} /></label>
          <label>Expense date<input type="date" value={expenseDate} onChange={(event) => setExpenseDate(event.target.value)} disabled={submitting} /></label>
          <label className="is-wide">What was it for?<input value={description} maxLength={1000} onChange={(event) => setDescription(event.target.value)} placeholder="e.g. Courier charges for passports" disabled={submitting} /></label>
          <label className="is-wide">Bill photo or document (optional)
            <input key={fileKey} type="file" accept="image/*,.pdf,.doc,.docx,.xls,.xlsx" onChange={(event) => setFile(event.target.files?.[0] ?? null)} disabled={submitting} />
          </label>
          <button type="button" className="payroll-save-btn" disabled={!valid || submitting} onClick={() => void submit()}>
            {submitting ? <RefreshCw size={15} className="icon-spin" /> : <Send size={15} />} Submit for approval
          </button>
        </div>
      </div>

      {loading ? (
        <div className="payroll-empty is-compact" role="status"><RefreshCw className="icon-spin" /><strong>Loading claims</strong></div>
      ) : !items.length ? (
        <div className="payroll-empty is-compact"><Receipt /><strong>No reimbursement claims yet</strong></div>
      ) : (
        <div className="payroll-table-wrap">
          <table className="payroll-table">
            <thead>
              <tr>
                <th>Employee</th>
                <th>Expense</th>
                <th className="is-num">Amount</th>
                <th>Bill</th>
                <th>Status</th>
                {canDecide && <th className="is-actions">Decision</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((claim) => (
                <tr key={claim.id}>
                  <td><strong>{claim.employee_id === currentUserId ? "You" : claim.employee_name}</strong><small className="payroll-extras-sub">Claimed {when(claim.created_at)}</small></td>
                  <td>{claim.description}<small className="payroll-extras-sub">Spent on {claim.expense_date}</small></td>
                  <td className="is-num">{money.format(claim.amount)}</td>
                  <td>
                    {claim.attachment
                      ? <a className="payroll-extras-link" href={reimbursementAttachmentUrl(claim.id)} target="_blank" rel="noreferrer"><Paperclip size={13} /> {claim.attachment.filename}</a>
                      : <span className="payroll-extras-sub">None</span>}
                  </td>
                  <td>
                    <span className={`payroll-state ${claim.status === "approved" ? "is-paid" : claim.status === "rejected" ? "is-rejected" : "is-review"}`}>
                      {claim.status === "approved" ? "Approved" : claim.status === "rejected" ? "Rejected" : "Pending"}
                    </span>
                    {claim.status === "approved" && claim.payroll_month && (
                      <small className="payroll-extras-sub">Paid in {MONTHS[claim.payroll_month - 1]} {claim.payroll_year}</small>
                    )}
                    {claim.decision_note && <small className="payroll-extras-sub">“{claim.decision_note}”</small>}
                  </td>
                  {canDecide && (
                    <td className="is-actions">
                      {claim.status === "pending" ? (
                        <div className="payroll-extras-decide">
                          <input placeholder="Note (optional)" value={notes[claim.id] ?? ""} onChange={(event) => setNotes((current) => ({ ...current, [claim.id]: event.target.value }))} />
                          <button type="button" className="payroll-pay-btn" disabled={deciding === claim.id} onClick={() => void decide(claim, true)}><Check size={14} /> Approve</button>
                          <button type="button" className="payroll-reopen-btn" disabled={deciding === claim.id} onClick={() => void decide(claim, false)}><X size={14} /> Reject</button>
                        </div>
                      ) : <span className="payroll-extras-sub">{claim.decided_by_name ? `By ${claim.decided_by_name}` : "Decided"}</span>}
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

// --------------------------------------------------------------------------- //
//  Net payable override
// --------------------------------------------------------------------------- //
export function NetPayableOverrideForm({ row, year, month, onSaved, onToast }: {
  row: PayrollRow;
  year: number;
  month: number;
  onSaved: () => void;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
}) {
  const [amount, setAmount] = useState(String(row.total_payable));
  const [remarks, setRemarks] = useState("");
  const [busy, setBusy] = useState(false);
  const ready = remarks.trim().length >= 3 && amount !== "" && Number(amount) >= 0;

  const save = async (clear: boolean) => {
    setBusy(true);
    try {
      await overrideNetPayable(year, month, row.employee_id, clear ? null : Number(amount), remarks.trim());
      onToast(clear ? `${row.name}'s net payable reset to the calculated amount` : `${row.name}'s net payable set to ${money.format(Number(amount))}`, "success");
      setRemarks("");
      onSaved();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not override net payable", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="payroll-settings">
      <div className="payroll-settings-title"><PencilLine size={15} /> Override net payable (admins)</div>
      <p className="payroll-extras-sub">
        Calculated: {money.format(row.computed_payable)}
        {row.net_payable_overridden && ` · Overridden to ${money.format(row.total_payable)} by ${row.overridden_by_name || "an admin"}: “${row.override_remarks ?? ""}”`}
      </p>
      <div className="payroll-extras-form">
        <label>Net payable (₹)<input type="number" min="0" step="0.01" value={amount} disabled={busy} onChange={(event) => setAmount(event.target.value)} /></label>
        <label className="is-wide">Remarks (required)<input value={remarks} maxLength={1000} disabled={busy} onChange={(event) => setRemarks(event.target.value)} placeholder="Why is the amount being changed?" /></label>
        <button type="button" className="payroll-save-btn" disabled={!ready || busy} onClick={() => void save(false)}>
          {busy ? <RefreshCw size={15} className="icon-spin" /> : <Check size={15} />} Save override
        </button>
        {row.net_payable_overridden && (
          <button type="button" className="payroll-reopen-btn" disabled={remarks.trim().length < 3 || busy} onClick={() => void save(true)}>
            <RotateCcw size={15} /> Reset to calculated
          </button>
        )}
      </div>
    </div>
  );
}

export function NetPayableLogs({ year, month, refreshKey }: { year: number; month: number; refreshKey: number }) {
  const [items, setItems] = useState<NetPayableLog[] | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    let live = true;
    fetchNetPayableLogs(year, month)
      .then((result) => { if (live) setItems(result.items); })
      .catch(() => { if (live) setItems([]); });
    return () => {
      live = false;
    };
  }, [month, open, refreshKey, year]);

  return (
    <section className="payroll-run payroll-extras">
      <div className="payroll-section-head">
        <div>
          <span className="payroll-section-icon"><History size={18} /></span>
          <div><h2>Net payable override log</h2><p>Every change an admin made to this month&rsquo;s net payable, with their remarks.</p></div>
        </div>
        <button type="button" className="payroll-expand-btn" aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Hide" : "Show"} log
        </button>
      </div>
      {open && (items === null ? (
        <div className="payroll-empty is-compact" role="status"><RefreshCw className="icon-spin" /><strong>Loading</strong></div>
      ) : !items.length ? (
        <div className="payroll-empty is-compact"><FileText /><strong>No overrides this month</strong></div>
      ) : (
        <div className="payroll-table-wrap">
          <table className="payroll-table">
            <thead>
              <tr><th>When</th><th>Employee</th><th className="is-num">Calculated</th><th className="is-num">Before</th><th className="is-num">After</th><th>Remarks</th><th>By</th></tr>
            </thead>
            <tbody>
              {items.map((entry) => (
                <tr key={entry.id}>
                  <td>{when(entry.created_at)}</td>
                  <td>{entry.employee_name}</td>
                  <td className="is-num">{money.format(entry.computed_payable)}</td>
                  <td className="is-num">{money.format(entry.previous_payable)}</td>
                  <td className="is-num">{entry.cleared ? "Reset" : money.format(entry.new_payable)}</td>
                  <td>{entry.remarks}</td>
                  <td>{entry.actor_name || entry.actor_email}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

// --------------------------------------------------------------------------- //
//  Payslip: asks for the payment date and incentives, then downloads
// --------------------------------------------------------------------------- //
export function PayslipDialog({ row, year, month, onClose, onSaved, onToast }: {
  row: PayrollRow;
  year: number;
  month: number;
  onClose: () => void;
  onSaved: () => void;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
}) {
  const [paymentDate, setPaymentDate] = useState(row.payment_date ?? todayIso());
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (!paymentDate || busy) return;
    setBusy(true);
    try {
      await savePayslipDetails(year, month, row.employee_id, { payment_date: paymentDate });
      window.location.assign(payslipUrl(year, month, row.employee_id));
      onToast(`${row.name}'s payslip is downloading`, "success");
      onSaved();
      onClose();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not prepare the payslip", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay active" onClick={() => !busy && onClose()}>
      <div className="modal-container is-narrow" role="dialog" aria-modal="true" aria-labelledby="payslip-title" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title" id="payslip-title">Payslip for {MONTHS[month - 1]} {year}</h2>
            <p className="modal-subtitle">{row.name}</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} disabled={busy} aria-label="Close"><X size={16} /></button>
        </div>
        <div className="modal-body payroll-settings">
          <label>Date of payment<input type="date" value={paymentDate} onChange={(event) => setPaymentDate(event.target.value)} disabled={busy} /></label>

          <div className="payroll-settings-title">Incentives this month</div>
          {row.incentives.length === 0
            ? <p className="payroll-extras-sub">None. Add incentives in the Incentives section below the payroll table.</p>
            : (
              <ul className="payroll-payslip-incentives">
                {row.incentives.map((item) => (
                  <li key={item.id}><strong>{money.format(item.amount)}</strong> · {item.remarks || "No remarks"} <span className="payroll-extras-sub">{item.incentive_date}</span></li>
                ))}
              </ul>
            )}

          <div className="payroll-breakdown-group payroll-payslip-sum">
            <div><span>Salary after deductions</span><strong>{money.format(row.net_salary)}</strong></div>
            <div><span>Incentives</span><strong>{money.format(row.incentive_amount)}</strong></div>
            <div><span>Reimbursements</span><strong>{money.format(row.reimbursement_amount)}</strong></div>
            <div className="is-total"><span>Net salary</span><strong>{money.format(row.total_payable)}</strong></div>
            {row.net_payable_overridden && (
              <p className="payroll-extras-sub">
                Net payable is overridden by {row.overridden_by_name || "an admin"}: {row.override_remarks}
              </p>
            )}
          </div>
        </div>
        <div className="modal-footer">
          <button type="button" className="modal-cancel-btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" className="db-btn is-primary" onClick={() => void save()} disabled={!paymentDate || busy}>
            {busy ? <RefreshCw size={14} className="icon-spin" /> : <Download size={14} />} Save &amp; download payslip
          </button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  Incentives
// --------------------------------------------------------------------------- //
export function IncentivesPanel({ year, month, currentUserId, refreshKey, onChanged, onToast }: {
  year: number;
  month: number;
  currentUserId: string;
  refreshKey: number;
  /** Called after a removal, so net payable can be reloaded. */
  onChanged: () => void;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
}) {
  const [items, setItems] = useState<PayrollIncentive[] | null>(null);
  const [canManage, setCanManage] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await fetchIncentives(year, month);
      setItems(result.items);
      setCanManage(result.can_manage);
    } catch (err) {
      setItems([]);
      onToast(err instanceof Error ? err.message : "Could not load incentives", "error");
    }
  }, [month, onToast, year]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load, refreshKey]);

  const remove = async (item: PayrollIncentive) => {
    if (!window.confirm(`Remove the ${money.format(item.amount)} incentive for ${item.employee_name}?`)) return;
    try {
      await deleteIncentive(year, month, item.id);
      onToast("Incentive removed", "success");
      await load();
      onChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not remove the incentive", "error");
    }
  };

  const total = (items ?? []).reduce((sum, item) => sum + item.amount, 0);

  return (
    <section className="payroll-run payroll-extras">
      <div className="payroll-section-head">
        <div>
          <span className="payroll-section-icon"><Gift size={18} /></span>
          <div>
            <h2>Incentives — {MONTHS[month - 1]} {year}</h2>
            <p>{canManage
              ? "Add incentives with the Incentive button on each employee's row. They are added to the month's net payable and listed on the payslip."
              : "Incentives added to your salary this month."}</p>
          </div>
        </div>
        {items && items.length > 0 && <span className="payroll-count">{items.length} · {money.format(total)}</span>}
      </div>

      {items === null ? (
        <div className="payroll-empty is-compact" role="status"><RefreshCw className="icon-spin" /><strong>Loading incentives</strong></div>
      ) : !items.length ? (
        <div className="payroll-empty is-compact"><Gift /><strong>No incentives this month</strong></div>
      ) : (
        <div className="payroll-table-wrap">
          <table className="payroll-table">
            <thead>
              <tr>
                <th>Employee</th>
                <th>Date</th>
                <th className="is-num">Amount</th>
                <th>Remarks</th>
                <th>Added by</th>
                {canManage && <th className="is-actions">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id}>
                  <td><strong>{item.employee_id === currentUserId ? "You" : item.employee_name}</strong></td>
                  <td>{item.incentive_date}</td>
                  <td className="is-num">{money.format(item.amount)}</td>
                  <td>{item.remarks || "—"}</td>
                  <td>{item.created_by_name || "—"}</td>
                  {canManage && (
                    <td className="is-actions">
                      <button type="button" className="payroll-reopen-btn" onClick={() => void remove(item)} aria-label={`Remove incentive for ${item.employee_name}`}>
                        <Trash2 size={14} /> Remove
                      </button>
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
 * One employee's incentives for the month, opened from their payroll row.
 * Any number can be added, each with its own amount, date and remarks.
 */
export function IncentiveDialog({ row, year, month, onClose, onChanged, onToast }: {
  row: PayrollRow;
  year: number;
  month: number;
  onClose: () => void;
  onChanged: () => void;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
}) {
  const [items, setItems] = useState<PayrollIncentive[] | null>(null);
  const [amount, setAmount] = useState("");
  const [incentiveDate, setIncentiveDate] = useState(todayIso());
  const [remarks, setRemarks] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await fetchIncentives(year, month);
      setItems(result.items.filter((item) => item.employee_id === row.employee_id));
    } catch (err) {
      setItems([]);
      onToast(err instanceof Error ? err.message : "Could not load incentives", "error");
    }
  }, [month, onToast, row.employee_id, year]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), 0);
    return () => window.clearTimeout(timer);
  }, [load]);

  const valid = Number(amount) > 0 && Boolean(incentiveDate);

  const add = async () => {
    if (!valid || busy) return;
    setBusy(true);
    try {
      await addIncentive(year, month, {
        employee_id: row.employee_id,
        amount: Number(amount),
        remarks: remarks.trim(),
        incentive_date: incentiveDate,
      });
      onToast(`Incentive of ${money.format(Number(amount))} added for ${row.name}`, "success");
      setAmount("");
      setRemarks("");
      await load();
      onChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not add the incentive", "error");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (item: PayrollIncentive) => {
    if (!window.confirm(`Remove the ${money.format(item.amount)} incentive?`)) return;
    setBusy(true);
    try {
      await deleteIncentive(year, month, item.id);
      onToast("Incentive removed", "success");
      await load();
      onChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not remove the incentive", "error");
    } finally {
      setBusy(false);
    }
  };

  const total = (items ?? []).reduce((sum, item) => sum + item.amount, 0);

  return (
    <div className="modal-overlay active" onClick={() => !busy && onClose()}>
      <div className="modal-container is-narrow" role="dialog" aria-modal="true" aria-labelledby="incentive-title" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title" id="incentive-title">Incentives — {MONTHS[month - 1]} {year}</h2>
            <p className="modal-subtitle">{row.name}{row.staff_code ? ` · ${row.staff_code}` : ""}</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} disabled={busy} aria-label="Close"><X size={16} /></button>
        </div>
        <div className="modal-body payroll-settings">
          <div className="payroll-extras-form">
            <label>Amount (₹)<input type="number" min="1" step="0.01" value={amount} onChange={(event) => setAmount(event.target.value)} disabled={busy} autoFocus /></label>
            <label>Date<input type="date" value={incentiveDate} onChange={(event) => setIncentiveDate(event.target.value)} disabled={busy} /></label>
            <label className="is-wide">Remarks<input value={remarks} maxLength={500} onChange={(event) => setRemarks(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void add(); }} placeholder="e.g. Interview incentive" disabled={busy} /></label>
            <button type="button" className="payroll-save-btn" disabled={!valid || busy} onClick={() => void add()}>
              {busy ? <RefreshCw size={15} className="icon-spin" /> : <Plus size={15} />} Add incentive
            </button>
          </div>

          <div className="payroll-settings-title">This month</div>
          {items === null ? (
            <p className="payroll-extras-sub">Loading…</p>
          ) : !items.length ? (
            <p className="payroll-extras-sub">No incentives yet. Add as many as needed above.</p>
          ) : (
            <table className="payroll-table">
              <thead>
                <tr><th>Date</th><th className="is-num">Amount</th><th>Remarks</th><th className="is-actions" /></tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.id}>
                    <td>{item.incentive_date}</td>
                    <td className="is-num">{money.format(item.amount)}</td>
                    <td>{item.remarks || "—"}<small className="payroll-extras-sub">Added by {item.created_by_name || "—"}</small></td>
                    <td className="is-actions">
                      <button type="button" className="payroll-reopen-btn" onClick={() => void remove(item)} disabled={busy} aria-label="Remove incentive"><Trash2 size={14} /></button>
                    </td>
                  </tr>
                ))}
                <tr className="payroll-incentive-total"><td>Total</td><td className="is-num">{money.format(total)}</td><td colSpan={2} /></tr>
              </tbody>
            </table>
          )}
        </div>
        <div className="modal-footer">
          <button type="button" className="db-btn is-primary" onClick={onClose} disabled={busy}><Check size={14} /> Done</button>
        </div>
      </div>
    </div>
  );
}
