"use client";

import React from "react";
import type { LucideIcon } from "lucide-react";

/** Colour is meaning, never decoration — pick the tone for what the figure is. */
export type StatTone = "blue" | "green" | "cyan" | "navy" | "yellow" | "red" | "slate";

interface StatTileProps {
  label: string;
  value: string;
  /** The context the number needs — one short line. */
  note?: React.ReactNode;
  icon: LucideIcon;
  tone?: StatTone;
}

/**
 * White tile with one coloured border edge. Shared by every screen that
 * reports figures, so a number reads the same way across the product.
 * Styling lives in globals.css under STAT TILES.
 */
export default function StatTile({ label, value, note, icon: Icon, tone = "blue" }: StatTileProps) {
  return (
    <article className={`stat-tile tone-${tone}`}>
      <span className="stat-tile-icon">
        <Icon size={17} strokeWidth={2} />
      </span>
      <div className="stat-tile-text">
        <span className="stat-tile-label">{label}</span>
        <span className="stat-tile-value">{value}</span>
        {note && <span className="stat-tile-note">{note}</span>}
      </div>
    </article>
  );
}
