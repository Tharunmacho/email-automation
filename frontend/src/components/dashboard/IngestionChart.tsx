"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatDayLong, formatDayShort, formatInt, type DayBucket } from "@/lib/dashboardMetrics";

export const RANGE_OPTIONS = [7, 14, 30] as const;
export type RangeOption = (typeof RANGE_OPTIONS)[number];

interface IngestionChartProps {
  buckets: DayBucket[];
  /** The same window one period earlier — drawn as the dashed ghost line. */
  compare?: DayBucket[];
  range: RangeOption;
  /** Plot height in px. The card sets it; the chart never assumes one. */
  height?: number;
}

/* Every colour in the plot comes from a CSS class rather than a literal, so the
   chart follows the palette — including the dark theme — without this file
   knowing what the palette is. Presentation attributes cannot resolve a custom
   property, which is why these are classes rather than a `var(--primary)`
   written straight onto a `stroke` attribute. */

const PAD = { top: 16, right: 6, bottom: 26, left: 38 };

/** Rounds the axis top up to a clean 1/2/5 x 10^n so ticks land on round numbers. */
function niceCeiling(value: number, ticks: number): number {
  if (value <= 0) return ticks;
  const rough = value / ticks;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  const normalized = rough / magnitude;
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
  return step * magnitude * ticks;
}

/** Axis labels are compacted the way the reference chart's are: 5K, not 5,000. */
function axisLabel(value: number): string {
  if (value >= 1000) {
    const k = value / 1000;
    return `${Number.isInteger(k) ? k : k.toFixed(1)}K`;
  }
  return formatInt(value);
}

/**
 * A monotone cubic through the points.
 *
 * The straight polyline this used to draw made a quiet week look like a saw
 * blade — every one-résumé day was a spike with two hard corners. Fitting the
 * tangents to the neighbouring slopes (and flattening them wherever the series
 * turns) gives the curve the reference chart has without ever overshooting a
 * value the data does not contain, which a plain Catmull–Rom would.
 */
function smoothPath(points: { x: number; y: number }[]): string {
  if (points.length === 0) return "";
  if (points.length === 1) return `M${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;

  const n = points.length;
  const slopes: number[] = [];
  for (let i = 0; i < n - 1; i += 1) {
    const dx = points[i + 1].x - points[i].x;
    slopes.push(dx === 0 ? 0 : (points[i + 1].y - points[i].y) / dx);
  }

  // Fritsch-Carlson tangents: zero at every local extremum, so the curve
  // touches each sample and stays inside the band its neighbours define.
  const tangents: number[] = new Array(n);
  tangents[0] = slopes[0];
  tangents[n - 1] = slopes[n - 2];
  for (let i = 1; i < n - 1; i += 1) {
    tangents[i] = slopes[i - 1] * slopes[i] <= 0 ? 0 : (slopes[i - 1] + slopes[i]) / 2;
  }

  let d = `M${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
  for (let i = 0; i < n - 1; i += 1) {
    const dx = points[i + 1].x - points[i].x;
    const c1x = points[i].x + dx / 3;
    const c1y = points[i].y + (tangents[i] * dx) / 3;
    const c2x = points[i + 1].x - dx / 3;
    const c2y = points[i + 1].y - (tangents[i + 1] * dx) / 3;
    d +=
      ` C${c1x.toFixed(2)} ${c1y.toFixed(2)}, ${c2x.toFixed(2)} ${c2y.toFixed(2)},` +
      ` ${points[i + 1].x.toFixed(2)} ${points[i + 1].y.toFixed(2)}`;
  }
  return d;
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

export default function IngestionChart({
  buckets,
  compare,
  range,
  height = 236,
}: IngestionChartProps) {
  const { ref, width } = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);

  const TICKS = 3;
  // The ghost line shares the axis — two series drawn to two different scales
  // is a comparison that flatters whichever one is smaller.
  const peak = Math.max(...buckets.map((b) => b.added), ...(compare ?? []).map((b) => b.added), 0);
  const axisMax = niceCeiling(peak, TICKS);

  const plotWidth = Math.max(0, width - PAD.left - PAD.right);
  const plotHeight = height - PAD.top - PAD.bottom;

  const project = useCallback(
    (series: DayBucket[]) => {
      if (series.length === 0 || plotWidth <= 0) return [] as { x: number; y: number }[];
      const stepX = series.length > 1 ? plotWidth / (series.length - 1) : 0;
      return series.map((bucket, index) => ({
        x: PAD.left + index * stepX,
        y: PAD.top + plotHeight - (bucket.added / axisMax) * plotHeight,
      }));
    },
    [plotWidth, plotHeight, axisMax],
  );

  const geometry = useMemo(() => {
    const points = project(buckets);
    if (points.length === 0) return { points, line: "", area: "" };

    const line = smoothPath(points);
    const baseline = PAD.top + plotHeight;
    const area = `${line} L${points[points.length - 1].x.toFixed(2)} ${baseline} L${points[0].x.toFixed(2)} ${baseline} Z`;

    return { points, line, area };
  }, [buckets, project, plotHeight]);

  const ghost = useMemo(() => {
    if (!compare || compare.length === 0) return "";
    return smoothPath(project(compare));
  }, [compare, project]);

  const labelEvery = range <= 7 ? 1 : range <= 14 ? 3 : 7;

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
  const activeCompare = hover !== null ? compare?.[hover] : undefined;

  return (
    <div className="dash-plot" ref={ref}>
      <svg
        width={width}
        height={height}
        role="img"
        aria-label={`Resumes parsed per day over the last ${range} days`}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id="dash-area-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" className="plot-fill-top" />
            <stop offset="62%" className="plot-fill-mid" />
            <stop offset="100%" className="plot-fill-end" />
          </linearGradient>
        </defs>

        {/* Dashed hairline grid + value ticks. Dashed rather than solid: the
            rules are scaffolding for reading a height off the plot, and a solid
            line at this weight competes with the series drawn over it. */}
        {Array.from({ length: TICKS + 1 }, (_, index) => {
          const value = (axisMax / TICKS) * index;
          const y = PAD.top + plotHeight - (index / TICKS) * plotHeight;
          return (
            <g key={value}>
              <line
                x1={PAD.left}
                y1={y}
                x2={width - PAD.right}
                y2={y}
                className="plot-grid"
                strokeWidth={1}
                strokeDasharray="4 5"
              />
              <text x={PAD.left - 10} y={y + 4} textAnchor="end" className="dash-axis-text">
                {axisLabel(value)}
              </text>
            </g>
          );
        })}

        {/* The previous window, behind everything — it is context for the line
            in front of it, never a second reading of equal weight. */}
        {ghost && (
          <path
            d={ghost}
            fill="none"
            className="plot-line-ghost"
            strokeWidth={1.75}
            strokeDasharray="5 5"
            strokeLinecap="round"
          />
        )}

        {geometry.line && (
          <>
            <path d={geometry.area} fill="url(#dash-area-fill)" />
            <path
              d={geometry.line}
              fill="none"
              className="plot-line"
              strokeWidth={2.25}
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
              y={height - 8}
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
              className="plot-crosshair"
              strokeWidth={1}
              strokeDasharray="3 3"
            />
            <circle cx={activePoint.x} cy={activePoint.y} r={7} className="plot-dot-halo" />
            <circle cx={activePoint.x} cy={activePoint.y} r={4} className="plot-dot" />
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
            <i className="dash-dot plot-dot-swatch" />
            {active.added} parsed
          </span>
          {activeCompare && (
            <span className="dash-tooltip-muted">
              {activeCompare.added} in the previous period
            </span>
          )}
          {active.review > 0 && (
            <span className="dash-tooltip-muted">{active.review} flagged for review</span>
          )}
        </div>
      )}
    </div>
  );
}
