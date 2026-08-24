"use client";

import { useEffect, useRef, useState } from "react";

interface BarChartProps {
  /** Seven values, index 0 = Sunday */
  data: number[];
  todayIndex?: number;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const HEIGHT = 120;
const BAR_WIDTH = 24;

export default function WeeklyBarChart({ data, todayIndex }: BarChartProps) {
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

  const peak = Math.max(...data, 1);
  const today = todayIndex ?? new Date().getDay();
  const slotW = containerWidth / 7;

  return (
    <div ref={containerRef} style={{ width: "100%", position: "relative" }}>
      <svg width={containerWidth} height={HEIGHT + 28} aria-label="Candidates per day of week">
        {data.map((val, i) => {
          const barH = Math.round((val / peak) * HEIGHT);
          const x = i * slotW + (slotW - BAR_WIDTH) / 2;
          const y = HEIGHT - barH;
          const isToday = i === today;

          return (
            <g key={i}>
              {/* Background bar track */}
              <rect
                x={x}
                y={0}
                width={BAR_WIDTH}
                height={HEIGHT}
                rx={8}
                className="bar-track"
              />
              {/* Value bar */}
              <rect
                x={x}
                y={y}
                width={BAR_WIDTH}
                height={barH}
                rx={8}
                className={`bar-fill ${isToday ? "is-today" : ""}`}
              />
              {/* Value label on today */}
              {isToday && val > 0 && (
                <text
                  x={x + BAR_WIDTH / 2}
                  y={y - 6}
                  textAnchor="middle"
                  className="bar-label-today"
                >
                  {val}
                </text>
              )}
              {/* Day label */}
              <text
                x={x + BAR_WIDTH / 2}
                y={HEIGHT + 20}
                textAnchor="middle"
                className={`bar-day-label ${isToday ? "is-today" : ""}`}
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
