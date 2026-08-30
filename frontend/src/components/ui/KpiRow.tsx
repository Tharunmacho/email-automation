"use client";

import { TrendingDown, TrendingUp, type LucideIcon } from "lucide-react";

export interface KpiCardData {
  label: string;
  value: string | number;
  caption: string;
  icon: LucideIcon;
  tone?: "default" | "success" | "warning" | "rose";
  delta?: number | null; // percent change vs previous period
}

function DeltaBadge({ pct }: { pct: number }) {
  const up = pct >= 0;
  return (
    <span className={`ov-delta ${up ? "is-up" : "is-down"}`}>
      {up ? <TrendingUp size={11} /> : <TrendingDown size={11} />}
      {Math.abs(pct).toFixed(1)}%
    </span>
  );
}

interface Props {
  cards: KpiCardData[];
}

export default function KpiRow({ cards }: Props) {
  return (
    <div className="ov-kpi-row">
      {cards.map((kpi) => {
        const Icon = kpi.icon;
        const tone = kpi.tone ?? "default";
        return (
          <article key={kpi.label} className="ov-kpi-card">
            <div className="ov-kpi-card-top">
              <span className="ov-kpi-card-label">{kpi.label}</span>
              <span className={`ov-kpi-card-icon${tone !== "default" ? ` is-${tone}` : ""}`}>
                <Icon size={17} />
              </span>
            </div>
            <p className={`ov-kpi-card-value${tone === "rose" ? " is-rose" : ""}`}>
              {kpi.value}
            </p>
            <div className="ov-kpi-card-foot">
              {kpi.delta != null && <DeltaBadge pct={kpi.delta} />}
              <span className="ov-kpi-card-caption">{kpi.caption}</span>
            </div>
          </article>
        );
      })}
    </div>
  );
}
