/**
 * Read-only mirrors of the last successful API response.
 *
 * The server is the source of truth. These caches exist so a screen still shows
 * something when the API is unreachable; a successful response — including an
 * empty one — always wins over them. They are never replayed back to the API.
 *
 * That last rule is the whole point. An earlier version re-POSTed every cached
 * record on mount, and because the create endpoints upsert, a record deleted
 * from one browser was resurrected by any other browser still holding it
 * locally. A cache that writes back is not a cache, it is a second database.
 */

export const CACHE_KEYS = {
  jobOrders: "job_orders_records",
  sourcingClients: "sourcing_records",
} as const;

/** Returns the mirrored list, or null when there is nothing usable stored. */
export function readCache<T>(key: string): T[] | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as T[]) : null;
  } catch {
    // Malformed JSON from an older schema, or storage blocked entirely.
    return null;
  }
}

export function writeCache<T>(key: string, records: T[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, JSON.stringify(records));
  } catch {
    // Quota exceeded or private browsing — the mirror is optional.
  }
}
