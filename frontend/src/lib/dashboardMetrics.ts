/**
 * Pure derivations that turn the raw candidate collection into the series the
 * dashboard renders. Kept out of the components so the numbers can be reasoned
 * about (and unit-tested) independently of the SVG that draws them.
 */

import { REVIEW_CONFIDENCE_THRESHOLD, type CandidateRecord } from "@/lib/api";

export interface DayBucket {
  /** Local yyyy-mm-dd — the grouping key. */
  key: string;
  date: Date;
  /** Candidates created on this day. */
  added: number;
  /** Of those, how many are verified / flagged for review. */
  verified: number;
  review: number;
  confidenceSum: number;
  confidenceCount: number;
}

export interface TrendPoint {
  key: string;
  date: Date;
  value: number;
}

export interface Delta {
  /** Signed change of the metric across the trailing window. */
  change: number;
  /** Percentage change, or null when the previous window was empty. */
  percent: number | null;
}

export const isVerified = (c: CandidateRecord) => c.status === "verified";

export const needsReview = (c: CandidateRecord) =>
  (c.profile?.confidence ?? 1) < REVIEW_CONFIDENCE_THRESHOLD;

function dayKey(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function parseCreated(candidate: CandidateRecord): Date | null {
  if (!candidate.created_at) return null;
  const parsed = new Date(candidate.created_at);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/**
 * One bucket per day for the trailing `days` window, oldest first. Days with no
 * ingestion are present with zero counts so the x-axis stays evenly spaced.
 */
export function buildDailyBuckets(
  candidates: CandidateRecord[],
  days: number,
  now: Date = new Date(),
): DayBucket[] {
  const today = startOfDay(now);
  const buckets: DayBucket[] = [];
  const index = new Map<string, DayBucket>();

  for (let offset = days - 1; offset >= 0; offset -= 1) {
    const date = new Date(today.getFullYear(), today.getMonth(), today.getDate() - offset);
    const bucket: DayBucket = {
      key: dayKey(date),
      date,
      added: 0,
      verified: 0,
      review: 0,
      confidenceSum: 0,
      confidenceCount: 0,
    };
    buckets.push(bucket);
    index.set(bucket.key, bucket);
  }

  for (const candidate of candidates) {
    const created = parseCreated(candidate);
    if (!created) continue;
    const bucket = index.get(dayKey(created));
    if (!bucket) continue;

    bucket.added += 1;
    if (isVerified(candidate)) bucket.verified += 1;
    if (needsReview(candidate)) bucket.review += 1;

    const confidence = candidate.profile?.confidence;
    if (typeof confidence === "number") {
      bucket.confidenceSum += confidence;
      bucket.confidenceCount += 1;
    }
  }

  return buckets;
}

/**
 * Running totals as of the end of each day — the shape a sparkline should show
 * for a "how big is the pool now" metric. Counts every candidate created on or
 * before the day, including those older than the window.
 */
export function buildCumulativeTrend(
  candidates: CandidateRecord[],
  days: number,
  predicate: (c: CandidateRecord) => boolean = () => true,
  now: Date = new Date(),
): TrendPoint[] {
  const buckets = buildDailyBuckets(candidates, days, now);
  if (buckets.length === 0) return [];

  const windowStart = buckets[0].date.getTime();
  const matching = candidates.filter(predicate);

  // Everything already in the pool before the window opened.
  let running = matching.filter((c) => {
    const created = parseCreated(c);
    return created ? created.getTime() < windowStart : true;
  }).length;

  const perDay = new Map<string, number>();
  for (const candidate of matching) {
    const created = parseCreated(candidate);
    if (!created || created.getTime() < windowStart) continue;
    const key = dayKey(created);
    perDay.set(key, (perDay.get(key) ?? 0) + 1);
  }

  return buckets.map((bucket) => {
    running += perDay.get(bucket.key) ?? 0;
    return { key: bucket.key, date: bucket.date, value: running };
  });
}

/**
 * Running mean parser confidence (0–100) of the whole pool as of each day.
 * Days before any resume exists carry the first known value so the line never
 * starts from a misleading zero.
 */
export function buildConfidenceTrend(
  candidates: CandidateRecord[],
  days: number,
  now: Date = new Date(),
): TrendPoint[] {
  const buckets = buildDailyBuckets(candidates, days, now);
  if (buckets.length === 0) return [];

  const windowStart = buckets[0].date.getTime();
  const scored = candidates.filter((c) => typeof c.profile?.confidence === "number");

  let sum = 0;
  let count = 0;
  const perDay = new Map<string, { sum: number; count: number }>();

  for (const candidate of scored) {
    const confidence = candidate.profile.confidence;
    const created = parseCreated(candidate);
    if (!created || created.getTime() < windowStart) {
      sum += confidence;
      count += 1;
      continue;
    }
    const key = dayKey(created);
    const entry = perDay.get(key) ?? { sum: 0, count: 0 };
    entry.sum += confidence;
    entry.count += 1;
    perDay.set(key, entry);
  }

  const points = buckets.map((bucket) => {
    const entry = perDay.get(bucket.key);
    if (entry) {
      sum += entry.sum;
      count += entry.count;
    }
    return {
      key: bucket.key,
      date: bucket.date,
      value: count > 0 ? (sum / count) * 100 : 0,
    };
  });

  // Backfill leading zeros with the first real reading.
  const firstReal = points.find((point) => point.value > 0);
  if (firstReal) {
    for (const point of points) {
      if (point.value === 0) point.value = firstReal.value;
      else break;
    }
  }

  return points;
}

/** Change across the trailing `window` days versus the `window` days before it. */
export function windowDelta(trend: TrendPoint[], window: number): Delta {
  if (trend.length === 0) return { change: 0, percent: null };

  const latest = trend[trend.length - 1].value;
  const previousIndex = trend.length - 1 - window;
  const previous = previousIndex >= 0 ? trend[previousIndex].value : trend[0].value;
  const change = latest - previous;

  return {
    change,
    percent: previous > 0 ? (change / previous) * 100 : null,
  };
}

/** Newest candidates first; records without a parseable date sort last. */
export function recentCandidates(candidates: CandidateRecord[], limit = 6): CandidateRecord[] {
  return [...candidates]
    .sort((a, b) => {
      const aTime = parseCreated(a)?.getTime() ?? 0;
      const bTime = parseCreated(b)?.getTime() ?? 0;
      return bTime - aTime;
    })
    .slice(0, limit);
}

/* Display formatters live in `./format` — they are shared with the candidate
   screens. Re-exported here so existing dashboard imports keep working. */
export {
  formatDayShort,
  formatDayLong,
  formatDateFull,
  formatInt,
  compactNumber,
  timeAgo,
  initialsOf,
} from "./format";
