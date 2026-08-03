"use client";

import React from "react";
import { ArrowDownRight, ArrowUpRight, Minus, type LucideIcon } from "lucide-react";
import Sparkline from "./Sparkline";
import { formatInt, type Delta } from "@/lib/dashboardMetrics";

/**
 * The dashboard tile carries a sparkline and a delta, so it keeps its own
 * taller shell — but it takes its colour from the product's shared tone
 * vocabulary rather than hand-picked hex per tile. There is no amber: at
 * border widths it reads brown.
 */
export type DashTone = "blue" | "green" | "cyan" | "navy" | "yellow";

/** Border and chip come from CSS; the SVG needs the values in JS. */
const TONE_INK: Record<DashTone, { accent: string; muted: string }> = {
  blue: { accent: "#0b5fff", muted: "#b9d3ff" },
  green: { accent: "#047857", muted: "#a7f3d0" },
  cyan: { accent: "#0891b2", muted: "#a5e8f7" },
  navy: { accent: "#0a2472", muted: "#c3cfe9" },
  yellow: { accent: "#f59e0b", muted: "#fde68a" },
};

interface StatTileProps {
  label: string;
  value: string;
  icon: LucideIcon;
  tone?: DashTone;
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
  tone = "blue",
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
  const ink = TONE_INK[tone];

  return (
    <article className={`metric-tile tone-${tone}`}>
      <div className="metric-tile-head">
        <span className="metric-tile-chip">
          <Icon size={16} strokeWidth={2} />
        </span>
        <Sparkline values={trend} accent={ink.accent} muted={ink.muted} />
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
