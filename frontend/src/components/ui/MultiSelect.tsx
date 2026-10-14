"use client";

import { useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { Check, ChevronDown, X } from "lucide-react";

import { usePopover } from "./usePopover";
import type { SelectOption } from "./Select";

interface MultiSelectProps {
  /** The chosen values, in the order they were chosen. */
  values: string[];
  options: SelectOption[];
  onChange: (values: string[]) => void;
  /** Shown when nothing is chosen. */
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  /** Needed whenever there is no visible <label> pointing at `id`. */
  ariaLabel?: string;
  size?: "md" | "sm";
  className?: string;
}

/**
 * The product's dropdown, for a field that takes more than one answer.
 *
 * A sibling of `Select` rather than a mode of it: the two differ in more than
 * arity. Choosing here does not close the list — picking four countries should
 * not mean opening the same menu four times — the trigger carries removable
 * chips instead of one line of text, and the rows are checkboxes, so `aria`
 * says `aria-multiselectable` and the ticks mean "included" rather than "this
 * one". The keyboard contract is otherwise the one `Select` established: Up and
 * Down move, Home and End jump, Enter and Space toggle, Escape closes, and
 * typing a letter jumps to the next option starting with it.
 *
 * Order is the order of choosing, and is meaningful to callers that treat the
 * first answer as the primary one.
 */
export default function MultiSelect({
  values,
  options,
  onChange,
  placeholder = "Select…",
  disabled = false,
  id,
  ariaLabel,
  size = "md",
  className = "",
}: MultiSelectProps) {
  const { open, up, anchorRef, panelRef, style, setOpen, close } = usePopover();
  const chosen = useMemo(() => new Set(values), [values]);
  /** Which row the keyboard is on. Distinct from what is chosen. */
  const [active, setActive] = useState(-1);
  const listRef = useRef<HTMLDivElement | null>(null);
  const typed = useRef({ buffer: "", at: 0 });
  const listboxId = `${useId()}-listbox`;

  /** Opening lands on the first chosen row, or on the first choosable one. */
  const openList = () => {
    const firstChosen = options.findIndex((option) => chosen.has(option.value) && !option.disabled);
    setActive(firstChosen >= 0 ? firstChosen : firstEnabled(options));
    setOpen(true);
  };

  // Keep the active row in view when the keyboard walks past the fold.
  useEffect(() => {
    if (!open) return;
    const row = listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`);
    row?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const toggle = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(
      chosen.has(option.value)
        ? values.filter((value) => value !== option.value)
        : [...values, option.value],
    );
  };

  const remove = (value: string) => onChange(values.filter((current) => current !== value));

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
        return;
      }
      // Backspace on the closed control takes the last chip back, the way it
      // works in every token field people already use.
      if (event.key === "Backspace" && values.length > 0) {
        event.preventDefault();
        onChange(values.slice(0, -1));
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
        // Toggles and stays: the list is a set being assembled, and closing it
        // after every pick is what makes multi-select tedious.
        event.preventDefault();
        toggle(active);
        break;
      case "Tab":
        close();
        break;
      default: {
        if (event.key.length !== 1 || event.metaKey || event.ctrlKey || event.altKey) return;
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

  const labelOf = (value: string) =>
    options.find((option) => option.value === value)?.label ?? value;

  return (
    <div className={`ui-select ui-multiselect ${size === "sm" ? "is-sm" : ""} ${className}`.trim()}>
      <button
        id={id}
        ref={anchorRef}
        type="button"
        role="combobox"
        aria-expanded={open}
        aria-controls={listboxId}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        aria-activedescendant={open && active >= 0 && options[active] ? `${listboxId}-option-${active}` : undefined}
        className={`ui-select-trigger ${open ? "is-open" : ""} ${values.length ? "" : "is-empty"}`}
        disabled={disabled}
        onClick={() => (open ? close(false) : openList())}
        onKeyDown={onKeyDown}
      >
        {values.length === 0 ? (
          <span className="ui-select-value">{placeholder}</span>
        ) : (
          <span className="ui-multiselect-chips">
            {values.map((value) => (
              <span className="ui-multiselect-chip" key={value}>
                <span className="ui-multiselect-chip-text">{labelOf(value)}</span>
                {/* A span, not a nested <button>: the trigger is already a
                    button and a button inside one is invalid markup that
                    browsers resolve by dropping the inner element. */}
                <span
                  role="button"
                  tabIndex={-1}
                  aria-label={`Remove ${labelOf(value)}`}
                  className="ui-multiselect-chip-remove"
                  onMouseDown={(event) => event.stopPropagation()}
                  onClick={(event) => {
                    event.stopPropagation();
                    remove(value);
                  }}
                >
                  <X size={11} aria-hidden="true" />
                </span>
              </span>
            ))}
          </span>
        )}
        <ChevronDown size={15} className="ui-select-caret" aria-hidden="true" />
      </button>

      {/* Portalled to <body> for the same reason `Select`'s panel is: the field
          can sit inside a dialog that clips its own overflow. */}
      {open &&
        createPortal(
          <div
            ref={panelRef}
            id={listboxId}
            className={`ui-pop ui-select-panel ${up ? "is-up" : ""}`}
            style={style}
            role="listbox"
            aria-multiselectable="true"
            aria-label={ariaLabel}
            tabIndex={-1}
          >
            <div className="ui-select-list" ref={listRef}>
              {options.length === 0 && <p className="ui-select-none">Nothing to choose from</p>}
              {options.map((option, index) => {
                const isChosen = chosen.has(option.value);
                return (
                  <button
                    key={option.value}
                    id={`${listboxId}-option-${index}`}
                    type="button"
                    tabIndex={-1}
                    role="option"
                    aria-selected={isChosen}
                    data-index={index}
                    className={`ui-select-option ${isChosen ? "is-selected" : ""} ${
                      index === active ? "is-active" : ""
                    }`}
                    disabled={option.disabled}
                    onMouseMove={() => !option.disabled && setActive(index)}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => toggle(index)}
                  >
                    <span className={`ui-multiselect-box ${isChosen ? "is-on" : ""}`} aria-hidden="true">
                      {isChosen && <Check size={11} />}
                    </span>
                    <span className="ui-select-option-text">
                      <span className="ui-select-option-label">{option.label}</span>
                      {option.hint && <span className="ui-select-option-hint">{option.hint}</span>}
                    </span>
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
  return options.findIndex((option) => !option.disabled);
}
