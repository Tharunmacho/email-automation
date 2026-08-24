"use client";

import { useMemo, useState } from "react";
import {
  ArrowRight,
  Briefcase,
  CheckCircle2,
  Cpu,
  RefreshCw,
  TrendingDown,
  TrendingUp,
  Users,
  FileSearch,
} from "lucide-react";

import IngestionChart, { RANGE_OPTIONS, type RangeOption } from "@/components/dashboard/IngestionChart";
import RecentCandidates from "@/components/dashboard/RecentCandidates";
import WeeklyBarChart from "@/components/dashboard/WeeklyBarChart";
import SlaGauge from "@/components/dashboard/SlaGauge";
import PipelineDonut from "@/components/dashboard/PipelineDonut";
import DashboardSkeleton from "@/components/dashboard/DashboardSkeleton";
import {
  buildDailyBuckets,
  isVerified,
  needsReview,
  windowDelta,
  buildCumulativeTrend,
} from "@/lib/dashboardMetrics";
import { compactNumber, formatInt } from "@/lib/format";
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

/** Count how many candidates arrived on each day of the week (Sun=0…Sat=6) */
function buildWeeklyActivity(candidates: CandidateRecord[]): number[] {
  const counts = [0, 0, 0, 0, 0, 0, 0];
  const cutoff = new Date(Date.now() - 28 * 24 * 60 * 60 * 1000);
  for (const c of candidates) {
    if (!c.created_at) continue;
    const d = new Date(c.created_at);
    if (!Number.isNaN(d.getTime()) && d >= cutoff) counts[d.getDay()] += 1;
  }
  return counts;
}

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
  const buckets     = useMemo(() => buildDailyBuckets(candidates, range), [candidates, range]);
  const windowTotal = buckets.reduce((s, b) => s + b.added, 0);

  const trend30       = useMemo(() => buildCumulativeTrend(candidates, 30), [candidates]);
  const deltaTotal    = windowDelta(trend30, 7);
  const verifiedTrend = useMemo(() => buildCumulativeTrend(candidates, 30, isVerified), [candidates]);
  const deltaVerified = windowDelta(verifiedTrend, 7);

  const weeklyData = useMemo(() => buildWeeklyActivity(candidates), [candidates]);
  const peakDay    = weeklyData.indexOf(Math.max(...weeklyData));

  /* ── Pipeline donut slices (live) ── */
  const pipelineSlices = [
    { label: "Verified",       value: verified, color: "var(--success)" },
    { label: "Active",         value: active,   color: "var(--primary)" },
    { label: "Pending Review", value: review,   color: "var(--warning)" },
  ].filter((s) => s.value > 0);

  if (!mounted) return <DashboardSkeleton />;

  return (
    <div className="ov-shopeers">

      {/* ── KPI row (4 cards) ── */}
      <div className="ov-kpi-row">
        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Total Candidates</span>
            <span className="ov-kpi-card-icon">
              <Users size={17} />
            </span>
          </div>
          <p className="ov-kpi-card-value">{compactNumber(total)}</p>
          <div className="ov-kpi-card-foot">
            <DeltaBadge pct={deltaTotal.percent} />
            <span className="ov-kpi-card-caption">vs. last 7 days</span>
          </div>
        </article>

        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Verified</span>
            <span className="ov-kpi-card-icon is-success">
              <CheckCircle2 size={17} />
            </span>
          </div>
          <p className="ov-kpi-card-value">{formatInt(verified)}</p>
          <div className="ov-kpi-card-foot">
            <DeltaBadge pct={deltaVerified.percent} />
            <span className="ov-kpi-card-caption">vs. last 7 days</span>
          </div>
        </article>

        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Avg AI Confidence</span>
            <span className="ov-kpi-card-icon is-warning">
              <Cpu size={17} />
            </span>
          </div>
          <p className="ov-kpi-card-value">{avgConfidence.toFixed(1)}%</p>
          <div className="ov-kpi-card-foot">
            <span className="ov-kpi-card-caption">
              {scored.length === 0
                ? "No scored résumés yet"
                : `Across ${formatInt(scored.length)} résumé${scored.length === 1 ? "" : "s"}`}
            </span>
          </div>
        </article>

        <article className="ov-kpi-card">
          <div className="ov-kpi-card-top">
            <span className="ov-kpi-card-label">Pending Review</span>
            <span className={`ov-kpi-card-icon ${review > 0 ? "is-rose" : "is-success"}`}>
              <FileSearch size={17} />
            </span>
          </div>
          <p className={`ov-kpi-card-value ${review > 0 ? "is-rose" : ""}`}>
            {formatInt(review)}
          </p>
          <div className="ov-kpi-card-foot">
            <span className="ov-kpi-card-caption">
              {review === 0 ? "All profiles cleared" : "Need manual review"}
            </span>
          </div>
        </article>
      </div>

      {/* ── Main 2-column grid ── */}
      <div className="ov-grid">

        {/* LEFT column */}
        <div className="ov-grid-left">

          {/* Sourced Candidates line chart */}
          <section className="ov-chart-card">
            <div className="ov-chart-card-head">
              <div>
                <h2 className="ov-chart-card-title">Sourced Candidates</h2>
                <p className="ov-chart-card-sub">
                  <span className="ov-chart-big">{formatInt(windowTotal)}</span>
                  <span className="ov-kpi-card-caption"> parsed in the last {range} days</span>
                </p>
              </div>
              <select
                className="ov-select"
                value={range}
                onChange={(e) => setRange(Number(e.target.value) as RangeOption)}
                aria-label="Chart time range"
              >
                {RANGE_OPTIONS.map((o) => (
                  <option key={o} value={o}>Last {o} days</option>
                ))}
              </select>
            </div>
            <div className="ov-chart-card-body">
              <IngestionChart buckets={buckets} range={range} />
            </div>
          </section>

          {/* Pipeline breakdown + Most Active Day — side by side */}
          <div className="ov-bottom-row">

            {/* Donut — Pipeline Breakdown */}
            <section className="ov-chart-card ov-bottom-card">
              <div className="ov-chart-card-head">
                <div>
                  <h2 className="ov-chart-card-title">Pipeline Breakdown</h2>
                  <p className="ov-chart-card-sub">
                    <span className="ov-kpi-card-caption">Status distribution of all {formatInt(total)} candidates</span>
                  </p>
                </div>
              </div>
              <div className="ov-chart-card-body">
                <PipelineDonut
                  slices={pipelineSlices}
                  total={candidates.length}
                  centerLabel="total"
                  centerValue={compactNumber(total)}
                />
              </div>
            </section>

            {/* Bar chart — Most Active Day */}
            <section className="ov-chart-card ov-bottom-card">
              <div className="ov-chart-card-head">
                <div>
                  <h2 className="ov-chart-card-title">Most Active Day</h2>
                  <p className="ov-chart-card-sub">
                    {weeklyData.some((v) => v > 0) ? (
                      <>
                        <span className="ov-chart-big" style={{ fontSize: "1.2rem" }}>
                          {["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][peakDay]}
                        </span>
                        <span className="ov-kpi-card-caption"> · {weeklyData[peakDay]} candidates (last 4 weeks)</span>
                      </>
                    ) : (
                      <span className="ov-kpi-card-caption">No data yet</span>
                    )}
                  </p>
                </div>
              </div>
              <div className="ov-chart-card-body">
                <WeeklyBarChart data={weeklyData} />
              </div>
            </section>
          </div>

          {/* Recent Candidates table */}
          <RecentCandidates candidates={candidates} onOpenCandidate={onOpenCandidate} />
        </div>

        {/* RIGHT column */}
        <div className="ov-grid-right">

          {/* Verification Rate gauge */}
          <section className="ov-side-card">
            <div className="ov-side-card-head">
              <h3 className="ov-side-card-title">Verification Rate</h3>
              <p className="ov-side-card-sub">
                {verifiedRate === 0
                  ? "No verified profiles yet"
                  : `${formatInt(verified)} of ${formatInt(candidates.length)} profiles cleared`}
              </p>
            </div>
            <SlaGauge
              percent={verifiedRate}
              label="Verification Rate"
            />
            <button
              type="button"
              className="ov-gauge-cta"
              onClick={() => onNavigate("candidates")}
            >
              View candidates <ArrowRight size={14} />
            </button>
          </section>

          {/* Quick Actions */}
          <section className="ov-side-card">
            <div className="ov-side-card-head">
              <h3 className="ov-side-card-title">Quick Actions</h3>
            </div>
            <div className="ov-quick-actions">
              <button type="button" className="ov-quick-btn" onClick={() => onNavigate("candidates")}>
                <span className="ov-quick-icon"><Users size={16} /></span>
                <span>Candidates Pool</span>
                <ArrowRight size={14} className="ov-quick-arrow" />
              </button>
              <button type="button" className="ov-quick-btn" onClick={() => onNavigate("job-orders")}>
                <span className="ov-quick-icon"><Briefcase size={16} /></span>
                <span>Job Orders</span>
                <ArrowRight size={14} className="ov-quick-arrow" />
              </button>
              <button type="button" className="ov-quick-btn" onClick={() => onNavigate("sourcing")}>
                <span className="ov-quick-icon"><RefreshCw size={16} /></span>
                <span>Sourcing Hub</span>
                <ArrowRight size={14} className="ov-quick-arrow" />
              </button>
              <button type="button" className="ov-quick-btn" onClick={() => onNavigate("settings")}>
                <span className="ov-quick-icon"><Cpu size={16} /></span>
                <span>AI Engine</span>
                <ArrowRight size={14} className="ov-quick-arrow" />
              </button>
            </div>
          </section>

          {/* Live confidence score card */}
          <section className="ov-side-card ov-confidence-card">
            <div className="ov-side-card-head">
              <h3 className="ov-side-card-title">AI Parser Score</h3>
              <p className="ov-side-card-sub">Average confidence across all scored résumés</p>
            </div>
            <div className="ov-conf-row">
              <span className="ov-conf-value">{avgConfidence.toFixed(0)}<span className="ov-conf-pct">%</span></span>
              <div className="ov-conf-bar-wrap">
                <div className="ov-conf-bar-track">
                  <div
                    className="ov-conf-bar-fill"
                    style={{ width: `${Math.min(100, avgConfidence)}%` }}
                  />
                </div>
                <span className="ov-kpi-card-caption">{formatInt(scored.length)} scored</span>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
