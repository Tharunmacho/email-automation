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
export const PALETTES = [
  { id: "classic", label: "Default" },
  { id: "blue", label: "Blue & White" },
  { id: "purple", label: "Purple & White" },
  { id: "green", label: "Green & White" },
] as const;
export type Palette = (typeof PALETTES)[number]["id"];

const STORAGE_KEY = "ats_theme";
const PALETTE_STORAGE_KEY = "ats_palette";

const listeners = new Set<() => void>();
let cache: Theme | null = null;
let paletteCache: Palette | null = null;

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

/** Palette and light/dark are independent, so either control keeps the other choice. */
export function getPaletteSnapshot(): Palette {
  if (paletteCache === null) {
    paletteCache = "classic";
    if (typeof window !== "undefined") {
      try {
        const stored = window.localStorage.getItem(PALETTE_STORAGE_KEY);
        const paletteId = stored === "teal" ? "green" : stored === "indigo" ? "purple" : stored;
        paletteCache = PALETTES.find((palette) => palette.id === paletteId)?.id ?? "classic";
      } catch {
        // Storage is optional; the default remains usable.
      }
      document.documentElement.dataset.palette = paletteCache;
    }
  }
  return paletteCache;
}

export function getPaletteServerSnapshot(): Palette {
  return "classic";
}

export function setPalette(palette: Palette): void {
  paletteCache = palette;
  if (typeof document !== "undefined") document.documentElement.dataset.palette = palette;
  try {
    window.localStorage.setItem(PALETTE_STORAGE_KEY, palette);
  } catch {
    // The selection still works when storage is unavailable.
  }
  for (const listener of listeners) listener();
}
