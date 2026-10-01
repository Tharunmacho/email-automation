"use client";

/**
 * Payroll additions around the monthly run: staff reimbursement claims (decided
 * by the super admin), the admin net-payable override with its remarks, and
 * the log of every override.
 */

import { useCallback, useEffect, useState } from "react";
import { Check, Download, FileText, History, Paperclip, PencilLine, Plus, Receipt, RefreshCw, RotateCcw, Send, Trash2, X } from "lucide-react";

import {
  decideReimbursement,
  fetchNetPayableLogs,
  fetchReimbursements,
  overrideNetPayable,
  payslipUrl,
  reimbursementAttachmentUrl,
  savePayslipDetails,
  submitReimbursement,
  type NetPayableLog,
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
  const [incentives, setIncentives] = useState<{ amount: string; notes: string }[]>(
    row.incentives?.length
      ? row.incentives.map((item) => ({ amount: String(item.amount), notes: item.notes }))
      : [],
  );
  const [busy, setBusy] = useState(false);

  const incentiveTotal = incentives.reduce((sum, item) => sum + (Number(item.amount) || 0), 0);
  // What the month pays with these incentives instead of the ones saved before.
  const computed = row.computed_payable - row.incentive_amount + incentiveTotal;
  const invalid = !paymentDate || incentives.some((item) => !(Number(item.amount) > 0));

  const update = (index: number, patch: Partial<{ amount: string; notes: string }>) =>
    setIncentives((current) => current.map((item, i) => (i === index ? { ...item, ...patch } : item)));

  const save = async () => {
    if (invalid || busy) return;
    setBusy(true);
    try {
      await savePayslipDetails(year, month, row.employee_id, {
        payment_date: paymentDate,
        incentives: incentives.map((item) => ({ amount: Number(item.amount), notes: item.notes.trim() })),
      });
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

          <div className="payroll-settings-title">Incentives</div>
          {incentives.length === 0 && <p className="payroll-extras-sub">No incentives this month.</p>}
          {incentives.map((item, index) => (
            <div className="payroll-extras-form" key={index}>
              <label>Amount (₹)<input type="number" min="1" step="0.01" value={item.amount} onChange={(event) => update(index, { amount: event.target.value })} disabled={busy} /></label>
              <label className="is-wide">Notes<input value={item.notes} maxLength={300} onChange={(event) => update(index, { notes: event.target.value })} placeholder="e.g. Interview incentive" disabled={busy} /></label>
              <button type="button" className="payroll-reopen-btn" onClick={() => setIncentives((current) => current.filter((_, i) => i !== index))} disabled={busy} aria-label="Remove incentive"><Trash2 size={14} /></button>
            </div>
          ))}
          <button type="button" className="payroll-expand-btn" onClick={() => setIncentives((current) => [...current, { amount: "", notes: "" }])} disabled={busy}>
            <Plus size={14} /> Add incentive
          </button>

          <div className="payroll-breakdown-group payroll-payslip-sum">
            <div><span>Salary after deductions</span><strong>{money.format(row.net_salary)}</strong></div>
            {row.extra_ot_amount > 0 && <div><span>Extra OT</span><strong>{money.format(row.extra_ot_amount)}</strong></div>}
            <div><span>Incentives</span><strong>{money.format(incentiveTotal)}</strong></div>
            <div><span>Reimbursements</span><strong>{money.format(row.reimbursement_amount)}</strong></div>
            <div className="is-total"><span>Net salary</span><strong>{money.format(row.net_payable_overridden ? row.total_payable : computed)}</strong></div>
            {row.net_payable_overridden && (
              <p className="payroll-extras-sub">
                Net payable is overridden to {money.format(row.total_payable)} by {row.overridden_by_name || "an admin"}. Reset the override to include these incentives in the calculation.
              </p>
            )}
          </div>
        </div>
        <div className="modal-footer">
          <button type="button" className="modal-cancel-btn" onClick={onClose} disabled={busy}>Cancel</button>
          <button type="button" className="db-btn is-primary" onClick={() => void save()} disabled={invalid || busy}>
            {busy ? <RefreshCw size={14} className="icon-spin" /> : <Download size={14} />} Save &amp; download payslip
          </button>
        </div>
      </div>
    </div>
  );
}

