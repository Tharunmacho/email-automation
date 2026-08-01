"use client";

import { useSyncExternalStore } from "react";

/** Never fires — the value is constant per environment, it just never changes. */
const subscribe = () => () => {};
const getSnapshot = () => true;
const getServerSnapshot = () => false;

/**
 * `false` during the server render and the first client paint, `true` after.
 * Use it to hold back UI derived from the clock or the timezone, which the
 * server cannot render the same way the browser will.
 *
 * Preferred over a `useState` + `useEffect` mount flag: no extra render pass,
 * and no `set-state-in-effect` cascade.
 */
export function useIsMounted(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
