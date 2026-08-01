"use client";

import React from "react";

interface SparklineProps {
  values: number[];
  /** Colour for the trailing (current) stretch of the line and the end dot. */
  accent: string;
  /** Recessive colour the earlier stretch is drawn in. */
  muted: string;
  width?: number;
  height?: number;
}

/**
 * A 12-ish point trend line for a stat tile. No axes, no labels — the tile's
 * value and delta carry the numbers; this only carries the shape.
 */
export default function Sparkline({
  values,
  accent,
  muted,
  width = 104,
  height = 34,
}: SparklineProps) {
  if (values.length < 2) {
    return <svg className="dash-sparkline" viewBox={`0 0 ${width} ${height}`} aria-hidden="true" />;
  }

  const padding = 4;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;

  const points = values.map((value, index) => {
    const x = padding + (index / (values.length - 1)) * (width - padding * 2);
    const y = height - padding - ((value - min) / span) * (height - padding * 2);
    return [x, y] as const;
  });

  const toPath = (slice: readonly (readonly [number, number])[]) =>
    slice.map(([x, y], index) => `${index === 0 ? "M" : "L"}${x.toFixed(2)} ${y.toFixed(2)}`).join(" ");

  // The last quarter of the window reads as "current" and takes the accent.
  const splitIndex = Math.max(0, points.length - 1 - Math.ceil((points.length - 1) / 4));
  const [endX, endY] = points[points.length - 1];

  return (
    <svg
      className="dash-sparkline"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-hidden="true"
      focusable="false"
    >
      <path
        d={toPath(points.slice(0, splitIndex + 1))}
        fill="none"
        stroke={muted}
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d={toPath(points.slice(splitIndex))}
        fill="none"
        stroke={accent}
        strokeWidth={2}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* 2px surface ring keeps the dot legible where it sits on the line. */}
      <circle cx={endX} cy={endY} r={4.5} fill="#ffffff" />
      <circle cx={endX} cy={endY} r={3} fill={accent} />
    </svg>
  );
}
