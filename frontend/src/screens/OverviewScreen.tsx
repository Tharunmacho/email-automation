"use client";

import { useMemo, useState } from "react";
import {
  ArrowRight,
  Briefcase,
  Calendar,
  CheckCircle2,
  Cpu,
  Eye,
  FileSearch,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  Users,
} from "lucide-react";

import IngestionChart, { RANGE_OPTIONS, type RangeOption } from "@/components/dashboard/IngestionChart";
import RecentCandidates from "@/components/dashboard/RecentCandidates";
import WeeklyBarChart from "@/components/dashboard/WeeklyBarChart";
import SlaGauge from "@/components/dashboard/SlaGauge";
import PipelineSplit from "@/components/dashboard/PipelineSplit";
import DashboardSkeleton from "@/components/dashboard/DashboardSkeleton";
import {
  buildDailyBuckets,
  isVerified,
  needsReview,
  windowDelta,
  buildCumulativeTrend,
} from "@/lib/dashboardMetrics";
import { compactNumber, formatDateFull, formatInt } from "@/lib/format";
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
const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

/** Green ▲ or red ▼ delta badge — only shows when delta data exists */
function DeltaBadge({ pct }: { pct: number | null }) {
  if (pct === null) return null;
  const up = pct >= 0;
  return (
    <span className={`ov-delta ${up ? "is-up" : "is-down"}`}>
      {up ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
      {Math.abs(pct).toFixed(1)}%
    </span>
  );
}

/**
 * `Date.now()` read from module scope rather than from the render body.
 *
 * The screen is a pure function of the candidate collection everywhere else,
 * and the clock is the one input that is not — reading it inline makes two
 * renders of the same props produce two different pages. These are the only
 * doors it comes through, and each is called from a `useMemo` or from behind
 * the mount gate, so the value is taken once per window rather than per paint.
 */
function daysAgo(days: number): Date {
  return new Date(Date.now() - days * DAY_MS);
}

function rightNow(): Date {
  return new Date();
}

/** Count how many candidates arrived on each day of the week (Sun=0…Sat=6) */
function buildWeeklyActivity(candidates: CandidateRecord[]): number[] {
  const counts = [0, 0, 0, 0, 0, 0, 0];
  const cutoff = new Date(Date.now() - 28 * DAY_MS);
  for (const c of candidates) {
    if (!c.created_at) continue;
    const d = new Date(c.created_at);
    if (!Number.isNaN(d.getTime()) && d >= cutoff) counts[d.getDay()] += 1;
  }
  return counts;
}

/**
 * Everything the whole product is doing, on one screen.
 *
 * The layout is four readings across the top, then a wide column carrying the
 * two things that change over time — what has been ingested, and who arrived —
 * beside a narrow column carrying the three that describe the current state.
 * Nothing here is a decoration: every card is drawn from the candidate
 * collection the page already holds, so an empty workspace shows an empty
 * dashboard rather than a demo one.
 */
export default function OverviewScreen({
  total,
  candidates,
  logs,
  onNavigate,
  onOpenCandidate,
}: OverviewScreenProps) {
  const [range, setRange] = useState<RangeOption>(30);
  const mounted = useIsMounted();

  /* ── Derived metrics (all from live candidate data, nothing hardcoded) ── */
  const verified  = candidates.filter(isVerified).length;
  const review    = candidates.filter(needsReview).length;
  const active    = candidates.filter((c) => c.status !== "verified" && !needsReview(c)).length;
  const scored    = candidates.filter((c) => typeof c.profile?.confidence === "number");

  const avgConfidence =
    scored.length > 0
      ? (scored.reduce((s, c) => s + (c.profile.confidence ?? 0), 0) / scored.length) * 100
      : 0;

  const verifiedRate =
    candidates.length > 0 ? Math.round((verified / candidates.length) * 100) : 0;

  /* ── Chart data ── */
  const buckets = useMemo(() => buildDailyBuckets(candidates, range), [candidates, range]);
  // The same window, one period back. Drawn behind the live series as the
  // dashed ghost line, which is what turns a curve into a comparison.
  const previous = useMemo(
    () => buildDailyBuckets(candidates, range, daysAgo(range)),
    [candidates, range],
  );

  const windowTotal   = buckets.reduce((s, b) => s + b.added, 0);
  const previousTotal = previous.reduce((s, b) => s + b.added, 0);
  const windowPct     = previousTotal > 0
    ? ((windowTotal - previousTotal) / previousTotal) * 100
    : null;

  const trend30       = useMemo(() => buildCumulativeTrend(candidates, 30), [candidates]);
  const deltaTotal    = windowDelta(trend30, 7);
  const verifiedTrend = useMemo(() => buildCumulativeTrend(candidates, 30, isVerified), [candidates]);
  const deltaVerified = windowDelta(verifiedTrend, 7);

  const weeklyData = useMemo(() => buildWeeklyActivity(candidates), [candidates]);
  const weeklyPeak = Math.max(...weeklyData, 0);
  const peakDay    = weeklyData.indexOf(weeklyPeak);

  // The label under the title, stated the way the reference states its range:
  // the two dates the window actually spans, not the number of days in it.
  const windowLabel = mounted
    ? `${formatDateFull(daysAgo(range - 1))} – ${formatDateFull(rightNow())}`
    : "";

  if (!mounted) return <DashboardSkeleton />;

  return (
    <div className="ov-shopeers">

      {/* ── Page header ── */}
      <header className="ov-page-head">
        <div>
          <h1 className="ov-page-title">Dashboard</h1>
          <p className="ov-page-sub">
            Every résumé the pipeline has parsed, and what still needs a person.
          </p>
        </div>

        <div className="ov-page-actions">
          <span className="ov-hdr-chip">
            <Calendar size={14} />
            {windowLabel}
          </span>

          <label className="ov-hdr-select">
            <select
              value={range}
              onChange={(e) => setRange(Number(e.target.value) as RangeOption)}
              aria-label="Dashboard time range"
            >
              {RANGE_OPTIONS.map((o) => (
                <option key={o} value={o}>Last {o} days</option>
              ))}
            </select>
          </label>

          <button type="button" className="ov-hdr-btn" onClick={() => onNavigate("sourcing")}>
            <RefreshCw size={14} />
            Sourcing
          </button>


        </div>
      </header>

      {/* ── KPI row (4 cards) ── */}
      <div className="ov-kpi-row">
        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Total Candidates</span>
            <span className="ov-kpi-card-icon">
              <Users size={17} strokeWidth={2.1} />
            </span>
          </div>
          <div className="ov-kpi-card-mid">
            <p className="ov-kpi-card-value">{compactNumber(total)}</p>
            <DeltaBadge pct={deltaTotal.percent} />
          </div>
          <div className="ov-kpi-card-foot">
            <span className="ov-kpi-card-caption">
              vs. {formatInt(Math.max(0, total - deltaTotal.change))} last week
            </span>
          </div>
        </article>

        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Verified</span>
            <span className="ov-kpi-card-icon is-success">
              <CheckCircle2 size={17} strokeWidth={2.1} />
            </span>
          </div>
          <div className="ov-kpi-card-mid">
            <p className="ov-kpi-card-value">{compactNumber(verified)}</p>
            <DeltaBadge pct={deltaVerified.percent} />
          </div>
          <div className="ov-kpi-card-foot">
            <span className="ov-kpi-card-caption">
              vs. {formatInt(Math.max(0, verified - deltaVerified.change))} last week
            </span>
          </div>
        </article>

        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Avg AI Confidence</span>
            <span className="ov-kpi-card-icon is-warning">
              <Cpu size={17} strokeWidth={2.1} />
            </span>
          </div>
          <div className="ov-kpi-card-mid">
            <p className="ov-kpi-card-value">{avgConfidence.toFixed(1)}%</p>
          </div>
          <div className="ov-kpi-card-foot">
            <span className="ov-kpi-card-caption">
              {scored.length === 0
                ? "No scored résumés yet"
                : `Across ${formatInt(scored.length)} scored résumé${scored.length === 1 ? "" : "s"}`}
            </span>
          </div>
        </article>

        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Pending Review</span>
            <span className={`ov-kpi-card-icon ${review > 0 ? "is-rose" : "is-success"}`}>
              <FileSearch size={17} strokeWidth={2.1} />
            </span>
          </div>
          <div className="ov-kpi-card-mid">
            <p className={`ov-kpi-card-value ${review > 0 ? "is-rose" : ""}`}>
              {compactNumber(review)}
            </p>
          </div>
          <div className="ov-kpi-card-foot">
            <span className="ov-kpi-card-caption">
              {review === 0 ? "All profiles cleared" : "Waiting on a human verdict"}
            </span>
          </div>
        </article>
      </div>

      {/* ── Main 2-column grid ── */}
      <div className="ov-grid">

        {/* LEFT column */}
        <div className="ov-grid-left">

          {/* Sourced Candidates — figure on the left, plot on the right, and
              the pipeline split nested underneath both. */}
          <section className="ov-chart-card">
            <div className="ov-chart-card-head">
              <h2 className="ov-chart-card-title">Sourced Candidates</h2>
              <span className="ov-legend">
                <span className="ov-legend-item">
                  <i className="ov-legend-swatch is-line" />
                  This period
                </span>
                <span className="ov-legend-item">
                  <i className="ov-legend-swatch is-ghost" />
                  Previous
                </span>
              </span>
            </div>

            <div className="ov-hero">
              <div className="ov-hero-figure">
                <p className="ov-hero-value">{formatInt(windowTotal)}</p>
                <div className="ov-hero-delta">
                  <DeltaBadge pct={windowPct} />
                  <span className="ov-kpi-card-caption">vs. last period</span>
                </div>
                <p className="ov-hero-note">
                  {formatInt(previousTotal)} parsed in the {range} days before this one
                </p>
              </div>

              <div className="ov-hero-plot">
                <IngestionChart buckets={buckets} compare={previous} range={range} />
              </div>
            </div>

            <div className="ov-chart-card-foot">
              <PipelineSplit
                title="Pipeline"
                segments={[
                  {
                    label: "Verified",
                    value: verified,
                    icon: CheckCircle2,
                    tone: "success",
                    onSelect: () => onNavigate("candidates"),
                  },
                  {
                    label: "In progress",
                    value: active,
                    icon: Users,
                    tone: "info",
                    onSelect: () => onNavigate("candidates"),
                  },
                  {
                    label: "Pending review",
                    value: review,
                    icon: FileSearch,
                    tone: "warning",
                    onSelect: () => onNavigate("candidates"),
                  },
                ]}
              />
            </div>
          </section>

          {/* Recent Candidates table */}
          <RecentCandidates
            candidates={candidates}
            onOpenCandidate={onOpenCandidate}
            onViewAll={() => onNavigate("candidates")}
          />
        </div>

        {/* RIGHT column */}
        <div className="ov-grid-right">

          {/* Most Active Day */}
          <section className="ov-side-card">
            <div className="ov-side-card-head">
              <h3 className="ov-side-card-title">Most Active Day</h3>
              <p className="ov-side-card-sub">
                {weeklyPeak > 0
                  ? `${DAY_NAMES[peakDay]} is the busiest — last 4 weeks`
                  : "Nothing has arrived in the last 4 weeks"}
              </p>
            </div>
            <WeeklyBarChart data={weeklyData} />
          </section>

          {/* Verification Rate gauge */}
          <section className="ov-side-card">
            <div className="ov-side-card-head">
              <h3 className="ov-side-card-title">Verification Rate</h3>
            </div>
            <SlaGauge
              percent={verifiedRate}
              label="Verification Rate"
              caption={
                candidates.length === 0
                  ? "Nothing to verify yet"
                  : `${formatInt(verified)} of ${formatInt(candidates.length)} cleared`
              }
            />
            <button
              type="button"
              className="ov-gauge-cta"
              onClick={() => onNavigate("candidates")}
            >
              Show details <ArrowRight size={14} />
            </button>
          </section>

          {/* AI parser score — the product's own confidence in itself */}
          <section className="ov-side-card ov-ai-card">
            <div className="ov-side-card-head">
              <h3 className="ov-side-card-title">AI Parser</h3>
              <p className="ov-side-card-sub">Mean confidence across every scored résumé</p>
            </div>

            <div className="ov-ai-orb-wrap">
              <span className="ov-ai-orb" aria-hidden="true" />
              <span className="ov-ai-score">
                {avgConfidence.toFixed(0)}
                <span className="ov-ai-pct">%</span>
              </span>
            </div>

            <div className="ov-ai-foot">
              <span className="ov-kpi-card-caption">{formatInt(scored.length)} scored</span>
              <button type="button" className="ov-ai-link" onClick={() => onNavigate("settings")}>
                Tune engine <ArrowRight size={13} />
              </button>
            </div>
          </section>

          {/* Quick Actions */}
          <section className="ov-side-card">
            <div className="ov-side-card-head">
              <h3 className="ov-side-card-title">Quick Actions</h3>
            </div>
            <div className="ov-quick-actions">
              <button type="button" className="ov-quick-btn" onClick={() => onNavigate("candidates")}>
                <span className="ov-quick-icon"><Users size={15} /></span>
                <span>Candidates Pool</span>
                <ArrowRight size={14} className="ov-quick-arrow" />
              </button>
              <button type="button" className="ov-quick-btn" onClick={() => onNavigate("job-orders")}>
                <span className="ov-quick-icon"><Briefcase size={15} /></span>
                <span>Job Orders</span>
                <ArrowRight size={14} className="ov-quick-arrow" />
              </button>
              <button type="button" className="ov-quick-btn" onClick={() => onNavigate("activity")}>
                <span className="ov-quick-icon"><Eye size={15} /></span>
                <span>Activity Logs</span>
                <ArrowRight size={14} className="ov-quick-arrow" />
              </button>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
