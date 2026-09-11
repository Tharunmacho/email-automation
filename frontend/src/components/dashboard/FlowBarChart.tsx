"use client";

import { useEffect, useRef, useState } from "react";
import { BarChart3 } from "lucide-react";
import styles from "./Chart.module.css";

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
  // Candidate counts are whole numbers: fractional steps would repeat labels
  // (0, 0, 1, 1, 1) when there are only one or two candidates in a period.
  const integerStep = Math.max(1, Math.ceil(step));
  return Array.from({ length: TICKS }, (_, i) => integerStep * i);
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
  // Start without an intrinsic SVG width. A desktop-sized default makes the
  // SVG itself widen its grid cell before ResizeObserver can measure it.
  const [width, setWidth] = useState(0);
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
  const labelEvery = slotW < 46 ? 2 : 1;

  return (
    <div className={`ds-flow ${styles.plot}`} ref={containerRef}>
      {peak === 0 ? (
        <div className={styles.empty}>
          <BarChart3 size={24} aria-hidden="true" />
          <strong>No candidates parsed in this period</strong>
          <p>Your intake trend will appear here as new resumes are parsed.</p>
        </div>
      ) : (
      <>
      <svg width={width} height={totalH} aria-hidden="true">
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
                className={`${styles.barMark} ${on ? styles.activeBar : ""}`}
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
              {((index % labelEvery === 0 && index < buckets.length - labelEvery) || index === buckets.length - 1) && (
              <text
                x={x + barW / 2}
                y={TOP_BAND + HEIGHT + 18}
                textAnchor="middle"
                className={`ds-flow-label ${on ? "is-on" : ""}`}
              >
                {bucket.label}
              </text>
              )}
            </g>
          );
        })}
      </svg>

      {/* Native buttons give pointer, touch, and keyboard users the same exact
          values. CSS sizes their grid immediately on a viewport change, so a
          pending ResizeObserver update cannot leave old hit targets offscreen. */}
      <div
        className={styles.barTargets}
        style={{ left: AXIS_W, top: TOP_BAND, height: HEIGHT, gridTemplateColumns: `repeat(${buckets.length}, minmax(0, 1fr))` }}
      >
      {buckets.map((bucket, index) => (
        <button
          key={`${bucket.full}-target`}
          type="button"
          className={styles.barTarget}
          aria-label={`${bucket.full}: ${bucket.value} candidates ${caption?.toLowerCase() ?? "parsed"}`}
          onMouseEnter={() => setHovered(index)}
          onMouseLeave={() => setHovered((current) => (current === index ? null : current))}
          onFocus={() => setHovered(index)}
          onBlur={() => setHovered(null)}
          onClick={() => setHovered(index)}
          onKeyDown={(event) => {
            if (event.key === "Escape") setHovered(null);
          }}
        />
      ))}
      </div>

      {active && (
        <div
          className="ds-flow-tip"
          aria-hidden="true"
          style={{
            left: `clamp(108px, ${xOf(hovered as number) + barW / 2}px, calc(100% - 108px))`,
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
      <div className={styles.legend}>
        <span><i className={styles.swatch} aria-hidden="true" />{caption ?? "Parsed"} candidates</span>
      </div>
      </>
      )}
      {buckets.length > 0 && (
        <details className={styles.details}>
          <summary>View chart data</summary>
          <table className={styles.dataTable}>
            <caption className={styles.screenReader}>Candidates parsed per period</caption>
            <thead><tr><th scope="col">Period</th><th scope="col">Candidates</th></tr></thead>
            <tbody>{buckets.map((bucket) => (
              <tr key={bucket.full}><th scope="row">{bucket.full}</th><td>{bucket.value}</td></tr>
            ))}</tbody>
          </table>
        </details>
      )}
    </div>
  );
}
