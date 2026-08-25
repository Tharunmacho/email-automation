"use client";

import { useMemo, useState } from "react";
import {
  ArrowRight,
  FileSearch,
  RefreshCw,
  Search,
  SlidersHorizontal,
  TrendingDown,
  TrendingUp,
  UserCheck,
  UserPlus,
  Users,
} from "lucide-react";

import FlowBarChart, { type FlowBucket } from "@/components/dashboard/FlowBarChart";
import DashboardSkeleton from "@/components/dashboard/DashboardSkeleton";
import { isVerified, needsReview, windowDelta, buildCumulativeTrend } from "@/lib/dashboardMetrics";
import {
  candidateNameOf,
  formatDateFull,
  formatInt,
  initialsOf,
} from "@/lib/format";
import { useIsMounted } from "@/lib/useIsMounted";
import type { NavId } from "@/lib/nav";
import type { CandidateRecord } from "@/lib/api";
import type { LogEntry } from "@/components/dashboard/ActivityLog";

interface OverviewScreenProps {
  total: number;
  candidates: CandidateRecord[];
  logs: LogEntry[];
  onNavigate: (id: NavId) => void;
  onOpenCandidate: (candidate: CandidateRecord) => void;
}

const DAY_MS = 24 * 60 * 60 * 1000;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

type Grain = "weekly" | "monthly";

/** Up or down against the period before, or nothing when there is no before. */
function Delta({ pct, against }: { pct: number | null; against: string }) {
  if (pct === null) return null;
  const up = pct >= 0;
  return (
    <span
      className={`ovf-delta ${up ? "is-up" : "is-down"}`}
      title={`${up ? "Up" : "Down"} ${Math.abs(pct).toFixed(1)}% on the ${against} before this one`}
    >
      {Math.abs(pct).toFixed(1)}%
      {up ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
    </span>
  );
}

/**
 * `Date.now()` read through one door rather than from the render body.
 *
 * Everything else on this screen is a pure function of the candidate
 * collection; the clock is the one input that is not, and reading it inline
 * makes two renders of the same props produce two different pages. Every call
 * below sits inside a `useMemo` or behind the mount gate.
 */
function now(): Date {
  return new Date();
}

/** One bucket per month for the last `count` months, oldest first. */
function monthlyBuckets(candidates: CandidateRecord[], count: number): FlowBucket[] {
  const today = now();
  const buckets: FlowBucket[] = [];
  const index = new Map<string, number>();

  for (let back = count - 1; back >= 0; back -= 1) {
    const date = new Date(today.getFullYear(), today.getMonth() - back, 1);
    const key = `${date.getFullYear()}-${date.getMonth()}`;
    index.set(key, buckets.length);
    buckets.push({
      label: MONTHS[date.getMonth()],
      full: `${MONTHS_LONG[date.getMonth()]} ${date.getFullYear()}`,
      value: 0,
    });
  }

  for (const candidate of candidates) {
    if (!candidate.created_at) continue;
    const date = new Date(candidate.created_at);
    if (Number.isNaN(date.getTime())) continue;
    const slot = index.get(`${date.getFullYear()}-${date.getMonth()}`);
    if (slot !== undefined) buckets[slot].value += 1;
  }

  return buckets;
}

/** One bucket per week for the last `count` weeks, oldest first. */
function weeklyBuckets(candidates: CandidateRecord[], count: number): FlowBucket[] {
  const today = now();
  // Anchored to the start of today, so a candidate parsed an hour ago lands in
  // this week rather than in a window that ends before they arrived.
  const anchor = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime() + DAY_MS;
  const buckets: FlowBucket[] = [];

  for (let back = count - 1; back >= 0; back -= 1) {
    const end = anchor - back * 7 * DAY_MS;
    const start = end - 7 * DAY_MS;
    const startDate = new Date(start);
    buckets.push({
      label: `${startDate.getDate()} ${MONTHS[startDate.getMonth()]}`,
      full: `Week of ${formatDateFull(startDate)}`,
      value: candidates.filter((candidate) => {
        if (!candidate.created_at) return false;
        const at = new Date(candidate.created_at).getTime();
        return !Number.isNaN(at) && at >= start && at < end;
      }).length,
    });
  }

  return buckets;
}

/** hh:mm for the activity table's own column, matching its date column. */
function clockOf(value: string | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}

/**
 * Everything the product is doing, on one screen.
 *
 * Three readings across the top with the headline one filled, then the state of
 * the pipeline beside the shape of the intake, then the profiles themselves.
 * Nothing here is decoration: every figure is derived from the candidate
 * collection this page already holds, so an empty workspace shows an empty
 * dashboard rather than a demonstration one.
 */
export default function OverviewScreen({
  total,
  candidates,
  onNavigate,
  onOpenCandidate,
}: OverviewScreenProps) {
  // Weekly by default. The pool is young — a monthly chart of a database that
  // started filling this month is one bar and five empty months, which reads as
  // a broken chart rather than as a new one.
  const [grain, setGrain] = useState<Grain>("weekly");
  const [query, setQuery] = useState("");
  const mounted = useIsMounted();

  const verified = candidates.filter(isVerified).length;
  const review = candidates.filter(needsReview).length;
  const active = candidates.filter((c) => c.status !== "verified" && !needsReview(c)).length;
  const unassigned = candidates.filter((c) => !c.assigned_staff_id).length;

  // The period control at the top is the page's, not just the chart's: the
  // deltas on the cards are measured over the same window the chart is drawn
  // at. A "This week" that moved only the plot would be claiming the figures
  // beside it had been re-measured when they had not.
  const windowDays = grain === "weekly" ? 7 : 30;
  const against = grain === "weekly" ? "week" : "month";

  // Twice the window, because the delta needs the period before this one to
  // compare against — a 30-day trend has no 30-days-ago to subtract.
  const totalTrend = useMemo(
    () => buildCumulativeTrend(candidates, windowDays * 2),
    [candidates, windowDays],
  );
  const verifiedTrend = useMemo(
    () => buildCumulativeTrend(candidates, windowDays * 2, isVerified),
    [candidates, windowDays],
  );
  const deltaTotal = windowDelta(totalTrend, windowDays);
  const deltaVerified = windowDelta(verifiedTrend, windowDays);

  const flow = useMemo(
    () => (grain === "monthly" ? monthlyBuckets(candidates, 7) : weeklyBuckets(candidates, 8)),
    [candidates, grain],
  );
  const flowTotal = flow.reduce((sum, bucket) => sum + bucket.value, 0);

  /** The newest arrivals, filtered by whatever is typed in the panel's search. */
  const recent = useMemo(() => {
    const term = query.trim().toLowerCase();
    return [...candidates]
      .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? ""))
      .filter((candidate) => {
        if (!term) return true;
        const haystack = [
          candidateNameOf(candidate),
          candidate.profile?.current_designation,
          candidate.profile?.email,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return haystack.includes(term);
      })
      .slice(0, 6);
  }, [candidates, query]);

  const share = (value: number) =>
    candidates.length > 0 ? `${Math.round((value / candidates.length) * 100)}% of the pool` : "—";

  if (!mounted) return <DashboardSkeleton />;

  return (
    <div className="ovf">
      {/* ── Page head ─────────────────────────────────────────────────── */}
      <header className="ovf-head">
        <div>
          <h1 className="ovf-head-title">Overview</h1>
          <p className="ovf-head-sub">Here is the summary of overall data</p>
        </div>

        <div className="ovf-head-actions">
          <div className="ovf-seg" role="group" aria-label="Reporting period">
            <button
              type="button"
              className={`ovf-seg-btn ${grain === "weekly" ? "is-on" : ""}`}
              onClick={() => setGrain("weekly")}
            >
              This week
            </button>
            <button
              type="button"
              className={`ovf-seg-btn ${grain === "monthly" ? "is-on" : ""}`}
              onClick={() => setGrain("monthly")}
            >
              This month
            </button>
          </div>

          <button type="button" className="ovf-ghost-btn" onClick={() => onNavigate("sourcing")}>
            <RefreshCw size={14} /> Sourcing
          </button>
        </div>
      </header>

      {/* ── Three readings, the headline one filled ───────────────────── */}
      <div className="ovf-cards">
        <article className="ovf-card is-feature">
          <div className="ovf-card-top">
            <span className="ovf-card-icon">
              <Users size={18} strokeWidth={2.1} />
            </span>
            <div>
              <h2 className="ovf-card-title">Total candidates</h2>
              <p className="ovf-card-sub">Every profile the pipeline has parsed</p>
            </div>
          </div>

          <div className="ovf-card-value">
            {formatInt(total)}
            <Delta pct={deltaTotal.percent} against={against} />
          </div>

          <button type="button" className="ovf-card-foot" onClick={() => onNavigate("candidates")}>
            See details <ArrowRight size={16} />
          </button>
        </article>

        <article className="ovf-card">
          <div className="ovf-card-top">
            <span className="ovf-card-icon">
              <UserCheck size={18} strokeWidth={2.1} />
            </span>
            <div>
              <h2 className="ovf-card-title">Verified profiles</h2>
              <p className="ovf-card-sub">Read and signed off by a person</p>
            </div>
          </div>

          <div className="ovf-card-value">
            {formatInt(verified)}
            <Delta pct={deltaVerified.percent} against={against} />
          </div>

          <button type="button" className="ovf-card-foot" onClick={() => onNavigate("candidates")}>
            View summary <ArrowRight size={16} />
          </button>
        </article>

        <article className="ovf-card">
          <div className="ovf-card-top">
            <span className="ovf-card-icon">
              <FileSearch size={18} strokeWidth={2.1} />
            </span>
            <div>
              <h2 className="ovf-card-title">Needs review</h2>
              <p className="ovf-card-sub">Parsed below the confidence line</p>
            </div>
          </div>

          <div className="ovf-card-value">{formatInt(review)}</div>

          <button type="button" className="ovf-card-foot" onClick={() => onNavigate("staff")}>
            Open the queue <ArrowRight size={16} />
          </button>
        </article>
      </div>

      {/* ── Pipeline beside intake ────────────────────────────────────── */}
      <div className="ovf-split">
        <section className="ovf-panel ovf-pipeline">
          <div className="ovf-panel-head">
            <div>
              <h2 className="ovf-panel-title">Pipeline</h2>
              <p className="ovf-panel-sub">
                {candidates.length > 0
                  ? `${formatInt(candidates.length)} profiles on file today`
                  : "Nothing on file yet"}
              </p>
            </div>
            <button type="button" className="ovf-pill-btn" onClick={() => onNavigate("staff")}>
              <UserPlus size={14} /> Allocate
            </button>
          </div>

          <div className="ovf-tiles">
            {[
              {
                key: "verified",
                label: "Verified",
                value: verified,
                note: share(verified),
                state: "Cleared",
                tone: "ok" as const,
                to: "candidates" as NavId,
              },
              {
                key: "progress",
                label: "In progress",
                value: active,
                note: share(active),
                state: "Active",
                tone: "ok" as const,
                to: "candidates" as NavId,
              },
              {
                key: "review",
                label: "Needs review",
                value: review,
                note: share(review),
                state: review > 0 ? "Action req." : "Clear",
                tone: review > 0 ? ("warn" as const) : ("ok" as const),
                to: "candidates" as NavId,
              },
              {
                key: "unassigned",
                label: "Unassigned",
                value: unassigned,
                note: share(unassigned),
                state: unassigned > 0 ? "Waiting" : "All allocated",
                tone: unassigned > 0 ? ("warn" as const) : ("ok" as const),
                to: "staff" as NavId,
              },
            ].map((tile) => (
              <button
                key={tile.key}
                type="button"
                className="ovf-tile"
                onClick={() => onNavigate(tile.to)}
              >
                <span className="ovf-tile-head">
                  <i className={`ovf-tile-dot is-${tile.tone}`} aria-hidden="true" />
                  {tile.label}
                </span>
                <span className="ovf-tile-value">{formatInt(tile.value)}</span>
                <span className="ovf-tile-note">{tile.note}</span>
                <span className={`ovf-tile-state is-${tile.tone}`}>{tile.state}</span>
              </button>
            ))}
          </div>
        </section>

        <section className="ovf-panel ovf-flow-panel">
          <div className="ovf-panel-head">
            <div>
              <p className="ovf-panel-eyebrow">Candidates parsed</p>
              <p className="ovf-flow-total">{formatInt(flowTotal)}</p>
            </div>

            <div className="ovf-seg is-quiet" role="group" aria-label="Chart grain">
              <button
                type="button"
                className={`ovf-seg-btn ${grain === "weekly" ? "is-on" : ""}`}
                onClick={() => setGrain("weekly")}
              >
                Weekly
              </button>
              <button
                type="button"
                className={`ovf-seg-btn ${grain === "monthly" ? "is-on" : ""}`}
                onClick={() => setGrain("monthly")}
              >
                Monthly
              </button>
            </div>
          </div>

          <FlowBarChart buckets={flow} caption="Parsed" />
        </section>
      </div>

      {/* ── The profiles themselves ───────────────────────────────────── */}
      <section className="ovf-panel">
        <div className="ovf-panel-head">
          <h2 className="ovf-panel-title">Recent activity</h2>

          <div className="ovf-panel-tools">
            <label className="ovf-search">
              <Search size={15} />
              <input
                type="search"
                value={query}
                placeholder="Search"
                onChange={(event) => setQuery(event.target.value)}
                aria-label="Search recent candidates"
              />
            </label>
            <button type="button" className="ovf-ghost-btn" onClick={() => onNavigate("candidates")}>
              <SlidersHorizontal size={14} /> Filter
            </button>
          </div>
        </div>

        {recent.length === 0 ? (
          <p className="ovf-empty">
            {candidates.length === 0
              ? "Nothing has been parsed yet. Run a sync to bring résumés in."
              : "No profile matches that search."}
          </p>
        ) : (
          <div className="ovf-table-wrap">
            <table className="ovf-table">
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Designation</th>
                  <th>Date</th>
                  <th>Time</th>
                  <th>Confidence</th>
                  <th>Status</th>
                  <th aria-label="Open" />
                </tr>
              </thead>
              <tbody>
                {recent.map((candidate) => {
                  const name = candidateNameOf(candidate);
                  const confidence = candidate.profile?.confidence;
                  const state = isVerified(candidate)
                    ? { label: "Verified", tone: "ok" }
                    : needsReview(candidate)
                      ? { label: "Needs review", tone: "warn" }
                      : { label: "In progress", tone: "info" };

                  return (
                    <tr key={candidate.id} onClick={() => onOpenCandidate(candidate)}>
                      <td>
                        <span className="ovf-who">
                          <span className="ovf-avatar" aria-hidden="true">
                            {initialsOf(name)}
                          </span>
                          <span className="ovf-who-text">
                            <strong>{name}</strong>
                            <small>{candidate.profile?.email ?? "No email on file"}</small>
                          </span>
                        </span>
                      </td>
                      <td>{candidate.profile?.current_designation || "—"}</td>
                      <td>
                        {candidate.created_at ? formatDateFull(new Date(candidate.created_at)) : "—"}
                      </td>
                      <td>{clockOf(candidate.created_at)}</td>
                      <td>
                        {typeof confidence === "number" ? `${Math.round(confidence * 100)}%` : "—"}
                      </td>
                      <td>
                        <span className={`ovf-status is-${state.tone}`}>
                          <i aria-hidden="true" />
                          {state.label}
                        </span>
                      </td>
                      <td>
                        <span className="ovf-open">
                          <ArrowRight size={15} />
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        <button type="button" className="ovf-see-all" onClick={() => onNavigate("candidates")}>
          See every candidate <ArrowRight size={15} />
        </button>
      </section>
    </div>
  );
}
