"use client";

interface GaugeProps {
  /** 0–100 */
  percent: number;
  label: string;
}

const R = 70;
const CX = 90;
const CY = 85;
const STROKE = 12;
const CIRCUMFERENCE = Math.PI * R; // half-circle

function arc(pct: number) {
  const clamped = Math.max(0, Math.min(100, pct));
  return (clamped / 100) * CIRCUMFERENCE;
}

export default function SlaGauge({ percent, label }: GaugeProps) {
  const filled = arc(percent);

  return (
    <div className="gauge-wrap">
      <svg
        width={180}
        height={110}
        viewBox="0 0 180 110"
        aria-label={`${label}: ${percent}%`}
      >
        <defs>
          <linearGradient id="gauge-grad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="var(--accent)" />
            <stop offset="100%" stopColor="var(--primary)" />
          </linearGradient>
        </defs>

        {/* Track (grey half arc) */}
        <path
          d={`M ${CX - R} ${CY} A ${R} ${R} 0 0 1 ${CX + R} ${CY}`}
          fill="none"
          stroke="var(--tint-3)"
          strokeWidth={STROKE}
          strokeLinecap="round"
        />

        {/* Filled arc */}
        <path
          d={`M ${CX - R} ${CY} A ${R} ${R} 0 0 1 ${CX + R} ${CY}`}
          fill="none"
          stroke="url(#gauge-grad)"
          strokeWidth={STROKE}
          strokeLinecap="round"
          strokeDasharray={`${filled} ${CIRCUMFERENCE}`}
          style={{ transition: "stroke-dasharray 0.8s cubic-bezier(.4,0,.2,1)" }}
        />

        {/* Center text */}
        <text
          x={CX}
          y={CY - 10}
          textAnchor="middle"
          className="gauge-pct"
        >
          {Math.round(percent)}%
        </text>
      </svg>
    </div>
  );
}
