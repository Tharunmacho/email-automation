"use client";

/**
 * Where a mapped candidate stands, for a table row: a status, six segments,
 * and the step count.
 *
 * One cell rather than separate Status and Progress columns — they answer the
 * same question, and the room is worth more to the job order and the
 * candidate. The words carry the meaning; the segment colours are a shortcut
 * for somebody scanning a column, never the only signal.
 */

import type { MappedCandidateRow } from "@/lib/api";

import { MAPPING_STAGES, STAGE_META, rowStatus } from "./stages";

export default function MappingProgress({ row, showStatus = true }: { row: MappedCandidateRow; showStatus?: boolean }) {
  const { mapping } = row;
  const current = mapping.current_stage;
  const status = rowStatus(row);

  const caption =
    mapping.status === "completed"
      ? "All 6 steps complete"
      : mapping.status === "closed"
        ? `Closed at ${STAGE_META[mapping.closed?.stage ?? "selection"].title.toLowerCase()}`
        : `Step ${MAPPING_STAGES.indexOf(current ?? "resume") + 1} of ${MAPPING_STAGES.length} · ${STAGE_META[current ?? "resume"].title}`;

  return (
    <span className="map-progress">
      {showStatus && (
        <span className={`ds-status is-${status.tone}`}><i aria-hidden="true" />{status.label}</span>
      )}
      <span className="map-progress-track" aria-hidden="true">
        {mapping.stages.map((stage) => (
          <i
            key={stage.key}
            className={`map-progress-seg is-${stage.state}`}
            title={`${STAGE_META[stage.key].title}: ${stage.state}`}
          />
        ))}
      </span>
      <span className="map-progress-caption">{caption}</span>
    </span>
  );
}
