"use client";

/**
 * A mapped candidate's six steps, from resume to travel, in one dialog.
 *
 * The steps read top to bottom as a timeline, and only the current one is open
 * for an answer — the API sends which step that is, so this never has to
 * decide it. Saving a step swaps in the refreshed row the API answers with,
 * which is what unlocks the next card.
 */

import { useEffect, useRef, useState } from "react";
import { Briefcase, CircleCheck, ExternalLink, Info, Lock, PlaneTakeoff, X } from "lucide-react";

import { useModalFocus } from "@/components/ui/useModalFocus";
import { initialsOf } from "@/lib/format";
import {
  recordMappingStage,
  type MappedCandidateRow,
  type MappingStageKey,
  type MappingStagePayload,
} from "@/lib/api";
import { getDisplayName, getReference } from "@/screens/CandidatesView";

import MappingProgress from "./MappingProgress";
import StageCard from "./StageCard";
import { MAPPING_STAGES, STAGE_META, formatDay, formatMoment, rowStatus } from "./stages";

type Toast = (message: string, type?: "success" | "error" | "info") => void;

interface Props {
  row: MappedCandidateRow;
  /** Managers and admins. Staff move one answered step at a time. */
  canSkip: boolean;
  onClose: () => void;
  onChanged: (row: MappedCandidateRow) => void;
  onToast: Toast;
  /** Open the full candidate profile; absent when this account cannot read it. */
  onOpenProfile?: (candidateId: string) => void;
}

function nextTitle(stage: MappingStageKey): string | null {
  const next = MAPPING_STAGES[MAPPING_STAGES.indexOf(stage) + 1];
  return next ? STAGE_META[next].title : null;
}

export default function MappingPipelineDialog({ row, canSkip, onClose, onChanged, onToast, onOpenProfile }: Props) {
  const [busyStage, setBusyStage] = useState<MappingStageKey | null>(null);
  const dialogRef = useModalFocus<HTMLDivElement>(true, () => {
    if (!busyStage) onClose();
  });
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const name = getDisplayName(row);
  const order = row.job_order;
  const status = rowStatus(row);
  const { mapping } = row;

  // Bring the step being worked into view — on open, and again when saving
  // one opens the next. Four answered steps above it would otherwise leave the
  // only form on screen below the fold.
  const currentStage = mapping.current_stage;
  useEffect(() => {
    const body = bodyRef.current;
    const node = body?.querySelector<HTMLElement>(".map-stage.is-current");
    if (!body || !node) return;
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const top = body.scrollTop + node.getBoundingClientRect().top - body.getBoundingClientRect().top - 12;
    body.scrollTo({ top: Math.max(0, top), behavior: reduce ? "auto" : "smooth" });
  }, [currentStage]);

  const save = async (stage: MappingStageKey, payload: MappingStagePayload): Promise<boolean> => {
    setBusyStage(stage);
    try {
      const updated = await recordMappingStage(row.id, stage, payload);
      onChanged(updated);
      const step = updated.mapping.stages.find((item) => item.key === stage);
      const title = STAGE_META[stage].title;
      const next = nextTitle(stage);
      if (updated.mapping.status === "closed") {
        onToast(`Mapping closed — ${name} is back in the available pool.`, "info");
      } else if (updated.mapping.status === "completed") {
        onToast(`${name} has travelled. All steps are complete.`, "success");
      } else if (step?.state === "skipped") {
        onToast(`${title} skipped${next ? ` — ${next} is open` : ""}.`, "success");
      } else if (step?.state === "passed") {
        onToast(`${title} recorded${next ? ` — ${next} is open` : ""}.`, "success");
      } else {
        onToast(`${title} updated. The step stays open.`, "info");
      }
      return true;
    } catch (error) {
      // The API explains every refusal (out of turn, closed, not permitted);
      // its own words are more use than a generic failure.
      onToast(error instanceof Error ? error.message : "The step could not be saved.", "error");
      return false;
    } finally {
      setBusyStage(null);
    }
  };

  const travel = mapping.stages.find((step) => step.key === "travel")?.entry;

  return (
    <div className="cm-overlay active" onClick={() => !busyStage && onClose()}>
      <div
        ref={dialogRef}
        className="cm-dialog sh-modal map-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="map-dialog-title"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="sh-modal-head map-dialog-head">
          <div className="map-dialog-who">
            <span className="ds-avatar" aria-hidden="true">{initialsOf(name)}</span>
            <span className="map-dialog-who-text">
              <h2 id="map-dialog-title" className="sh-modal-title">{name}</h2>
              <span className="crm-record-id">Candidate ID · {getReference(row)}</span>
            </span>
          </div>
          <button
            type="button"
            className="sh-modal-close"
            onClick={onClose}
            disabled={Boolean(busyStage)}
            aria-label="Close pipeline"
          >
            <X size={18} />
          </button>
        </div>

        <div className="map-dialog-body" ref={bodyRef}>
          <section className="map-order-card" aria-label="Job order">
            <span className="map-order-icon" aria-hidden="true"><Briefcase size={17} /></span>
            <span className="map-order-text">
              <span className="map-order-eyebrow">Mapped to job order</span>
              <strong>{order.missing ? "Job order no longer on file" : order.title || "Untitled order"}</strong>
              <small>
                {[order.client, order.destinationCountry, order.id].filter(Boolean).join(" · ")}
              </small>
            </span>
            <span className="map-order-side">
              <span className={`ds-status is-${status.tone}`}><i aria-hidden="true" />{status.label}</span>
              <MappingProgress row={row} showStatus={false} />
            </span>
          </section>

          {mapping.status === "closed" && mapping.closed && (
            <p className="map-callout is-bad" role="status">
              <Lock size={15} aria-hidden="true" />
              <span>
                <strong>This mapping is closed</strong> at {STAGE_META[mapping.closed.stage].title.toLowerCase()}
                {mapping.closed.by ? ` by ${mapping.closed.by}` : ""} on {formatMoment(mapping.closed.at)}. The
                candidate is back in the available pool; mapping them to a job order again starts a new pipeline.
              </span>
            </p>
          )}

          {mapping.status === "completed" && (
            <p className="map-callout is-ok" role="status">
              <PlaneTakeoff size={15} aria-hidden="true" />
              <span>
                <strong>Travelled</strong>
                {travel?.travel_date ? ` on ${formatDay(travel.travel_date)}` : ""}
                {travel?.travel_time ? ` at ${travel.travel_time}` : ""}
                {travel?.flight_number ? `, flight ${travel.flight_number}` : ""}. Every step is complete.
              </span>
            </p>
          )}

          <ol className="map-stages" aria-label="Pipeline steps">
            {mapping.stages.map((step, index) => (
              <StageCard
                key={step.key}
                step={step}
                index={index}
                previousTitle={index > 0 ? STAGE_META[mapping.stages[index - 1].key].title : undefined}
                canSkip={canSkip}
                busy={busyStage === step.key}
                onSave={(payload) => save(step.key, payload)}
              />
            ))}
          </ol>

          <p className="map-dialog-hint">
            {mapping.status === "completed" ? (
              <><CircleCheck size={14} aria-hidden="true" /> Answers stay on the candidate&rsquo;s recruitment history.</>
            ) : (
              <>
                <Info size={14} aria-hidden="true" />
                Each step opens once the one before it is complete.
                {canSkip ? " As a manager or admin you can skip the current step." : " Only managers and admins can skip a step."}
              </>
            )}
          </p>
        </div>

        <div className="modal-footer map-dialog-foot">
          {onOpenProfile ? (
            <button type="button" className="ds-ghost-btn" onClick={() => onOpenProfile(row.id)} disabled={Boolean(busyStage)}>
              <ExternalLink size={15} /> Open profile
            </button>
          ) : <span />}
          <button type="button" className="modal-cancel-btn" onClick={onClose} disabled={Boolean(busyStage)}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
