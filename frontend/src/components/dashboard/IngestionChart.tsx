"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity } from "lucide-react";
import { formatDayLong, formatDayShort, formatInt, type DayBucket } from "@/lib/dashboardMetrics";

export const RANGE_OPTIONS = [7, 14, 30] as const;
export type RangeOption = (typeof RANGE_OPTIONS)[number];

interface IngestionChartProps {
  buckets: DayBucket[];
  range: RangeOption;
  onRangeChange: (range: RangeOption) => void;
}

/** Dark blue — the line is the loudest thing on an otherwise white surface. */
const ACCENT = "#1e40af";
/* Gridlines stay recessive, but tinted from the same navy as every border. */
const GRID = "#e3eaf7";

const PAD = { top: 18, right: 18, bottom: 30, left: 40 };
const HEIGHT = 268;

/** Rounds the axis top up to a clean 1/2/5 × 10ⁿ so ticks land on round numbers. */
function niceCeiling(value: number, ticks: number): number {
  if (value <= 0) return ticks;
  const rough = value / ticks;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude * ticks;
}

/** Tracks the rendered width so the SVG draws at 1:1 instead of being scaled. */
function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(720);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;

    const observer = new ResizeObserver((entries) => {
      const next = entries[0]?.contentRect.width;
      if (next && next > 0) setWidth(next);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return { ref, width };
}

export default function IngestionChart({ buckets, range, onRangeChange }: IngestionChartProps) {
  const { ref, width } = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const TICKS = 4;
  const values = buckets.map((bucket) => bucket.added);
  const peak = Math.max(...values, 0);
  const axisMax = niceCeiling(peak, TICKS);

  const plotWidth = Math.max(0, width - PAD.left - PAD.right);
  const plotHeight = HEIGHT - PAD.top - PAD.bottom;

  const geometry = useMemo(() => {
    if (buckets.length === 0 || plotWidth <= 0) {
      return { points: [] as { x: number; y: number }[], line: "", area: "" };
    }

    const stepX = buckets.length > 1 ? plotWidth / (buckets.length - 1) : 0;
    const points = buckets.map((bucket, index) => ({
      x: PAD.left + index * stepX,
      y: PAD.top + plotHeight - (bucket.added / axisMax) * plotHeight,
    }));

    const line = points
      .map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
      .join(" ");

    const baseline = PAD.top + plotHeight;
    const area = `${line} L${points[points.length - 1].x.toFixed(2)} ${baseline} L${points[0].x.toFixed(2)} ${baseline} Z`;

    return { points, line, area };
  }, [buckets, plotWidth, plotHeight, axisMax]);

  const labelEvery = range <= 7 ? 1 : range <= 14 ? 2 : 5;

  const handleMove = useCallback(
    (event: React.MouseEvent<SVGRectElement>) => {
      if (buckets.length < 2 || plotWidth <= 0) return;
      const bounds = event.currentTarget.getBoundingClientRect();
      const offsetX = event.clientX - bounds.left;
      const ratio = Math.min(1, Math.max(0, offsetX / plotWidth));
      setHover(Math.round(ratio * (buckets.length - 1)));
    },
    [buckets.length, plotWidth],
  );

  const active = hover !== null ? buckets[hover] : null;
  const activePoint = hover !== null ? geometry.points[hover] : null;
  const total = values.reduce((sum, value) => sum + value, 0);

  return (
    <section className="dash-card dash-chart-card">
      <header className="dash-card-head">
        <div>
          <h3 className="dash-card-title">
            <Activity size={17} strokeWidth={2.2} /> Ingestion activity
          </h3>
          <p className="dash-card-sub">
            {formatInt(total)} resume{total === 1 ? "" : "s"} parsed in the last {range} days
          </p>
        </div>

        <div className="dash-segmented" role="group" aria-label="Time range">
          {RANGE_OPTIONS.map((option) => (
            <button
              key={option}
              type="button"
              className={`dash-segmented-btn ${option === range ? "active" : ""}`}
              onClick={() => onRangeChange(option)}
              aria-pressed={option === range}
            >
              {option}D
            </button>
          ))}
        </div>
      </header>

      <div className="dash-plot" ref={ref}>
        <svg
          width={width}
          height={HEIGHT}
          role="img"
          aria-label={`Resumes parsed per day over the last ${range} days`}
          onMouseLeave={() => setHover(null)}
        >
          <defs>
            <linearGradient id="dash-area-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={ACCENT} stopOpacity="0.16" />
              <stop offset="100%" stopColor={ACCENT} stopOpacity="0" />
            </linearGradient>
          </defs>

          {/* Recessive hairline grid + value ticks */}
          {Array.from({ length: TICKS + 1 }, (_, index) => {
            const value = (axisMax / TICKS) * index;
            const y = PAD.top + plotHeight - (index / TICKS) * plotHeight;
            return (
              <g key={value}>
                <line x1={PAD.left} y1={y} x2={width - PAD.right} y2={y} stroke={GRID} strokeWidth={1} />
                <text x={PAD.left - 10} y={y + 4} textAnchor="end" className="dash-axis-text">
                  {formatInt(value)}
                </text>
              </g>
            );
          })}

          {geometry.line && (
            <>
              <path d={geometry.area} fill="url(#dash-area-fill)" />
              <path
                d={geometry.line}
                fill="none"
                stroke={ACCENT}
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </>
          )}

          {/* Day labels */}
          {buckets.map((bucket, index) => {
            if (index % labelEvery !== 0 && index !== buckets.length - 1) return null;
            const point = geometry.points[index];
            if (!point) return null;
            return (
              <text
                key={bucket.key}
                x={point.x}
                y={HEIGHT - 10}
                textAnchor={index === 0 ? "start" : index === buckets.length - 1 ? "end" : "middle"}
                className="dash-axis-text"
              >
                {formatDayShort(bucket.date)}
              </text>
            );
          })}

          {/* Crosshair */}
          {activePoint && (
            <g pointerEvents="none">
              <line
                x1={activePoint.x}
                y1={PAD.top}
                x2={activePoint.x}
                y2={PAD.top + plotHeight}
                stroke={ACCENT}
                strokeOpacity="0.35"
                strokeWidth={1}
              />
              <circle cx={activePoint.x} cy={activePoint.y} r={6} fill="#ffffff" />
              <circle cx={activePoint.x} cy={activePoint.y} r={4} fill={ACCENT} />
            </g>
          )}

          {/* Hit area — sits above the marks so the whole plot is hoverable. */}
          <rect
            x={PAD.left}
            y={PAD.top}
            width={Math.max(0, plotWidth)}
            height={plotHeight}
            fill="transparent"
            onMouseMove={handleMove}
          />
        </svg>

        {active && activePoint && (
          <div
            className="dash-tooltip"
            style={{
              left: `${Math.min(Math.max(activePoint.x, 70), Math.max(width - 70, 70))}px`,
              top: `${Math.max(activePoint.y - 16, 8)}px`,
            }}
          >
            <span className="dash-tooltip-date">{formatDayLong(active.date)}</span>
            <span className="dash-tooltip-row">
              <i className="dash-dot" style={{ background: ACCENT }} />
              {active.added} parsed
            </span>
            {active.review > 0 && (
              <span className="dash-tooltip-muted">{active.review} flagged for review</span>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
