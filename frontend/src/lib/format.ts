/**
 * Display formatters shared by every screen.
 *
 * Dates and numbers are formatted by hand rather than through `toLocaleString`.
 * The server and the browser can resolve different locales, and any mismatch in
 * the rendered text triggers a React hydration error.
 */

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/** "Jul 19" */
export function formatDayShort(date: Date): string {
  return `${MONTHS[date.getMonth()]} ${date.getDate()}`;
}

/** "Sat, Jul 19" */
export function formatDayLong(date: Date): string {
  return `${WEEKDAYS[date.getDay()]}, ${MONTHS[date.getMonth()]} ${date.getDate()}`;
}

/** "Jul 19, 2026" */
export function formatDateFull(date: Date): string {
  return `${MONTHS[date.getMonth()]} ${date.getDate()}, ${date.getFullYear()}`;
}

/** Thousands-separated integer, locale-independent. */
export function formatInt(value: number): string {
  if (!Number.isFinite(value)) return "—";
  return Math.round(value).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** 1,284 → "1,284"; 12,900 → "12.9K" — the stat-tile compaction rule. */
export function compactNumber(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 10_000) return `${(value / 1_000).toFixed(1)}K`;
  return formatInt(value);
}

export function timeAgo(value: string | undefined, now: Date = new Date()): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "—";

  const seconds = Math.max(0, Math.floor((now.getTime() - parsed.getTime()) / 1000));
  if (seconds < 60) return "just now";

  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;

  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks}w ago`;

  return formatDayShort(parsed);
}

export function initialsOf(name: string | null | undefined, fallback = "?"): string {
  const parts = String(name ?? "").trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return fallback;
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[parts.length - 1][0]}`.toUpperCase();
}
