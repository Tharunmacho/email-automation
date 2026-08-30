"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * The plumbing every popover control in the product shares: open state, a click
 * outside, Escape, and where the panel should be drawn.
 *
 * It lives in one place because these are the behaviours people mean when they
 * say a dropdown "feels broken" — a panel that stays open after you click the
 * page, one Escape does not close, one that opens downward off the bottom of the
 * window, one that gets cut in half by the dialog it is in. Getting them right
 * once is worth more than getting them right in the select and forgetting them
 * in the date picker.
 *
 * The panel is meant to be portalled into `document.body` and positioned from
 * `style`, NOT absolutely positioned inside its own field. That is not a
 * preference: `.modal-container` sets `overflow: hidden` and `.modal-body` sets
 * `overflow-y: auto`, so a panel rendered in the flow gets clipped at the edge
 * of the dialog — the role dropdown in the edit-user modal would have lost the
 * bottom half of its list. `position: fixed` alone does not save it either,
 * because the modal animates on a `transform`, and a transformed ancestor
 * becomes the containing block for fixed children and clips them all the same.
 */
export interface PopoverState {
  open: boolean;
  /** The panel opens upward because there is not enough room below the anchor. */
  up: boolean;
  anchorRef: React.RefObject<HTMLButtonElement | null>;
  panelRef: React.RefObject<HTMLDivElement | null>;
  /** Fixed-position style for the portalled panel. Spread onto it. */
  style: React.CSSProperties;
  setOpen: (next: boolean) => void;
  toggle: () => void;
  /** Close, and hand focus back to the trigger. */
  close: (refocus?: boolean) => void;
  /** Re-measure the anchor. Call after the panel's own content changes height. */
  measure: () => void;
}

/** Gap between the trigger and the panel, in px. Matches the old CSS offset. */
const GAP = 6;

interface Anchor {
  left: number;
  /** Viewport y where a downward panel starts. */
  top: number;
  /** Distance from the viewport bottom to the anchor's top, for an upward one. */
  bottom: number;
  width: number;
}

/**
 * @param minRoomBelow How much space the panel needs under the trigger before it
 *   gives up and opens upward. Pass the panel's own height — a calendar needs
 *   more than a six-row list.
 */
export function usePopover(minRoomBelow = 260): PopoverState {
  const [open, setOpenState] = useState(false);
  const [up, setUp] = useState(false);
  const [anchor, setAnchor] = useState<Anchor | null>(null);
  const anchorRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const measure = useCallback(() => {
    const element = anchorRef.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    setAnchor({
      left: rect.left,
      top: rect.bottom + GAP,
      bottom: window.innerHeight - rect.top + GAP,
      width: rect.width,
    });
    const below = window.innerHeight - rect.bottom;
    setUp(below < minRoomBelow && rect.top > below);
  }, [minRoomBelow]);

  const close = useCallback((refocus = true) => {
    setOpenState(false);
    if (refocus) anchorRef.current?.focus();
  }, []);

  const setOpen = useCallback((next: boolean) => setOpenState(next), []);
  const toggle = useCallback(() => setOpenState((value) => !value), []);

  // Measured before the panel paints, not after: from a passive effect the panel
  // would appear at the previous position for a frame and visibly jump.
  useLayoutEffect(() => {
    if (open) measure();
  }, [open, measure]);

  useEffect(() => {
    if (!open) return;

    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (anchorRef.current?.contains(target)) return;
      if (panelRef.current?.contains(target)) return;
      // A click on the page is a dismissal, not a selection — so no refocus.
      // Pulling focus back to the trigger here would fight whatever the person
      // actually clicked on.
      setOpenState(false);
    };

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close();
      }
    };

    /**
     * Anything that moves the trigger re-measures, so the panel stays glued to
     * it instead of hanging in the air where the field used to be.
     *
     * `capture: true` on scroll is the load-bearing part: scroll events do not
     * bubble, and the trigger is very often inside a scrolling container — a
     * modal body, the workspace column, the allocation table. Listening on
     * window without capture would miss every one of them.
     */
    const onReflow = (event: Event) => {
      // A scroll inside the panel's own list moves nothing about the anchor.
      if (event.target instanceof Node && panelRef.current?.contains(event.target)) return;
      measure();
    };

    // `pointerdown` rather than `click`: a control that closes only on mouse-up
    // stays open through a drag, and on touch the click event arrives late
    // enough to feel like a lag.
    document.addEventListener("pointerdown", onPointerDown, true);
    document.addEventListener("keydown", onKeyDown, true);
    document.addEventListener("scroll", onReflow, true);
    window.addEventListener("resize", onReflow);

    return () => {
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown, true);
      document.removeEventListener("scroll", onReflow, true);
      window.removeEventListener("resize", onReflow);
    };
  }, [open, close, measure]);

  // Hidden rather than absent until the first measurement lands, so the panel
  // cannot flash at 0,0 in the corner of the window on the way in.
  const style: React.CSSProperties = anchor
    ? {
        position: "fixed",
        left: anchor.left,
        minWidth: anchor.width,
        ...(up ? { bottom: anchor.bottom } : { top: anchor.top }),
      }
    : { position: "fixed", visibility: "hidden" };

  return { open, up, anchorRef, panelRef, style, setOpen, toggle, close, measure };
}
