"use client";

import { useEffect, useRef, useState } from "react";

interface BarChartProps {
  /** Seven values, index 0 = Sunday */
  data: number[];
  /** Which column is called out in the accent. Defaults to the busiest. */
  highlightIndex?: number;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const HEIGHT = 150;
const LABEL_BAND = 26;
/** Headroom for the value label that floats over the highlighted column. */
const TOP_BAND = 22;
const MAX_BAR_WIDTH = 26;

/**
 * Seven columns, one week, one of them called out.
 *
 * There is no grey track behind the bars any more. A full-height track turns
 * every column into a pair of readings — the bar and the socket it sits in —
 * and at this size the socket is the louder of the two. What is left is the
 * data: light columns for the ordinary days, the accent for the busiest, and
 * its count set directly above it so the peak can be read without a hover.
 */
export default function WeeklyBarChart({ data, highlightIndex }: BarChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [containerWidth, setContainerWidth] = useState(280);

  useEffect(() => {
    const node = containerRef.current;
    if (!node) return;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w && w > 0) setContainerWidth(w);
    });
    ro.observe(node);
    return () => ro.disconnect();
  }, []);

  const peak = Math.max(...data, 0);
  const scale = peak > 0 ? peak : 1;
  const highlight = highlightIndex ?? (peak > 0 ? data.indexOf(peak) : -1);
  const slotW = containerWidth / 7;
  const barW = Math.min(MAX_BAR_WIDTH, Math.max(12, slotW - 14));
  const radius = barW / 2;

  return (
    <div ref={containerRef} className="wk-chart">
      <svg
        width={containerWidth}
        height={HEIGHT + LABEL_BAND + TOP_BAND}
        aria-label="Candidates per day of week"
      >
        {data.map((val, i) => {
          // A zero day still gets a stub, so the axis reads as seven days with
          // nothing on one of them rather than as a week with a day missing.
          const barH = val > 0 ? Math.max(radius * 2, Math.round((val / scale) * HEIGHT)) : 6;
          const x = i * slotW + (slotW - barW) / 2;
          const y = TOP_BAND + HEIGHT - barH;
          const isPeak = i === highlight && val > 0;

          return (
            <g key={i}>
              <rect
                x={x}
                y={y}
                width={barW}
                height={barH}
                rx={radius}
                className={`bar-fill ${isPeak ? "is-peak" : ""}`}
              />
              {isPeak && (
                <text
                  x={x + barW / 2}
                  y={y - 9}
                  textAnchor="middle"
                  className="bar-label-peak"
                >
                  {val}
                </text>
              )}
              <text
                x={x + barW / 2}
                y={TOP_BAND + HEIGHT + 18}
                textAnchor="middle"
                className={`bar-day-label ${isPeak ? "is-peak" : ""}`}
              >
                {DAYS[i]}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
