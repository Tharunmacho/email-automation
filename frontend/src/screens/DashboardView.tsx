"use client";

import React, { useMemo, useState } from "react";
import { CheckCircle2, Cpu, Eye, Users } from "lucide-react";

import StatTile from "@/components/dashboard/StatTile";
import IngestionChart, {
  type RangeOption,
} from "@/components/dashboard/IngestionChart";
import RecentCandidates from "@/components/dashboard/RecentCandidates";
import ActivityLog, { type LogEntry } from "@/components/dashboard/ActivityLog";
import DashboardSkeleton from "@/components/dashboard/DashboardSkeleton";
import {
  buildConfidenceTrend,
  buildCumulativeTrend,
  buildDailyBuckets,
  compactNumber,
  formatInt,
  isVerified,
  needsReview,
  windowDelta,
} from "@/lib/dashboardMetrics";
import { useIsMounted } from "@/lib/useIsMounted";
import type { CandidateRecord } from "@/lib/api";

interface DashboardViewProps {
  total: number;
  candidates: CandidateRecord[];
  logs: LogEntry[];
  onOpenCandidate?: (candidate: CandidateRecord) => void;
}

/** Trailing window the stat-tile deltas compare against. */
const DELTA_WINDOW = 7;
/** Points in each stat-tile sparkline. */
const SPARK_DAYS = 14;

export default function DashboardView({
  total,
  candidates,
  logs,
  onOpenCandidate,
}: DashboardViewProps) {
  const [range, setRange] = useState<RangeOption>(14);

  /**
   * Every day-bucketed series below is anchored to "now". The server's clock and
   * timezone are not the viewer's, so the date-derived UI is held back until the
   * browser has mounted and owns the clock — otherwise the two renders disagree
   * and React throws a hydration error.
   */
  const mounted = useIsMounted();

  const verified = candidates.filter(isVerified).length;
  const review = candidates.filter(needsReview).length;

  const buckets = useMemo(
    () => buildDailyBuckets(candidates, range),
    [candidates, range],
  );

  const totalTrend = useMemo(
    () => buildCumulativeTrend(candidates, SPARK_DAYS),
    [candidates],
  );
  const verifiedTrend = useMemo(
    () => buildCumulativeTrend(candidates, SPARK_DAYS, isVerified),
    [candidates],
  );
  const reviewTrend = useMemo(
    () => buildCumulativeTrend(candidates, SPARK_DAYS, needsReview),
    [candidates],
  );
  const confidenceTrend = useMemo(
    () => buildConfidenceTrend(candidates, SPARK_DAYS),
    [candidates],
  );

  const avgConfidence =
    confidenceTrend.length > 0 ? confidenceTrend[confidenceTrend.length - 1].value : 0;
  const scoredCount = candidates.filter(
    (candidate) => typeof candidate.profile?.confidence === "number",
  ).length;

  const verifiedShare = candidates.length > 0 ? Math.round((verified / candidates.length) * 100) : 0;

  if (!mounted) return <DashboardSkeleton />;

  return (
    <div className="tab-content active dash-root">
      <div className="metric-tiles">
        <StatTile
          label="Total candidates"
          value={compactNumber(total)}
          icon={Users}
          accent="#1d4ed8"
          accentSoft="rgba(29, 78, 216, 0.10)"
          accentMuted="#bfdbfe"
          trend={totalTrend.map((point) => point.value)}
          delta={windowDelta(totalTrend, DELTA_WINDOW)}
          formatDelta={(change) => `${change > 0 ? "+" : "−"}${formatInt(Math.abs(change))}`}
        />

        <StatTile
          label="Verified profiles"
          value={compactNumber(verified)}
          icon={CheckCircle2}
          accent="#10b981"
          accentSoft="rgba(16, 185, 129, 0.10)"
          accentMuted="#a7f3d0"
          trend={verifiedTrend.map((point) => point.value)}
          delta={windowDelta(verifiedTrend, DELTA_WINDOW)}
          formatDelta={(change) => `${change > 0 ? "+" : "−"}${formatInt(Math.abs(change))}`}
          deltaCaption={`${verifiedShare}% of the pool`}
        />

        <StatTile
          label="Pending review"
          value={compactNumber(review)}
          icon={Eye}
          accent="#f59e0b"
          accentSoft="rgba(245, 158, 11, 0.12)"
          accentMuted="#fde68a"
          trend={reviewTrend.map((point) => point.value)}
          delta={windowDelta(reviewTrend, DELTA_WINDOW)}
          formatDelta={(change) => `${change > 0 ? "+" : "−"}${formatInt(Math.abs(change))}`}
          riseIsGood={false}
        />

        <StatTile
          label="Avg extraction confidence"
          value={`${avgConfidence.toFixed(1)}%`}
          icon={Cpu}
          accent="#7c3aed"
          accentSoft="rgba(124, 58, 237, 0.10)"
          accentMuted="#ddd6fe"
          trend={confidenceTrend.map((point) => point.value)}
          delta={windowDelta(confidenceTrend, DELTA_WINDOW)}
          formatDelta={(change) => `${change > 0 ? "+" : "−"}${Math.abs(change).toFixed(1)} pts`}
          deltaCaption={`across ${scoredCount} scored resume${scoredCount === 1 ? "" : "s"}`}
        />
      </div>

      {/* Live ingestion trace sits directly under the headline numbers — it is
          what you watch while a Gmail sync runs. Analytics follow below. */}
      <ActivityLog logs={logs} />

      <div className="dash-row dash-row-primary">
        <IngestionChart buckets={buckets} range={range} onRangeChange={setRange} />
        <RecentCandidates candidates={candidates} onOpenCandidate={onOpenCandidate} />
      </div>
    </div>
  );
}
