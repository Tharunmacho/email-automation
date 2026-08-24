"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown } from "lucide-react";

import { usePopover } from "./usePopover";

export interface SelectOption {
  value: string;
  label: string;
  /** A second line in the list. Never shown on the closed trigger. */
  hint?: string;
  disabled?: boolean;
}

interface SelectProps {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  /** Shown when nothing is chosen. */
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  /** Needed whenever there is no visible <label> pointing at `id`. */
  ariaLabel?: string;
  /** `sm` is the in-table size — a shorter control, same type scale. */
  size?: "md" | "sm";
  className?: string;
}

/**
 * The product's dropdown.
 *
 * A native `<select>` was doing this job, and doing three things wrong that
 * mattered: its list is drawn by the operating system, so it ignores the theme
 * entirely and lands a white menu on a black page; it cannot show a second line
 * of explanation against an option, which is exactly what the role picker needs
 * ("Staff — reviews the candidates allocated to them" was being squeezed into
 * one truncated line); and its closed state cannot be styled to match the text
 * inputs beside it.
 *
 * So it is a listbox. Everything the native control gave away for free is
 * reimplemented here on purpose, and the keyboard contract is the one people
 * already know: Up/Down move, Home/End jump, Enter and Space commit, Escape
 * abandons, and typing a letter goes to the next option that starts with it.
 */
export default function Select({
  value,
  options,
  onChange,
  placeholder = "Select…",
  disabled = false,
  id,
  ariaLabel,
  size = "md",
  className = "",
}: SelectProps) {
  const { open, up, anchorRef, panelRef, style, setOpen, close } = usePopover();
  const selectedIndex = useMemo(
    () => options.findIndex((option) => option.value === value),
    [options, value],
  );
  /** Which row the keyboard is on. Distinct from what is selected. */
  const [active, setActive] = useState(selectedIndex);
  const listRef = useRef<HTMLDivElement | null>(null);
  const typed = useRef({ buffer: "", at: 0 });
  // `aria-controls` has to name the listbox, and there can be many selects on a
  // screen — so the id comes from React rather than from a constant.
  const listboxId = `${useId()}-listbox`;

  /**
   * Opening puts the cursor on the current value, not on the first row — the
   * list is a place you are already standing in, not one you arrive at.
   *
   * Done here rather than in an effect keyed on `open`. An effect would set the
   * cursor after the panel had already painted with the stale one, which is both
   * a wasted render and a visible flash of the wrong highlighted row.
   */
  const openList = () => {
    setActive(selectedIndex >= 0 ? selectedIndex : firstEnabled(options));
    setOpen(true);
  };

  // Keep the active row in view when the keyboard walks past the fold.
  useEffect(() => {
    if (!open) return;
    const row = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const selected = selectedIndex >= 0 ? options[selectedIndex] : null;

  const commit = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    close();
  };

  const step = (from: number, delta: number): number => {
    const count = options.length;
    if (count === 0) return -1;
    let next = from;
    // Walk over disabled rows rather than stopping on them, and stop walking
    // after a full lap so a list of nothing but disabled options cannot spin.
    for (let hops = 0; hops < count; hops += 1) {
      next = (next + delta + count) % count;
      if (!options[next]?.disabled) return next;
    }
    return from;
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (disabled) return;

    if (!open) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
        event.preventDefault();
        openList();
      }
      return;
    }

    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setActive((current) => step(current, 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        setActive((current) => step(current, -1));
        break;
      case "Home":
        event.preventDefault();
        setActive(firstEnabled(options));
        break;
      case "End":
        event.preventDefault();
        setActive(step(0, -1));
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        commit(active);
        break;
      case "Tab":
        // Tab is a commit-and-leave everywhere else in a form; it should not be
        // the one key that silently discards what the cursor is on.
        commit(active);
        break;
      default: {
        if (event.key.length !== 1 || event.metaKey || event.ctrlKey || event.altKey) return;
        // Typeahead. Successive letters inside a second build a prefix; a pause
        // starts a new one, so "s","t" finds "Staff" but "s" … "t" finds "T…".
        const now = performance.now();
        typed.current.buffer =
          now - typed.current.at > 900 ? event.key : typed.current.buffer + event.key;
        typed.current.at = now;
        const prefix = typed.current.buffer.toLowerCase();
        const hit = options.findIndex(
          (option) => !option.disabled && option.label.toLowerCase().startsWith(prefix),
        );
        if (hit >= 0) setActive(hit);
      }
    }
  };

  return (
    <div className={`ui-select ${size === "sm" ? "is-sm" : ""} ${className}`.trim()}>
      <button
        id={id}
        ref={anchorRef}
        type="button"
        role="combobox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        className={`ui-select-trigger ${open ? "is-open" : ""} ${selected ? "" : "is-empty"}`}
        disabled={disabled}
        onClick={() => (open ? close(false) : openList())}
        onKeyDown={onKeyDown}
      >
        <span className="ui-select-value">{selected ? selected.label : placeholder}</span>
        <ChevronDown size={15} className="ui-select-caret" aria-hidden="true" />
      </button>

      {/* Portalled to <body>, and positioned from `style`. It cannot be laid out
          inside the field: `.modal-container` is `overflow: hidden` and
          `.modal-body` is `overflow-y: auto`, so in the edit-user dialog the
          list would be cut off at the edge of the form. React events still
          bubble through the React tree rather than the DOM one, so a click on an
          option is still caught by the dialog's own stopPropagation and does not
          reach the overlay that would close it. */}
      {open &&
        createPortal(
          <div
            ref={panelRef}
            id={listboxId}
            className={`ui-pop ui-select-panel ${up ? "is-up" : ""}`}
            style={style}
            role="listbox"
            aria-label={ariaLabel}
            tabIndex={-1}
          >
            <div className="ui-select-list" ref={listRef}>
              {options.length === 0 && <p className="ui-select-none">Nothing to choose from</p>}
              {options.map((option, index) => {
                const isSelected = option.value === value;
                return (
                  <button
                    key={option.value}
                    type="button"
                    role="option"
                    aria-selected={isSelected}
                    data-index={index}
                    className={`ui-select-option ${isSelected ? "is-selected" : ""} ${
                      index === active ? "is-active" : ""
                    }`}
                    disabled={option.disabled}
                    // Hover moves the cursor too, so the pointer and the keyboard
                    // never disagree about which row is current.
                    onMouseMove={() => !option.disabled && setActive(index)}
                    onClick={() => commit(index)}
                  >
                    <span className="ui-select-option-text">
                      <span className="ui-select-option-label">{option.label}</span>
                      {option.hint && <span className="ui-select-option-hint">{option.hint}</span>}
                    </span>
                    {isSelected && (
                      <Check size={14} className="ui-select-tick" aria-hidden="true" />
                    )}
                  </button>
                );
              })}
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

function firstEnabled(options: SelectOption[]): number {
  const index = options.findIndex((option) => !option.disabled);
  return index >= 0 ? index : 0;
}
