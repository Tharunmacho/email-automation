"use client";

import type { LucideIcon } from "lucide-react";

import { formatInt } from "@/lib/format";

export interface PipelineSegment {
  label: string;
  value: number;
  icon: LucideIcon;
  /** One of the tone names the stylesheet knows: info, success, warning, rose. */
  tone: "info" | "success" | "warning" | "rose";
  /** Fired when the segment is pressed — omit to render it as plain text. */
  onSelect?: () => void;
}

interface PipelineSplitProps {
  title: string;
  segments: PipelineSegment[];
}

/**
 * The pipeline stated as three parallel readings rather than as one ring.
 *
 * A donut answers "what share of the pool is verified"; this answers "how many
 * are verified, how many are working, how many are stuck" — which is the
 * question a recruiter actually opens the dashboard with. The rule under each
 * column is the share, so the comparison a donut was carrying is still here,
 * read along a straight line where lengths can be compared instead of around a
 * circle where they cannot.
 */
export default function PipelineSplit({ title, segments }: PipelineSplitProps) {
  const total = segments.reduce((sum, segment) => sum + segment.value, 0);

  return (
    <section className="pl-split">
      <header className="pl-split-head">
        <h3 className="pl-split-title">{title}</h3>
      </header>

      <div className="pl-split-row">
        {segments.map((segment) => {
          const Icon = segment.icon;
          // With nothing in the pool every rule would be full width, which reads
          // as three complete stages rather than as an empty pipeline.
          const share = total > 0 ? (segment.value / total) * 100 : 0;

          const body = (
            <>
              <span className="pl-split-top">
                <span className={`pl-split-icon is-${segment.tone}`}>
                  <Icon size={13} strokeWidth={2.4} />
                </span>
                <span className="pl-split-value">{formatInt(segment.value)}</span>
              </span>
              <span className="pl-split-label">{segment.label}</span>
              <span className="pl-split-track">
                <span
                  className={`pl-split-fill is-${segment.tone}`}
                  style={{ width: `${share}%` }}
                />
              </span>
            </>
          );

          return segment.onSelect ? (
            <button
              key={segment.label}
              type="button"
              className="pl-split-cell is-clickable"
              onClick={segment.onSelect}
              title={`${formatInt(segment.value)} ${segment.label.toLowerCase()}`}
            >
              {body}
            </button>
          ) : (
            <div key={segment.label} className="pl-split-cell">
              {body}
            </div>
          );
        })}
      </div>
    </section>
  );
}
