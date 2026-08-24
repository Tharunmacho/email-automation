"use client";

import { useEffect, useRef, useState } from "react";

interface DonutProps {
  /** Slices: { label, value, color } */
  slices: { label: string; value: number; color: string }[];
  total: number;
  centerLabel: string;
  centerValue: string | number;
}

const R = 52;
const CX = 70;
const CY = 70;
const STROKE = 18;
const CIRCUMFERENCE = 2 * Math.PI * R;

export default function PipelineDonut({ slices, total, centerLabel, centerValue }: DonutProps) {
  const [mounted, setMounted] = useState(false);
  useEffect(() => { setMounted(true); }, []);

  if (total === 0) {
    return (
      <div className="donut-empty">No data yet</div>
    );
  }

  let offset = 0;
  const drawn = slices.map((slice) => {
    const frac = slice.value / total;
    const dash = frac * CIRCUMFERENCE;
    const gap = CIRCUMFERENCE - dash;
    const startOffset = CIRCUMFERENCE - offset;
    offset += dash;
    return { ...slice, dash, gap, startOffset };
  });

  return (
    <div className="donut-wrap">
      <svg width={140} height={140} viewBox="0 0 140 140" aria-label="Pipeline breakdown">
        {/* Track */}
        <circle
          cx={CX} cy={CY} r={R}
          fill="none"
          stroke="var(--tint-2)"
          strokeWidth={STROKE}
        />
        {/* Slices */}
        {drawn.map((slice) => (
          <circle
            key={slice.label}
            cx={CX} cy={CY} r={R}
            fill="none"
            stroke={slice.color}
            strokeWidth={STROKE}
            strokeDasharray={`${mounted ? slice.dash : 0} ${CIRCUMFERENCE}`}
            strokeDashoffset={slice.startOffset}
            strokeLinecap="butt"
            style={{ transition: "stroke-dasharray 0.9s cubic-bezier(.4,0,.2,1)", transform: "rotate(-90deg)", transformOrigin: `${CX}px ${CY}px` }}
          />
        ))}
        {/* Center */}
        <text x={CX} y={CY - 6} textAnchor="middle" className="donut-center-value">
          {centerValue}
        </text>
        <text x={CX} y={CY + 12} textAnchor="middle" className="donut-center-label">
          {centerLabel}
        </text>
      </svg>

      {/* Legend */}
      <div className="donut-legend">
        {slices.map((slice) => (
          <div key={slice.label} className="donut-legend-row">
            <span className="donut-legend-dot" style={{ background: slice.color }} />
            <span className="donut-legend-label">{slice.label}</span>
            <span className="donut-legend-value">{slice.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
