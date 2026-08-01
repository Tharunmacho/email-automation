"use client";

import React from "react";
import { ArrowDownRight, ArrowUpRight, Minus, type LucideIcon } from "lucide-react";
import Sparkline from "./Sparkline";
import { formatInt, type Delta } from "@/lib/dashboardMetrics";

interface StatTileProps {
  label: string;
  value: string;
  icon: LucideIcon;
  /** Series colour for the icon chip and the sparkline's current stretch. */
  accent: string;
  accentSoft: string;
  accentMuted: string;
  trend: number[];
  delta?: Delta | null;
  /** How the delta is worded, e.g. "vs previous 7 days". */
  deltaCaption?: string;
  /** Formats the raw delta change for display. */
  formatDelta?: (change: number) => string;
  /** False when a rise is bad (a growing review queue, say). */
  riseIsGood?: boolean;
  footnote?: string;
}

export default function StatTile({
  label,
  value,
  icon: Icon,
  accent,
  accentSoft,
  accentMuted,
  trend,
  delta,
  deltaCaption = "vs previous 7 days",
  formatDelta = (change) => formatInt(Math.abs(change)),
  riseIsGood = true,
  footnote,
}: StatTileProps) {
  const change = delta?.change ?? 0;
  const flat = Math.abs(change) < 0.05;
  const good = riseIsGood ? change > 0 : change < 0;

  const DeltaIcon = flat ? Minus : change > 0 ? ArrowUpRight : ArrowDownRight;
  const deltaTone = flat ? "flat" : good ? "up" : "down";

  return (
    <article className="metric-tile">
      <div className="metric-tile-head">
        <span className="metric-tile-chip" style={{ background: accentSoft, color: accent }}>
          <Icon size={18} strokeWidth={2} />
        </span>
        <Sparkline values={trend} accent={accent} muted={accentMuted} />
      </div>

      <div className="metric-tile-body">
        <span className="metric-tile-label">{label}</span>
        <span className="metric-tile-value">{value}</span>
      </div>

      <div className="metric-tile-foot">
        {delta ? (
          <>
            <span className={`dash-delta dash-delta-${deltaTone}`}>
              <DeltaIcon size={13} strokeWidth={2.5} />
              {flat ? "no change" : formatDelta(change)}
            </span>
            <span className="metric-tile-caption">{deltaCaption}</span>
          </>
        ) : (
          <span className="metric-tile-caption">{footnote}</span>
        )}
      </div>
    </article>
  );
}
