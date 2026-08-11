"use client";

import { useMemo } from "react";
import { Cpu, FileScan, Gauge, Mail, RefreshCw, ScanLine } from "lucide-react";

import { formatInt } from "@/lib/format";
import { REVIEW_CONFIDENCE_THRESHOLD, type CandidateProfile, type CandidateRecord } from "@/lib/api";
import type { NavId } from "@/lib/nav";

interface ResumeParserScreenProps {
  candidates: CandidateRecord[];
  syncing: boolean;
  onSync: () => void;
  onNavigate: (id: NavId) => void;
}

/** Confidence bands, coarse enough that each one means something different. */
const BANDS = [
  { label: "High (90–100%)", min: 0.9, tone: "is-success" },
  { label: "Good (75–90%)", min: 0.75, tone: "is-success" },
  { label: "Fair (50–75%)", min: 0.5, tone: "is-warn" },
  { label: "Low (below 50%)", min: 0, tone: "is-error" },
] as const;

/**
 * The fields the extractor is asked for, and how to tell whether it found one.
 * Ordered by how much a recruiter misses it when it is absent.
 */
const FIELDS: { label: string; has: (p: CandidateProfile) => boolean }[] = [
  { label: "Full name", has: (p) => Boolean(p.full_name?.trim()) },
  { label: "Email", has: (p) => Boolean(p.email?.trim()) },
  { label: "Phone", has: (p) => Boolean(p.phone?.trim()) },
  { label: "Location", has: (p) => Boolean(p.location?.trim()) },
  { label: "Current designation", has: (p) => Boolean(p.current_designation?.trim()) },
  { label: "Total experience", has: (p) => typeof p.total_experience_years === "number" },
  { label: "Skills", has: (p) => (p.skills?.length ?? 0) > 0 },
  { label: "Work experience", has: (p) => (p.work_experience?.length ?? 0) > 0 },
  { label: "Education", has: (p) => (p.education?.length ?? 0) > 0 },
  { label: "Projects", has: (p) => (p.projects?.length ?? 0) > 0 },
  { label: "Certifications", has: (p) => (p.certifications?.length ?? 0) > 0 },
  { label: "Languages", has: (p) => (p.languages?.length ?? 0) > 0 },
  { label: "LinkedIn", has: (p) => Boolean(p.linkedin_url?.trim()) },
  { label: "Summary", has: (p) => Boolean(p.resume_summary?.trim()) },
];

function Bar({
  label,
  count,
  total,
  tone,
  suffix,
}: {
  label: string;
  count: number;
  total: number;
  tone?: string;
  suffix?: string;
}) {
  const share = total > 0 ? (count / total) * 100 : 0;
  return (
    <div className="db-bar-row">
      <span className="db-bar-label" title={label}>
        {label}
      </span>
      <span className="db-bar-track">
        <span className={`db-bar-fill ${tone ?? ""}`} style={{ width: `${share}%` }} />
      </span>
      <span className="db-bar-value">{suffix ?? formatInt(count)}</span>
    </div>
  );
}

/**
 * The parser as a tool: how documents reach it, how confident it is, and which
 * fields it actually manages to pull out.
 *
 * Everything here is read from what the engine has already written — every
 * résumé carries the method that read it and the confidence it scored — so the
 * numbers cannot drift out of step with the candidate list.
 */
export default function ResumeParserScreen({
  candidates,
  syncing,
  onSync,
  onNavigate,
}: ResumeParserScreenProps) {
  const stats = useMemo(() => {
    const scored = candidates.filter((c) => typeof c.profile?.confidence === "number");
    const confidences = scored.map((c) => c.profile.confidence as number);

    const methods = new Map<string, number>();
    let ocrUsed = 0;
    let multiPage = 0;

    for (const candidate of candidates) {
      const method = candidate.resume?.extraction_method?.trim() || "unrecorded";
      methods.set(method, (methods.get(method) ?? 0) + 1);
      if (candidate.resume?.ocr_used) ocrUsed += 1;
      const pages = candidate.raw_ocr?.pages;
      if (Array.isArray(pages) && pages.length > 1) multiPage += 1;
    }

    const mean =
      confidences.length > 0 ? confidences.reduce((a, b) => a + b, 0) / confidences.length : 0;
    const sorted = [...confidences].sort((a, b) => a - b);
    const median = sorted.length > 0 ? sorted[Math.floor(sorted.length / 2)] : 0;

    return {
      scored,
      mean,
      median,
      ocrUsed,
      multiPage,
      methods: [...methods.entries()].sort((a, b) => b[1] - a[1]),
      belowThreshold: scored.filter((c) => (c.profile.confidence ?? 1) < REVIEW_CONFIDENCE_THRESHOLD)
        .length,
    };
  }, [candidates]);

  /** How often each field survives extraction, across the whole pool. */
  const fieldFill = useMemo(
    () =>
      FIELDS.map((field) => ({
        label: field.label,
        count: candidates.filter((c) => c.profile && field.has(c.profile)).length,
      })).sort((a, b) => b.count - a.count),
    [candidates],
  );

  const bands = BANDS.map((band, index) => {
    const upper = index === 0 ? Infinity : BANDS[index - 1].min;
    const count = stats.scored.filter((c) => {
      const value = c.profile.confidence ?? 0;
      return value >= band.min && value < upper;
    }).length;
    return { ...band, count };
  });

  /* Not a file dropzone. This pipeline ingests from a mailbox and the API has
     no upload route, so a drop target here would take a recruiter's CV and lose
     it. The card states where documents actually come from and offers the one
     control that does put a résumé through the parser. */
  const intake = (
    <section className="db-card">
      <header className="db-card-head">
        <div>
          <h3 className="db-card-title">Feed the parser</h3>
          <p className="db-card-sub">How a document gets in front of the extraction engine.</p>
        </div>
      </header>
      <div className="db-card-body">
        <div className="ov-intake">
          <Mail size={20} strokeWidth={1.8} />
          <span className="ov-intake-title">Résumés arrive by email</span>
          <span className="ov-intake-sub">
            Attachments sent to the connected mailbox are detected, read — with OCR when there is no
            text layer — then scored and stored. There is no manual upload route on this deployment.
          </span>
        </div>
        <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.85rem", flexWrap: "wrap" }}>
          <button type="button" className="db-btn is-primary" onClick={onSync} disabled={syncing}>
            <RefreshCw size={15} className={syncing ? "icon-spin" : undefined} />
            {syncing ? "Parsing…" : "Parse new résumés"}
          </button>
          <button type="button" className="db-btn" onClick={() => onNavigate("visualizer")}>
            Watch the pipeline
          </button>
        </div>
      </div>
    </section>
  );

  if (candidates.length === 0) {
    return (
      <>
        {intake}
        <div className="db-empty">
          <Cpu size={28} strokeWidth={1.5} />
          <span className="db-empty-title">Nothing parsed yet</span>
          <span className="db-empty-sub">
            Parser statistics are derived from stored résumés. Run a cycle and this screen fills
            itself in.
          </span>
        </div>
      </>
    );
  }

  return (
    <>
      <section className="ov-kpis">
        <article className="ov-kpi">
          <p className="ov-kpi-label">
            Mean confidence
            <Gauge size={15} />
          </p>
          <p className="ov-kpi-value">{(stats.mean * 100).toFixed(1)}%</p>
          <p className="ov-kpi-caption">
            Median {(stats.median * 100).toFixed(1)}% across {formatInt(stats.scored.length)} scored
            parses
          </p>
        </article>

        <article className="ov-kpi">
          <p className="ov-kpi-label">
            Below threshold
            <FileScan size={15} />
          </p>
          <p className="ov-kpi-value">{formatInt(stats.belowThreshold)}</p>
          <p className="ov-kpi-caption">
            Scored under {(REVIEW_CONFIDENCE_THRESHOLD * 100).toFixed(0)}% — routed to pending review
          </p>
        </article>

        <article className="ov-kpi">
          <p className="ov-kpi-label">
            OCR required
            <ScanLine size={15} />
          </p>
          <p className="ov-kpi-value">{formatInt(stats.ocrUsed)}</p>
          <p className="ov-kpi-caption">
            {Math.round((stats.ocrUsed / candidates.length) * 100)}% of résumés had no text layer
          </p>
        </article>

        <article className="ov-kpi">
          <p className="ov-kpi-label">
            Multi-page scans
            <Cpu size={15} />
          </p>
          <p className="ov-kpi-value">{formatInt(stats.multiPage)}</p>
          <p className="ov-kpi-caption">
            Documents where the page classifier had to find the résumé
          </p>
        </article>
      </section>

      {intake}

      <div className="db-split">
        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Confidence breakdown</h3>
              <p className="db-card-sub">Where the parser lands across every scored résumé.</p>
            </div>
          </header>
          <div className="db-card-body">
            {bands.map((band) => (
              <Bar
                key={band.label}
                label={band.label}
                count={band.count}
                total={stats.scored.length}
                tone={band.tone}
              />
            ))}
            {stats.belowThreshold > 0 && (
              <button
                type="button"
                className="db-btn is-block"
                style={{ marginTop: "0.9rem" }}
                onClick={() => onNavigate("candidates")}
              >
                Review {formatInt(stats.belowThreshold)} low-confidence profile
                {stats.belowThreshold === 1 ? "" : "s"}
              </button>
            )}
          </div>
        </section>

        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Extraction method</h3>
              <p className="db-card-sub">Which reader got the text out of each document.</p>
            </div>
          </header>
          <div className="db-card-body">
            {stats.methods.map(([method, count]) => (
              <Bar key={method} label={method} count={count} total={candidates.length} />
            ))}
          </div>
        </section>
      </div>

      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">AI field extraction</h3>
            <p className="db-card-sub">
              How often each field survives extraction, across all {formatInt(candidates.length)}{" "}
              parsed profiles. A low bar is usually the résumé, not the model — most CVs carry no
              portfolio link.
            </p>
          </div>
        </header>
        <div className="db-card-body">
          {fieldFill.map((field) => {
            const share = (field.count / candidates.length) * 100;
            return (
              <Bar
                key={field.label}
                label={field.label}
                count={field.count}
                total={candidates.length}
                tone={share >= 75 ? "is-success" : share >= 40 ? "is-warn" : "is-error"}
                suffix={`${Math.round(share)}%`}
              />
            );
          })}
        </div>
      </section>
    </>
  );
}
