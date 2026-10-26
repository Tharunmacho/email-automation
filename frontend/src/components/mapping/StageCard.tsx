"use client";

/**
 * One step of a mapped candidate's pipeline.
 *
 * Five ways to look, one per state the API reports:
 *
 *   passed / skipped  what was answered, by whom and when; "Change" while no
 *                     later step has been recorded
 *   current           the form, open — this is the step being worked
 *   failed            the answer that closed the mapping
 *   locked            what it is waiting on, and nothing to press
 *
 * Skipping is offered only to managers and admins, and only on the current
 * step. The API refuses it for anybody else regardless; hiding the button is
 * so a staff member is not shown a control whose only result is a refusal.
 */

import { useState } from "react";
import { Check, Loader2, Lock, Pencil, SkipForward, X } from "lucide-react";

import type { MappingStagePayload, MappingStageView } from "@/lib/api";

import StageForm from "./StageForm";
import { STAGE_META, answerLabel, answerTone, entryFacts, formatMoment } from "./stages";

interface Props {
  step: MappingStageView;
  index: number;
  /** The step before this one, named in the locked message. */
  previousTitle?: string;
  canSkip: boolean;
  busy: boolean;
  /** Resolves true when the API accepted the change. */
  onSave: (payload: MappingStagePayload) => Promise<boolean>;
}

export default function StageCard({ step, index, previousTitle, canSkip, busy, onSave }: Props) {
  const meta = STAGE_META[step.key];
  const Icon = meta.icon;
  const [editing, setEditing] = useState(false);
  const [skipping, setSkipping] = useState(false);
  const [skipReason, setSkipReason] = useState("");

  const isCurrent = step.state === "current";
  const answered = step.state === "passed" || step.state === "skipped" || step.state === "failed";
  const showForm = step.answerable && (isCurrent || editing) && !skipping;
  const entry = step.entry;

  const save = async (payload: MappingStagePayload) => {
    if (await onSave(payload)) {
      setEditing(false);
      setSkipping(false);
      setSkipReason("");
    }
  };

  const node =
    step.state === "passed" ? <Check size={15} strokeWidth={2.6} />
      : step.state === "failed" ? <X size={15} strokeWidth={2.6} />
        : step.state === "skipped" ? <SkipForward size={14} />
          : step.state === "locked" ? <Lock size={13} />
            : <Icon size={15} />;

  return (
    <li className={`map-stage is-${step.state}`} aria-current={isCurrent ? "step" : undefined}>
      <span className="map-stage-node" aria-hidden="true">{node}</span>

      <div className="map-stage-card">
        <header className="map-stage-head">
          <span className="map-stage-icon" aria-hidden="true"><Icon size={16} /></span>
          <span className="map-stage-titles">
            <span className="map-stage-step">Step {index + 1}</span>
            <h4 className="map-stage-title">{meta.title}</h4>
          </span>
          {step.state === "locked" ? (
            <span className="map-chip is-neutral"><Lock size={11} /> Locked</span>
          ) : entry ? (
            <span className={`map-chip is-${answerTone(step)}`}>{answerLabel(step.key, entry)}</span>
          ) : (
            <span className="map-chip is-info">In progress</span>
          )}
        </header>

        {step.state === "locked" && (
          <p className="map-stage-note">
            Opens once {previousTitle ? <strong>{previousTitle}</strong> : "the previous step"} is complete.
          </p>
        )}

        {entry && (answered || (isCurrent && !showForm)) && !editing && (
          <div className="map-stage-summary">
            {entryFacts(step.key, entry).length > 0 && (
              <dl className="map-facts">
                {entryFacts(step.key, entry).map((fact) => (
                  <div key={fact.label}>
                    <dt>{fact.label}</dt>
                    <dd>{fact.value}</dd>
                  </div>
                ))}
              </dl>
            )}
            {entry.notes && <p className="map-stage-notes">“{entry.notes}”</p>}
            <p className="map-stage-meta">
              {entry.derived
                ? "Recorded on the candidate's recruitment pipeline."
                : `${entry.skipped ? "Skipped" : "Recorded"} by ${entry.actor_name || "—"} · ${formatMoment(entry.at)}`}
            </p>
          </div>
        )}

        {/* A partial answer on the current step ("not yet", "unfit") is shown
            above its form so the person updating it sees what is on file. */}
        {entry && isCurrent && showForm && (
          <p className="map-stage-meta">
            On file: <strong>{answerLabel(step.key, entry)}</strong>
            {entry.actor_name ? ` · ${entry.actor_name}, ${formatMoment(entry.at)}` : ""}
          </p>
        )}

        {showForm && (
          <StageForm
            key={`${step.key}-${entry?.at ?? "new"}`}
            stage={step.key}
            entry={entry}
            busy={busy}
            onSave={(payload) => void save(payload)}
            onCancel={editing ? () => setEditing(false) : undefined}
          />
        )}

        {answered && step.answerable && !editing && step.state !== "failed" && (
          <button type="button" className="map-link-btn" onClick={() => setEditing(true)} disabled={busy}>
            <Pencil size={13} /> Change answer
          </button>
        )}

        {isCurrent && step.answerable && canSkip && (
          skipping ? (
            <div className="map-skip">
              <p className="map-skip-title">
                <SkipForward size={14} aria-hidden="true" /> Skip “{meta.title}”?
              </p>
              <p className="map-stage-note">
                The next step opens without an answer recorded here. The skip and your reason are kept on
                the candidate&rsquo;s history.
              </p>
              <textarea
                className="map-input map-textarea"
                rows={2}
                maxLength={1000}
                value={skipReason}
                onChange={(event) => setSkipReason(event.target.value)}
                placeholder="Reason (optional) — e.g. client confirmed directly"
                aria-label="Reason for skipping"
                disabled={busy}
              />
              <div className="map-form-actions">
                <button type="button" className="ds-ghost-btn" onClick={() => setSkipping(false)} disabled={busy}>
                  Cancel
                </button>
                <button
                  type="button"
                  className="ds-primary-btn"
                  onClick={() => void save({ skip: true, notes: skipReason.trim() })}
                  disabled={busy}
                >
                  {busy ? <Loader2 size={15} className="icon-spin" /> : <SkipForward size={15} />}
                  Skip and open next step
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="map-link-btn" onClick={() => setSkipping(true)} disabled={busy}>
              <SkipForward size={13} /> Skip this step
            </button>
          )
        )}
      </div>
    </li>
  );
}
