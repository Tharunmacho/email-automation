/**
 * Light/dark, driven by the rail's segmented pill.
 *
 * The choice is stamped as `data-theme` on <html>, where the token blocks in
 * globals.css pick it up — no component reads a theme value, so nothing has to
 * re-render for the whole product to change.
 *
 * Read through `useSyncExternalStore` for the same reason the activity log is:
 * localStorage cannot be touched on the server, and loading it from an effect
 * would paint the light theme first and flip to dark a frame later.
 */

export type Theme = "light" | "dark";

const STORAGE_KEY = "ats_theme";

const listeners = new Set<() => void>();
let cache: Theme | null = null;

/**
 * Light is the product's default, on every visit and every refresh. The
 * operating system does not get a vote: the screens are designed and reviewed
 * light, so a machine set to dark used to open on a theme its owner never asked
 * this app for. Dark is reached one way only — by pressing the pill, which is
 * the choice that gets remembered.
 */
function readStored(): Theme {
  if (typeof window === "undefined") return "light";
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "dark" ? "dark" : "light";
  } catch {
    return "light";
  }
}

/** Puts the current theme on <html> so the CSS token blocks resolve. */
function paint(theme: Theme): void {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

export function subscribeTheme(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getThemeSnapshot(): Theme {
  if (cache === null) {
    cache = readStored();
    paint(cache);
  }
  return cache;
}

/** The server, and the hydration pass, always render light. */
export function getThemeServerSnapshot(): Theme {
  return "light";
}

export function setTheme(theme: Theme): void {
  cache = theme;
  paint(theme);
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // Private browsing — the choice just will not survive the tab.
  }
  for (const listener of listeners) listener();
}
