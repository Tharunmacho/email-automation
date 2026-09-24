"use client";

/**
 * The candidate's journey outside this office, and the controls that move it.
 *
 *   Shortlisted -> Submitted -> Interview -> Outcome -> Offer -> Placement
 *
 * Every control here maps to exactly one backend transition, and the backend
 * decides what is legal — this only offers the actions that make sense from the
 * state the candidate is actually in, so a reviewer is not invited to record an
 * interview for somebody who has not been submitted anywhere.
 *
 * The four statuses are kept visibly apart, because they are different facts:
 * the verdict panel next door is *our* opinion of the profile; everything here
 * is what a company or associate has done about them.
 */

import { useState } from "react";
import {
  Ban,
  Briefcase,
  CalendarClock,
  CheckCircle2,
  FileSignature,
  History,
  Lock,
  MapPinned,
  Send,
} from "lucide-react";

import {
  reassignCandidate,
  submitCandidate,
  updateCandidateInterview,
  updateCandidateOffer,
  updateCandidateOutcome,
  type CandidateRecord,
  type InterviewOutcome,
  type InterviewStatus,
  type Office,
  type OfferStatus,
  type RecruitmentStatus,
  type StaffMember,
} from "@/lib/api";

type Toast = (message: string, type?: "success" | "error" | "info") => void;

interface Props {
  candidate: CandidateRecord;
  /** Whether this account may move the pipeline. */
  canManage: boolean;
  /** Whether this account may reassign across countries, offices and desks. */
  canReassign: boolean;
  countries: { id: string; name: string }[];
  offices: Office[];
  staff: StaffMember[];
  onToast: Toast;
  /** Called with the updated record so the page can refresh what it holds. */
  onChanged: (record: CandidateRecord) => void;
}

const STATUS_LABEL: Record<RecruitmentStatus, string> = {
  available: "Available",
  submitted_to_company: "Submitted to company",
  submitted_to_associate: "Submitted to associate",
  interviewing: "Interviewing",
  interview_completed: "Interview completed",
  selected: "Selected",
  on_hold: "On hold",
  offer_issued: "Offer issued",
  offer_accepted: "Offer accepted",
};

const OUTCOME_LABEL: Record<InterviewOutcome, string> = {
  selected: "Selected",
  on_hold: "On hold",
  rejected: "Rejected",
  offer_declined: "Offer declined",
};

const INTERVIEW_LABEL: Record<InterviewStatus, string> = {
  pending: "Pending",
  scheduled: "Scheduled",
  completed: "Completed",
  unavailable: "Unavailable",
};

const SUBMITTED_STATES: RecruitmentStatus[] = [
  "submitted_to_company",
  "submitted_to_associate",
  "interviewing",
  "interview_completed",
];

function tone(status: RecruitmentStatus): string {
  if (status === "offer_accepted") return "is-ok";
  if (status === "selected" || status === "offer_issued") return "is-info";
  if (status === "on_hold") return "is-warn";
  return "is-info";
}

function when(value?: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleDateString("en-IN", {
    day: "2-digit", month: "short", year: "numeric",
  });
}

function whenExact(value?: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("en-IN", {
    day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function RecruitmentPanel({
  candidate, canManage, canReassign, countries, offices, staff, onToast, onChanged,
}: Props) {
  const status = (candidate.recruitment_status ?? "available") as RecruitmentStatus;
  const locked = Boolean(candidate.placement_locked);
  const history = candidate.recruitment_history ?? [];

  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<null | "submit" | "interview" | "outcome" | "offer" | "reassign">(null);

  // Submission
  const [targetType, setTargetType] = useState<"company" | "associate">("company");
  const [targetName, setTargetName] = useState("");
  const [jobOrderId, setJobOrderId] = useState("");
  const [submitNotes, setSubmitNotes] = useState("");
  // Interview
  const [interviewStatus, setInterviewStatus] = useState<InterviewStatus>("scheduled");
  const [interviewAt, setInterviewAt] = useState("");
  const [interviewNotes, setInterviewNotes] = useState("");
  // Outcome
  const [outcome, setOutcome] = useState<InterviewOutcome>("selected");
  const [outcomeNotes, setOutcomeNotes] = useState("");
  // Offer
  const [offerStatus, setOfferStatus] = useState<OfferStatus>("issued");
  const [offerNotes, setOfferNotes] = useState("");
  // Reassignment
  const [country, setCountry] = useState(candidate.profile?.destination_country ?? "");
  const [officeId, setOfficeId] = useState(candidate.office_id ?? "");
  const [staffId, setStaffId] = useState("");
  const [reassignReason, setReassignReason] = useState("");

  /** Run one transition, reporting the backend's own message on refusal. */
  const run = async (
    label: string,
    action: () => Promise<CandidateRecord>,
  ) => {
    setBusy(true);
    try {
      const updated = await action();
      onChanged(updated);
      onToast(label, "success");
      setOpen(null);
    } catch (error) {
      // The backend refuses duplicate submissions and anything against a locked
      // placement with an explanation; showing it verbatim is more use than a
      // generic failure.
      onToast(error instanceof Error ? error.message : "The change could not be saved", "error");
    } finally {
      setBusy(false);
    }
  };

  const doSubmit = () => {
    if (!targetName.trim()) return onToast("Name the company or associate", "info");
    return run(`Submitted to ${targetName.trim()}`, () =>
      submitCandidate(candidate.id, {
        target_type: targetType,
        target_name: targetName.trim(),
        job_order_id: jobOrderId.trim() || null,
        notes: submitNotes.trim(),
      }),
    );
  };

  const doInterview = () => {
    if (!interviewNotes.trim()) return onToast("Add a note for this interview update", "info");
    return run(`Interview marked ${INTERVIEW_LABEL[interviewStatus].toLowerCase()}`, () =>
      updateCandidateInterview(candidate.id, {
        status: interviewStatus,
        interview_at: interviewAt ? new Date(interviewAt).toISOString() : null,
        notes: interviewNotes.trim(),
      }),
    );
  };

  const doOutcome = () => {
    if (!outcomeNotes.trim()) return onToast("Record what the client said", "info");
    const returning = outcome === "rejected" || outcome === "offer_declined";
    return run(
      returning
        ? `${OUTCOME_LABEL[outcome]} — returned to the available pool`
        : `Outcome recorded: ${OUTCOME_LABEL[outcome]}`,
      () => updateCandidateOutcome(candidate.id, { status: outcome, notes: outcomeNotes.trim() }),
    );
  };

  const doOffer = () => {
    if (!offerNotes.trim()) return onToast("Add a note for this offer update", "info");
    const message =
      offerStatus === "accepted"
        ? "Offer accepted — candidate locked to this placement"
        : offerStatus === "declined"
          ? "Offer declined — returned to the available pool"
          : "Offer issued";
    return run(message, () =>
      updateCandidateOffer(candidate.id, { status: offerStatus, notes: offerNotes.trim() }),
    );
  };

  const doReassign = async () => {
    if (!country.trim()) return onToast("Choose the new destination country", "info");
    if (!reassignReason.trim()) return onToast("A reassignment needs a reason", "info");
    setBusy(true);
    try {
      const result = await reassignCandidate(candidate.id, {
        destination_country: country,
        office_id: officeId || null,
        staff_id: staffId || null,
        reason: reassignReason.trim(),
      });
      onChanged(result.candidate);
      onToast(
        `Reassigned to ${result.reassignment.to_country}` +
          (result.reassignment.to_office_name ? ` · ${result.reassignment.to_office_name}` : ""),
        "success",
      );
      setReassignReason("");
      setOpen(null);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "Reassignment failed", "error");
    } finally {
      setBusy(false);
    }
  };

  // Which actions make sense from here. The backend is the authority; this just
  // avoids offering a button whose only outcome would be a refusal.
  const canSubmit = canManage && !locked;
  const canRecordInterview = canManage && !locked && SUBMITTED_STATES.includes(status);
  const canRecordOutcome = canManage && !locked && status === "interview_completed";
  const canIssueOffer = canManage && !locked && (status === "selected" || status === "on_hold");
  const canAnswerOffer = canManage && (status === "offer_issued" || locked);

  return (
    <section className="cprof-card recruit-panel" id="cprof-recruitment">
      <div className="recruit-head">
        <div>
          <h3 className="cprof-card-title">Recruitment pipeline</h3>
          <p className="recruit-sub">
            Where this candidate stands with a company or associate. Separate from your own
            verdict on the profile.
          </p>
        </div>
        <span className={`ds-status ${tone(status)}`}><i />{STATUS_LABEL[status] ?? status}</span>
      </div>

      {locked && (
        <p className="recruit-lock" role="status">
          <Lock size={15} />
          <span>
            <strong>Locked to a placement.</strong> This candidate accepted an offer and is
            excluded from new job-order matching. Record the offer as declined if the placement
            falls through.
          </span>
        </p>
      )}

      <div className="recruit-facts">
        <div>
          <span>Submitted to</span>
          <strong>{candidate.submission_target_name || "—"}</strong>
          <small>
            {candidate.submission_target_type
              ? `${candidate.submission_target_type} · ${when(candidate.submission_date)}`
              : "Not submitted"}
          </small>
        </div>
        <div>
          <span>Interview</span>
          <strong>
            {candidate.interview_status ? INTERVIEW_LABEL[candidate.interview_status] : "—"}
          </strong>
          <small>{candidate.interview_at ? whenExact(candidate.interview_at) : "No date set"}</small>
        </div>
        <div>
          <span>Offer</span>
          <strong>{candidate.offer_status ?? "—"}</strong>
          <small>{locked ? "Accepted and signed" : "No standing offer"}</small>
        </div>
        <div>
          <span>Last outcome</span>
          <strong>
            {candidate.last_outcome ? OUTCOME_LABEL[candidate.last_outcome] : "—"}
          </strong>
          <small>{when(candidate.last_outcome_at)}</small>
        </div>
        <div>
          <span>Office</span>
          <strong>{candidate.office_name || "Unassigned"}</strong>
          <small>{candidate.profile?.destination_country || "No destination set"}</small>
        </div>
        <div>
          <span>Job order</span>
          <strong>{candidate.job_order_id || "—"}</strong>
          <small>{status === "available" ? "Open to new matching" : "Committed"}</small>
        </div>
      </div>

      {(canManage || canReassign) && (
        <div className="recruit-actions">
          {canSubmit && (
            <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => setOpen(open === "submit" ? null : "submit")}>
              <Send size={15} /> Submit candidate
            </button>
          )}
          {canRecordInterview && (
            <button type="button" className="ds-ghost-btn" disabled={busy} onClick={() => setOpen(open === "interview" ? null : "interview")}>
              <CalendarClock size={15} /> Interview
            </button>
          )}
          {canRecordOutcome && (
            <button type="button" className="ds-ghost-btn" disabled={busy} onClick={() => setOpen(open === "outcome" ? null : "outcome")}>
              <CheckCircle2 size={15} /> Record outcome
            </button>
          )}
          {(canIssueOffer || canAnswerOffer) && (
            <button type="button" className="ds-ghost-btn" disabled={busy} onClick={() => setOpen(open === "offer" ? null : "offer")}>
              <FileSignature size={15} /> Offer
            </button>
          )}
          {canReassign && (
            <button type="button" className="ds-ghost-btn" disabled={busy} onClick={() => setOpen(open === "reassign" ? null : "reassign")}>
              <MapPinned size={15} /> Reassign
            </button>
          )}
        </div>
      )}

      {open === "submit" && (
        <div className="recruit-form">
          <label>
            Send to
            <select value={targetType} onChange={(e) => setTargetType(e.target.value as "company" | "associate")}>
              <option value="company">Company</option>
              <option value="associate">Associate</option>
            </select>
          </label>
          <label>
            Name
            <input value={targetName} onChange={(e) => setTargetName(e.target.value)} placeholder="Keppel Shipyard" maxLength={200} />
          </label>
          <label>
            Job order (optional)
            <input value={jobOrderId} onChange={(e) => setJobOrderId(e.target.value)} placeholder="JO-1042" />
          </label>
          <label className="is-wide">
            Notes
            <textarea rows={2} value={submitNotes} onChange={(e) => setSubmitNotes(e.target.value)} placeholder="Why this candidate suits this requirement" />
          </label>
          <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void doSubmit()}>
            <Send size={15} /> Submit and record the date
          </button>
        </div>
      )}

      {open === "interview" && (
        <div className="recruit-form">
          <label>
            Status
            <select value={interviewStatus} onChange={(e) => setInterviewStatus(e.target.value as InterviewStatus)}>
              <option value="scheduled">Scheduled</option>
              <option value="completed">Completed</option>
              <option value="pending">Still pending</option>
              <option value="unavailable">Candidate unavailable</option>
            </select>
          </label>
          <label>
            Date and time
            <input type="datetime-local" value={interviewAt} onChange={(e) => setInterviewAt(e.target.value)} />
          </label>
          <label className="is-wide">
            Notes
            <textarea rows={2} value={interviewNotes} onChange={(e) => setInterviewNotes(e.target.value)} placeholder="Round, panel, or why it did not happen" />
          </label>
          <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void doInterview()}>
            <CalendarClock size={15} /> Save interview status
          </button>
        </div>
      )}

      {open === "outcome" && (
        <div className="recruit-form">
          <label>
            Client decision
            <select value={outcome} onChange={(e) => setOutcome(e.target.value as InterviewOutcome)}>
              <option value="selected">Selected</option>
              <option value="on_hold">On hold</option>
              <option value="rejected">Rejected</option>
              <option value="offer_declined">Offer declined</option>
            </select>
          </label>
          <label className="is-wide">
            Notes
            <textarea rows={2} value={outcomeNotes} onChange={(e) => setOutcomeNotes(e.target.value)} placeholder="What the company said" />
          </label>
          {(outcome === "rejected" || outcome === "offer_declined") && (
            <p className="recruit-note">
              This returns the candidate to the available pool, where they can be matched against
              new job orders. The decision itself stays on their record.
            </p>
          )}
          <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void doOutcome()}>
            <CheckCircle2 size={15} /> Record outcome
          </button>
        </div>
      )}

      {open === "offer" && (
        <div className="recruit-form">
          <label>
            Offer
            <select value={offerStatus} onChange={(e) => setOfferStatus(e.target.value as OfferStatus)}>
              {!locked && <option value="issued">Issued</option>}
              <option value="accepted">Accepted / signed</option>
              <option value="declined">Declined by candidate</option>
            </select>
          </label>
          <label className="is-wide">
            Notes
            <textarea rows={2} value={offerNotes} onChange={(e) => setOfferNotes(e.target.value)} placeholder="Salary, start date, or why it was declined" />
          </label>
          <p className="recruit-note">
            {offerStatus === "accepted"
              ? "Accepting locks this candidate out of new job-order matching."
              : offerStatus === "declined"
                ? "Declining releases the candidate back to the available pool."
                : "Issuing an offer does not lock the candidate; accepting it does."}
          </p>
          <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void doOffer()}>
            <FileSignature size={15} /> Save offer status
          </button>
        </div>
      )}

      {open === "reassign" && (
        <div className="recruit-form">
          <label>
            Destination country
            <select value={country} onChange={(e) => setCountry(e.target.value)}>
              <option value="">Select a country</option>
              {countries.map((row) => (
                <option key={row.id} value={row.name}>{row.name}</option>
              ))}
            </select>
          </label>
          <label>
            Office
            <select value={officeId} onChange={(e) => setOfficeId(e.target.value)}>
              <option value="">Leave unchanged</option>
              {offices.map((office) => (
                <option key={office.id} value={office.id}>{office.name}</option>
              ))}
            </select>
          </label>
          <label>
            New owner
            <select value={staffId} onChange={(e) => setStaffId(e.target.value)}>
              <option value="">Leave with {candidate.assigned_staff_name || "the current owner"}</option>
              {staff.map((person) => (
                <option key={person.id} value={person.id}>{person.name}</option>
              ))}
            </select>
          </label>
          <label className="is-wide">
            Reason <span aria-hidden="true">*</span>
            <textarea rows={2} value={reassignReason} onChange={(e) => setReassignReason(e.target.value)} placeholder="Candidate withdrew from Singapore and asked for Europe" />
          </label>
          <p className="recruit-note">
            A deliberate move across countries, offices and desks. Both sides of the change are
            recorded below, with your name and the reason. Choosing a new owner restarts their
            review.
          </p>
          <button type="button" className="ds-primary-btn" disabled={busy} onClick={() => void doReassign()}>
            <MapPinned size={15} /> Reassign candidate
          </button>
        </div>
      )}

      <div className="recruit-history">
        <h4><History size={15} /> Pipeline history</h4>
        {history.length === 0 ? (
          <p className="recruit-empty">
            <Briefcase size={16} /> Nothing recorded yet. Submitting this candidate starts the trail.
          </p>
        ) : (
          <ol>
            {[...history].reverse().map((event, index) => (
              <li key={`${event.type}-${event.at}-${index}`}>
                <span className="recruit-event-type">{event.type}</span>
                <span className="recruit-event-body">
                  <strong>
                    {event.type === "submitted"
                      ? `Submitted to ${event.target_name ?? "—"}`
                      : event.type === "interview"
                        ? `Interview ${event.status ?? ""}`
                        : event.type === "outcome"
                          ? `${OUTCOME_LABEL[(event.status ?? "selected") as InterviewOutcome] ?? event.status}${event.returned_to_pool ? " · returned to pool" : ""}`
                          : `Offer ${event.status ?? ""}`}
                  </strong>
                  {event.notes && <small>{event.notes}</small>}
                  <small className="recruit-event-meta">
                    {whenExact(event.at)}
                    {event.actor_name ? ` · ${event.actor_name}` : ""}
                  </small>
                </span>
              </li>
            ))}
          </ol>
        )}
      </div>

      {!canManage && !canReassign && (
        <p className="recruit-note">
          <Ban size={14} /> You can see this candidate&rsquo;s pipeline but not change it.
        </p>
      )}
    </section>
  );
}
