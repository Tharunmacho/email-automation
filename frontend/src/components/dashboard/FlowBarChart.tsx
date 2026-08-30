"use client";

import { useEffect, useRef, useState } from "react";

export interface FlowBucket {
  /** The axis label — "Mar", "W-32". */
  label: string;
  /** What the tooltip calls this bucket in full — "March 2026". */
  full: string;
  value: number;
}

interface FlowBarChartProps {
  buckets: FlowBucket[];
  /** Named in the tooltip's second line, under the count. */
  caption?: string;
}

const HEIGHT = 190;
const LABEL_BAND = 26;
const TOP_BAND = 10;
const AXIS_W = 34;
const MAX_BAR_W = 44;
const TICKS = 5;

/** 0, 5, 10… — round steps that reach just past the tallest bar. */
function axisTicks(peak: number): number[] {
  if (peak <= 0) return [0, 1, 2, 3, 4];
  const rough = peak / (TICKS - 1);
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= rough) ?? magnitude * 10;
  return Array.from({ length: TICKS }, (_, i) => Math.round(step * i));
}

/**
 * One column per period, and the one under the pointer called out.
 *
 * The columns are washed rather than solid — a page with four saturated blocks
 * of accent on it has no accent left — and the hovered column is the only one
 * that fills, so the thing being read is the only thing that is loud. The
 * tooltip is positioned from the bar's own geometry rather than the mouse, so
 * it sits over the column it describes instead of trailing the cursor.
 */
export default function FlowBarChart({ buckets, caption }: FlowBarChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(560);
  const [hovered, setHovered] = useState<number | null>(null);

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const observer = new ResizeObserver((entries) => {
      const measured = entries[0]?.contentRect.width;
      if (measured && measured > 0) setWidth(measured);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  const peak = Math.max(...buckets.map((b) => b.value), 0);
  const ticks = axisTicks(peak);
  // Scaled to the top tick, not to the peak, so the tallest bar stops at a
  // gridline instead of running off the top of its own axis.
  const ceiling = ticks[ticks.length - 1] || 1;

  const plotW = Math.max(0, width - AXIS_W);
  const slotW = buckets.length > 0 ? plotW / buckets.length : plotW;
  const barW = Math.min(MAX_BAR_W, Math.max(10, slotW - 18));
  const radius = Math.min(8, barW / 2);
  const totalH = TOP_BAND + HEIGHT + LABEL_BAND;

  const xOf = (index: number) => AXIS_W + index * slotW + (slotW - barW) / 2;
  const heightOf = (value: number) =>
    value > 0 ? Math.max(radius, Math.round((value / ceiling) * HEIGHT)) : 2;

  const active = hovered !== null ? buckets[hovered] : null;

  return (
    <div className="ds-flow" ref={containerRef}>
      <svg width={width} height={totalH} role="img" aria-label="Candidates parsed per period">
        <defs>
          <linearGradient id="ds-bar" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(var(--primary-rgb))" stopOpacity="0.32" />
            <stop offset="100%" stopColor="rgb(var(--primary-rgb))" stopOpacity="0.04" />
          </linearGradient>
          <linearGradient id="ds-bar-on" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="rgb(var(--primary-rgb))" stopOpacity="1" />
            <stop offset="100%" stopColor="rgb(var(--primary-rgb))" stopOpacity="0.72" />
          </linearGradient>
        </defs>

        {/* Gridlines, drawn behind everything and labelled down the left. */}
        {ticks.map((tick, index) => {
          const y = TOP_BAND + HEIGHT - (tick / ceiling) * HEIGHT;
          return (
            <g key={`tick-${index}-${tick}`}>
              <line x1={AXIS_W} y1={y} x2={width} y2={y} className="ds-flow-grid" />
              <text x={AXIS_W - 8} y={y + 4} textAnchor="end" className="ds-flow-tick">
                {tick}
              </text>
            </g>
          );
        })}

        {buckets.map((bucket, index) => {
          const barH = heightOf(bucket.value);
          const x = xOf(index);
          const y = TOP_BAND + HEIGHT - barH;
          const on = hovered === index;

          return (
            <g key={bucket.label + index}>
              <rect
                x={x}
                y={y}
                width={barW}
                height={barH}
                rx={radius}
                fill={on ? "url(#ds-bar-on)" : "url(#ds-bar)"}
              />
              {on && (
                <>
                  <line
                    x1={x + barW / 2}
                    y1={TOP_BAND}
                    x2={x + barW / 2}
                    y2={TOP_BAND + HEIGHT}
                    className="ds-flow-cursor"
                  />
                  <circle cx={x + barW / 2} cy={y} r={5} className="ds-flow-knob" />
                </>
              )}
              {/* One hit area per column, the full height of the plot, so a
                  short bar is as easy to read as a tall one. */}
              <rect
                x={AXIS_W + index * slotW}
                y={TOP_BAND}
                width={slotW}
                height={HEIGHT}
                fill="transparent"
                onMouseEnter={() => setHovered(index)}
                onMouseLeave={() => setHovered((current) => (current === index ? null : current))}
              />
              <text
                x={x + barW / 2}
                y={TOP_BAND + HEIGHT + 18}
                textAnchor="middle"
                className={`ds-flow-label ${on ? "is-on" : ""}`}
              >
                {bucket.label}
              </text>
            </g>
          );
        })}
      </svg>

      {active && (
        <div
          className="ds-flow-tip"
          style={{
            left: `${Math.min(
              Math.max(xOf(hovered as number) + barW / 2, 82),
              Math.max(width - 82, 82),
            )}px`,
            top: `${Math.max(TOP_BAND, TOP_BAND + HEIGHT - heightOf(active.value) - 78)}px`,
          }}
        >
          <span className="ds-flow-tip-when">{active.full}</span>
          <span className="ds-flow-tip-row">
            <span>{caption ?? "Parsed"}</span>
            <strong>{active.value}</strong>
          </span>
        </div>
      )}
    </div>
  );
}
