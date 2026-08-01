"use client";

import React from "react";
import type { LucideIcon } from "lucide-react";

interface MetricTileProps {
  label: string;
  value: string | number;
  icon: LucideIcon;
  /** Icon-chip colour. Defaults to the navy accent. */
  accent?: string;
  accentSoft?: string;
  /** Small line under the divider — the context the number needs. */
  caption?: React.ReactNode;
}

/**
 * Compact stat tile for screens whose metrics are not time series. Used by
 * the Job Orders screen. Styling lives in globals.css under METRIC TILES.
 */
export default function MetricTile({
  label,
  value,
  icon: Icon,
  accent = "var(--primary)",
  accentSoft = "rgba(29, 78, 216, 0.10)",
  caption,
}: MetricTileProps) {
  return (
    <article className="metric-tile">
      <div className="metric-tile-head">
        <span className="metric-tile-chip" style={{ background: accentSoft, color: accent }}>
          <Icon size={18} strokeWidth={2} />
        </span>
      </div>

      <div className="metric-tile-body">
        <span className="metric-tile-label">{label}</span>
        <span className="metric-tile-value">{value}</span>
      </div>

      {caption && (
        <div className="metric-tile-foot">
          <span className="metric-tile-caption">{caption}</span>
        </div>
      )}
    </article>
  );
}
