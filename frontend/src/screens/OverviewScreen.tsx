"use client";

import { useMemo, useState } from "react";
import { ArrowRight, Briefcase, CheckCircle2, Cpu, FileText, Users } from "lucide-react";

import IngestionChart, { RANGE_OPTIONS, type RangeOption } from "@/components/dashboard/IngestionChart";
import ActivityLog, { type LogEntry } from "@/components/dashboard/ActivityLog";
import RecentCandidates from "@/components/dashboard/RecentCandidates";
import DashboardSkeleton from "@/components/dashboard/DashboardSkeleton";
import { buildDailyBuckets, isVerified, needsReview } from "@/lib/dashboardMetrics";
import { compactNumber, formatInt } from "@/lib/format";
import { useIsMounted } from "@/lib/useIsMounted";
import type { NavId } from "@/lib/nav";
import type { CandidateRecord } from "@/lib/api";

interface OverviewScreenProps {
  total: number;
  candidates: CandidateRecord[];
  logs: LogEntry[];
  onNavigate: (id: NavId) => void;
  onOpenCandidate: (candidate: CandidateRecord) => void;
}

interface QuickAction {
  id: string;
  icon: typeof FileText;
  title: string;
  subtitle: string;
  accent?: boolean;
  arrow?: boolean;
  onClick: () => void;
}

/**
 * The Overview.
 *
 * Four things to do, four numbers that say whether the pipeline is healthy, the
 * live trace, and then the detail: what came in over time and who arrived.
 *
 * The quick-ingestion panel that used to sit beside the chart is gone. Its one
 * working control — run a sync — is in the header on every screen, and its
 * status rows repeated what Settings reports properly. Removing it lets the
 * chart use the full column width, which is what a thirty-day series needs.
 *
 * Every figure is derived from the live candidate collection rather than stored
 * separately, so a KPI cannot drift out of step with the list it summarises.
 */
export default function OverviewScreen({
  total,
  candidates,
  logs,
  onNavigate,
  onOpenCandidate,
}: OverviewScreenProps) {
  const [range, setRange] = useState<RangeOption>(30);

  /**
   * Every day-bucketed series is anchored to "now". The server's clock and
   * timezone are not the viewer's, so date-derived UI is held back until the
   * browser has mounted and owns the clock — otherwise the two renders disagree
   * and React throws a hydration error.
   */
  const mounted = useIsMounted();

  const verified = candidates.filter(isVerified).length;
  const review = candidates.filter(needsReview).length;

  const scored = candidates.filter((c) => typeof c.profile?.confidence === "number");
  const avgConfidence =
    scored.length > 0
      ? (scored.reduce((sum, c) => sum + (c.profile.confidence ?? 0), 0) / scored.length) * 100
      : 0;

  const verifiedRate = candidates.length > 0 ? Math.round((verified / candidates.length) * 100) : 0;

  const buckets = useMemo(() => buildDailyBuckets(candidates, range), [candidates, range]);
  const windowTotal = buckets.reduce((sum, bucket) => sum + bucket.added, 0);

  if (!mounted) return <DashboardSkeleton />;

  const actions: QuickAction[] = [
    {
      id: "pool",
      icon: Users,
      title: "Candidates Pool",
      subtitle: "Browse, filter and verify every parsed profile.",
      accent: true,
      arrow: true,
      onClick: () => onNavigate("candidates"),
    },
    {
      id: "jobs",
      icon: Briefcase,
      title: "Job Orders",
      subtitle: "Match the candidate pool to active requisitions.",
      onClick: () => onNavigate("job-orders"),
    },
    {
      id: "parser",
      icon: FileText,
      title: "Resume Parser",
      subtitle: "Confidence breakdown and the fields the engine extracts.",
      onClick: () => onNavigate("resume-parser"),
    },
    {
      id: "engine",
      icon: Cpu,
      title: "AI Engine",
      subtitle: "Resume parser model active & scoring.",
      onClick: () => onNavigate("settings"),
    },
  ];

  const kpis = [
    {
      label: "Total candidates",
      value: compactNumber(total),
      caption: "Total extracted candidate profiles",
      icon: Users,
    },
    {
      label: "Verified rate",
      value: `${verifiedRate}%`,
      caption: `${formatInt(verified)} signed off without further review`,
      icon: CheckCircle2,
    },
    {
      label: "Avg confidence",
      value: `${avgConfidence.toFixed(1)}%`,
      caption: `Across ${formatInt(scored.length)} scored resume parse${scored.length === 1 ? "" : "s"}`,
      icon: Cpu,
    },
    {
      label: "Pending review",
      value: formatInt(review),
      caption: "Profiles requiring manual recruiter review",
      icon: Briefcase,
    },
  ];

  return (
    <>
      <section className="ov-actions">
        {actions.map((action) => {
          const Icon = action.icon;
          return (
            <button
              key={action.id}
              type="button"
              className={`ov-action ${action.accent ? "is-accent" : ""}`}
              onClick={action.onClick}
            >
              <span className="ov-action-icon" aria-hidden="true">
                <Icon size={19} strokeWidth={2} />
              </span>
              <span className="ov-action-text">
                <span className="ov-action-title">
                  {action.title}
                  {action.arrow && <ArrowRight size={14} />}
                </span>
                <span className="ov-action-sub">{action.subtitle}</span>
              </span>
            </button>
          );
        })}
      </section>

      <section className="ov-kpis">
        {kpis.map((kpi) => {
          const Icon = kpi.icon;
          return (
            <article key={kpi.label} className="ov-kpi">
              <p className="ov-kpi-label">
                {kpi.label}
                <Icon size={15} strokeWidth={2} />
              </p>
              <p className="ov-kpi-value">{kpi.value}</p>
              <p className="ov-kpi-caption">{kpi.caption}</p>
            </article>
          );
        })}
      </section>

      {/* The live trace sits directly under the headline numbers, above the
          analytics. It is what you watch while a sync runs — burying it below a
          chart means the one thing that moves in real time is off-screen at the
          moment it matters. */}
      <ActivityLog logs={logs} />

      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">Sourced Candidates</h3>
            <p className="db-card-sub">
              {formatInt(windowTotal)} parsed in the last {range} days.
            </p>
          </div>
          <select
            className="ov-select"
            value={range}
            onChange={(event) => setRange(Number(event.target.value) as RangeOption)}
            aria-label="Chart time range"
          >
            {RANGE_OPTIONS.map((option) => (
              <option key={option} value={option}>
                Last {option} days
              </option>
            ))}
          </select>
        </header>
        <div className="db-card-body">
          <IngestionChart buckets={buckets} range={range} />
        </div>
      </section>

      <RecentCandidates candidates={candidates} onOpenCandidate={onOpenCandidate} />
    </>
  );
}
