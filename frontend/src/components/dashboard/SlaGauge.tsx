"use client";

import { useEffect, useState } from "react";

interface GaugeProps {
  /** 0–100 */
  percent: number;
  label: string;
  /** Caption under the figure — what the number is measured against. */
  caption?: string;
}

/* Geometry. The arc is a 250° sweep centred on the top of the dial, which is
   wide enough that the two ends read as the ends of a scale rather than as a
   half-circle that happens to be open at the bottom. */
const SIZE = 208;
const CX = SIZE / 2;
const CY = SIZE / 2 + 6;
const OUTER = 88;
const INNER = 66;
const SWEEP = 250;
const START = 90 + (360 - SWEEP) / 2; // degrees, clockwise from 3 o'clock
const TICKS = 40;

/**
 * A dial drawn as a comb of radial ticks rather than as one thick stroke.
 *
 * The ticks are what make the reading quantised: a solid arc invites the eye to
 * measure an angle, which nobody can do to better than about ten points, while
 * a comb can simply be counted. The filled teeth run a teal-to-green ramp so
 * the far end of the scale is visibly a different place from the near end —
 * with one flat colour, 40% and 90% are the same picture at different lengths.
 */
export default function SlaGauge({ percent, label, caption }: GaugeProps) {
  const clamped = Math.max(0, Math.min(100, percent));

  // Animate up from empty on mount, so the dial states a result rather than
  // arriving already having stated it.
  const [shown, setShown] = useState(0);
  useEffect(() => {
    const id = requestAnimationFrame(() => setShown(clamped));
    return () => cancelAnimationFrame(id);
  }, [clamped]);

  const lit = Math.round((shown / 100) * TICKS);

  return (
    <div className="gauge-wrap">
      <svg
        width={SIZE}
        height={CY + 26}
        viewBox={`0 0 ${SIZE} ${CY + 26}`}
        role="img"
        aria-label={`${label}: ${Math.round(clamped)}%`}
      >
        <defs>
          <linearGradient id="gauge-comb" x1="0%" y1="100%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="var(--gauge-from)" />
            <stop offset="55%" stopColor="var(--gauge-mid)" />
            <stop offset="100%" stopColor="var(--gauge-to)" />
          </linearGradient>
        </defs>

        {Array.from({ length: TICKS }, (_, i) => {
          const angle = ((START + (SWEEP * i) / (TICKS - 1)) * Math.PI) / 180;
          const cos = Math.cos(angle);
          const sin = Math.sin(angle);
          const on = i < lit;

          return (
            <line
              key={i}
              x1={CX + cos * INNER}
              y1={CY + sin * INNER}
              x2={CX + cos * OUTER}
              y2={CY + sin * OUTER}
              className={`gauge-tick ${on ? "is-on" : ""}`}
              strokeWidth={4.5}
              strokeLinecap="round"
              style={{ transitionDelay: `${i * 14}ms` }}
            />
          );
        })}

        <text x={CX} y={CY - 6} textAnchor="middle" className="gauge-pct">
          {Math.round(clamped)}%
        </text>
        {caption && (
          <text x={CX} y={CY + 18} textAnchor="middle" className="gauge-caption">
            {caption}
          </text>
        )}
      </svg>
    </div>
  );
}
