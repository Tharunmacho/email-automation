"use client";

export interface DonutSlice {
  label: string;
  value: number;
  /** A CSS colour. Ordered strongest-first by the caller. */
  color: string;
}

interface SplitDonutProps {
  slices: DonutSlice[];
  /** Drawn in the hole. Omitted leaves it empty, which is the quieter reading. */
  centre?: string;
  size?: number;
}

const THICKNESS = 0.28;

/**
 * A ring and the list that names it.
 *
 * The legend is not optional decoration: a ring with three arcs and no labels
 * is a picture of a proportion nobody can read back. Percentages are computed
 * here from the raw counts so the arc and the number in the legend can never
 * disagree — they are the same division.
 *
 * A total of zero draws the empty track rather than nothing at all, so the card
 * keeps its shape on a workspace where nothing has happened yet.
 */
export default function SplitDonut({ slices, centre, size = 132 }: SplitDonutProps) {
  const total = slices.reduce((sum, slice) => sum + slice.value, 0);
  const radius = size / 2;
  const stroke = size * THICKNESS;
  const ringRadius = radius - stroke / 2;
  const circumference = 2 * Math.PI * ringRadius;

  // Where each arc starts, as a running offset around the ring.
  let consumed = 0;

  return (
    <div className="ds-donut">
      <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} role="img"
        aria-label={slices.map((s) => `${s.label} ${s.value}`).join(", ")}>
        <circle
          cx={radius}
          cy={radius}
          r={ringRadius}
          fill="none"
          strokeWidth={stroke}
          className="ds-donut-track"
        />

        {total > 0 &&
          slices.map((slice) => {
            const share = slice.value / total;
            const length = share * circumference;
            const offset = consumed * circumference;
            consumed += share;
            if (slice.value === 0) return null;

            return (
              <circle
                key={slice.label}
                cx={radius}
                cy={radius}
                r={ringRadius}
                fill="none"
                stroke={slice.color}
                strokeWidth={stroke}
                strokeLinecap="butt"
                strokeDasharray={`${length} ${circumference - length}`}
                strokeDashoffset={-offset}
                // Zero starts at three o'clock in SVG; the quarter turn puts it
                // at twelve, where a ring is read from.
                transform={`rotate(-90 ${radius} ${radius})`}
              />
            );
          })}

        {centre && (
          <text x={radius} y={radius} textAnchor="middle" dominantBaseline="central" className="ds-donut-centre">
            {centre}
          </text>
        )}
      </svg>

      <ul className="ds-donut-legend">
        {slices.map((slice) => (
          <li key={slice.label}>
            <i style={{ background: slice.color }} aria-hidden="true" />
            <strong>{total > 0 ? Math.round((slice.value / total) * 100) : 0}%</strong>
            {slice.label}
          </li>
        ))}
      </ul>
    </div>
  );
}
