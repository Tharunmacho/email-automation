"use client";

import { useEffect, useRef } from "react";

const FOCUSABLE = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

type ModalEntry = { element: HTMLElement; lastFocused: HTMLElement | null };
type PopoverEntry = { anchor: HTMLElement; panel: HTMLElement };
const modalStack: ModalEntry[] = [];
const popoverStack: PopoverEntry[] = [];
let originalBodyOverflow = "";

/** Connect body-portaled controls to their owning dialog's focus boundary. */
export function registerModalPopover(anchor: HTMLElement, panel: HTMLElement) {
  const entry = { anchor, panel };
  popoverStack.push(entry);
  return () => {
    const index = popoverStack.indexOf(entry);
    if (index >= 0) popoverStack.splice(index, 1);
  };
}

/** Only the visible, uppermost popover may consume a dismissal key. */
export function isTopModalPopover(panel: HTMLElement) {
  const top = popoverStack.filter((entry) => entry.panel.isConnected).at(-1);
  const modal = modalStack.at(-1);
  return top?.panel === panel && (!modal || modal.element.contains(top.anchor));
}

function visible(element: HTMLElement) {
  return element.getClientRects().length > 0
    && getComputedStyle(element).visibility !== "hidden"
    && !element.closest("[inert], [aria-hidden='true']");
}

function controlsIn(element: HTMLElement) {
  return Array.from(element.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (control) => control.tabIndex >= 0 && visible(control),
  );
}

/** Focus, Escape, scroll-lock, and Tab containment for an aria-modal dialog. */
export function useModalFocus<T extends HTMLElement>(open: boolean, onClose: () => void) {
  const dialogRef = useRef<T>(null);
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return;
    const dialog = dialogRef.current;
    if (!dialog) return;

    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const entry: ModalEntry = { element: dialog, lastFocused: null };
    if (modalStack.length === 0) originalBodyOverflow = document.body.style.overflow;
    modalStack.push(entry);
    document.body.style.overflow = "hidden";

    const ownedPopovers = () => popoverStack.filter(
      (popover) => dialog.contains(popover.anchor) && popover.panel.isConnected,
    );
    const inside = (element: Node | null) => element !== null && (
      dialog.contains(element) || ownedPopovers().some((popover) => popover.panel.contains(element))
    );
    const focusInitial = () => {
      const preferred = dialog.querySelector<HTMLElement>("[data-dialog-initial-focus]");
      const first = preferred && visible(preferred) ? preferred : controlsIn(dialog)[0];
      (first ?? dialog).focus({ preventScroll: true });
    };
    const frame = window.requestAnimationFrame(() => {
      if (modalStack.at(-1) === entry) focusInitial();
    });

    const onKeyDown = (event: KeyboardEvent) => {
      if (modalStack.at(-1) !== entry || event.defaultPrevented) return;
      if (event.key === "Escape") {
        // A Select or date picker owns Escape while its portaled panel is open.
        if (ownedPopovers().length > 0) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const controls = controlsIn(dialog);
      // Calendars live under body. Insert their controls after the field that
      // opened them, so keyboard order still follows the visible form. Select
      // keeps focus on its combobox and handles its own arrow/Tab contract.
      const calendars = ownedPopovers().filter((popover) => popover.panel.getAttribute("role") !== "listbox");
      for (const popover of calendars) {
        const anchorIndex = controls.indexOf(popover.anchor);
        controls.splice(anchorIndex < 0 ? controls.length : anchorIndex + 1, 0, ...controlsIn(popover.panel));
      }
      if (controls.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = controls[0];
      const last = controls[controls.length - 1];
      const activeIndex = controls.indexOf(document.activeElement as HTMLElement);
      if (calendars.length > 0 || activeIndex < 0) {
        event.preventDefault();
        const nextIndex = activeIndex < 0
          ? (event.shiftKey ? controls.length - 1 : 0)
          : (activeIndex + (event.shiftKey ? -1 : 1) + controls.length) % controls.length;
        controls[nextIndex].focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    const onFocusIn = (event: FocusEvent) => {
      if (modalStack.at(-1) !== entry) return;
      if (inside(event.target as Node)) {
        entry.lastFocused = event.target instanceof HTMLElement ? event.target : null;
      } else if (entry.lastFocused?.isConnected && visible(entry.lastFocused)) {
        entry.lastFocused.focus({ preventScroll: true });
      } else {
        focusInitial();
      }
    };

    // Bubble phase lets controls consume Escape and Tab before their dialog.
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      const index = modalStack.indexOf(entry);
      if (index >= 0) modalStack.splice(index, 1);
      if (modalStack.length === 0) document.body.style.overflow = originalBodyOverflow;
      window.requestAnimationFrame(() => {
        const remaining = modalStack.at(-1);
        if (opener?.isConnected && visible(opener) && (!remaining || remaining.element.contains(opener))) {
          opener.focus({ preventScroll: true });
        }
      });
    };
  }, [open]);

  return dialogRef;
}
