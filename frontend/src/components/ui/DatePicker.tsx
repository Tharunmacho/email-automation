"use client";

import { useEffect, useId, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { CalendarDays, ChevronLeft, ChevronRight, X } from "lucide-react";

import { usePopover } from "./usePopover";

interface DatePickerProps {
  /** `yyyy-mm-dd`, or "" for empty — the same wire format `<input type="date">` used. */
  value: string;
  onChange: (value: string) => void;
  /** Inclusive bounds, same format. Days outside them are shown and refused. */
  min?: string;
  max?: string;
  placeholder?: string;
  disabled?: boolean;
  /** Offer a Clear control. Off where the field is required. */
  clearable?: boolean;
  id?: string;
  ariaLabel?: string;
  className?: string;
  /** Month fields keep the yyyy-mm wire format. */
  mode?: "date" | "month";
}

const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];

/**
 * The product's date field.
 *
 * `<input type="date">` was doing this, and it is the worst-behaved native
 * control in a themed app: the calendar is drawn by the browser, so it takes
 * none of the product's colours; the trigger glyph cannot be styled or even
 * reliably positioned; the text format follows the machine's locale rather than
 * the one the rest of the screen prints dates in; and Firefox, Safari and
 * Chrome each show a different popup, so no two people see the same field.
 *
 * This is a plain month grid instead. Dates are handled as `yyyy-mm-dd` strings
 * and split by hand — never through `new Date("2026-03-01")`, which parses as
 * UTC midnight and so lands on February 28th for anyone west of Greenwich. That
 * off-by-one-day is the classic bug in a hand-rolled picker, and it is why every
 * conversion here goes through `parse` and `serialise` below.
 */
export default function DatePicker({
  value,
  onChange,
  min,
  max,
  placeholder = "Pick a date",
  disabled = false,
  clearable = true,
  id,
  ariaLabel,
  className = "",
  mode = "date",
}: DatePickerProps) {
  // A six-week grid plus its header and footer is about 330px tall, so it needs
  // more room under the field than a list does before it flips upward.
  const { open, up, anchorRef, panelRef, style, setOpen, close } = usePopover(340);
  const selected = useMemo(() => parse(value), [value]);
  /** Which month the grid shows. Not the selection — you browse away from it. */
  const [cursor, setCursor] = useState(() => startOfMonth(selected ?? today()));
  const [view, setView] = useState<"days" | "months" | "years">(mode === "month" ? "months" : "days");
  const [focused, setFocused] = useState(value);
  const panelId = `${useId()}-calendar`;

  /**
   * Re-opening returns to the month the value is in, however far the last visit
   * browsed away from it.
   *
   * Set on the way in rather than from an effect keyed on `open`: an effect
   * would run after the calendar had already painted the month left over from
   * last time, so the grid would visibly jump to the right one a frame later.
   */
  const openCalendar = () => {
    const day = selected ?? today();
    setCursor(startOfMonth(day));
    setFocused(serialise(day));
    setView(mode === "month" ? "months" : "days");
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => {
      const selector = view === "days" ? `[data-date="${focused}"]` : view === "months" ? `[data-month="${cursor.m}"]` : `[data-year="${cursor.y}"]`;
      const preferred = panelRef.current?.querySelector<HTMLButtonElement>(`${selector}:not(:disabled)`);
      (preferred ?? panelRef.current?.querySelector<HTMLButtonElement>(".ui-date-choice:not(:disabled), .ui-date-day:not(:disabled)"))?.focus({ preventScroll: true });
    });
    return () => cancelAnimationFrame(frame);
  }, [open, view, cursor, focused, panelRef]);

  const days = useMemo(() => buildGrid(cursor), [cursor]);
  const lower = parse(min);
  const upper = parse(max);
  const now = today();

  const outOfRange = (day: Cal): boolean =>
    (lower !== null && compare(mode === "month" ? startOfMonth(day) : day, lower) < 0) || (upper !== null && compare(mode === "month" ? startOfMonth(day) : day, upper) > 0);

  const pick = (day: Cal) => {
    if (outOfRange(day)) return;
    onChange(mode === "month" ? serialise(day).slice(0, 7) : serialise(day));
    close();
  };

  const shiftMonth = (delta: number) => {
    const month = cursor.m + delta;
    const next = { y: cursor.y + Math.floor((month - 1) / 12), m: ((month - 1 + 12) % 12) + 1, d: 1 };
    setCursor(next);
    setFocused(serialise(next));
  };
  const shift = (delta: number) => {
    if (view === "days") shiftMonth(delta);
    else setCursor((current) => ({ ...current, y: current.y + delta * (view === "years" ? 12 : 1) }));
  };
  const moveDay = (event: React.KeyboardEvent, day: Cal) => {
    const delta = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -weekdayIndex(day), End: 6 - weekdayIndex(day) }[event.key];
    if (delta === undefined && event.key !== "PageUp" && event.key !== "PageDown") return;
    event.preventDefault();
    const moved = event.key === "PageUp" || event.key === "PageDown"
      ? new Date(day.y, day.m - 1 + (event.key === "PageUp" ? -1 : 1), 1)
      : new Date(day.y, day.m - 1, day.d + (delta ?? 0));
    const next = { y: moved.getFullYear(), m: moved.getMonth() + 1, d: Math.min(day.d, daysInMonth(moved.getFullYear(), moved.getMonth() + 1)) };
    if (delta !== undefined) next.d = moved.getDate();
    if (outOfRange(next)) return;
    setFocused(serialise(next));
    setCursor(startOfMonth(next));
  };
  const yearStart = Math.floor(cursor.y / 12) * 12;

  return (
    <div className={`ui-date ${className}`.trim()}>
      <button
        id={id}
        ref={anchorRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={ariaLabel}
        className={`ui-date-trigger ${open ? "is-open" : ""} ${selected ? "" : "is-empty"}`}
        disabled={disabled}
        onClick={() => (open ? close(false) : openCalendar())}
      >
        <CalendarDays size={15} className="ui-date-icon" aria-hidden="true" />
        <span className="ui-date-value">{selected ? mode === "month" ? monthLabel(selected) : longDate(selected) : placeholder}</span>
        {clearable && selected && (
          // A span, not a button: a button inside a button is invalid markup and
          // browsers resolve it by dropping one of the two. The keyboard reaches
          // Clear from the panel's own footer instead.
          <span
            role="presentation"
            className="ui-date-clear"
            title="Clear the date"
            onClick={(event) => {
              event.stopPropagation();
              onChange("");
            }}
          >
            <X size={13} />
          </span>
        )}
      </button>

      {/* Portalled for the same reason the select's list is: the Job Orders edit
          dialog sets `overflow: hidden`, and a calendar laid out inside the form
          loses its bottom two weeks to that edge. */}
      {open &&
        createPortal(
        <div
          ref={panelRef}
          id={panelId}
          className={`ui-pop ui-date-panel ${up ? "is-up" : ""}`}
          style={{ ...style, minWidth: 0, width: 320, maxWidth: "calc(100vw - 16px)" }}
          role="dialog"
          aria-label={ariaLabel ?? "Choose a date"}
        >
          <div className="ui-date-head">
            <button
              type="button"
              className="ui-date-nav"
              onClick={() => shift(-1)}
              aria-label={view === "days" ? "Previous month" : view === "months" ? "Previous year" : "Previous 12 years"}
            >
              <ChevronLeft size={15} />
            </button>
            <button type="button" className="ui-date-month" onClick={() => setView(view === "days" ? "months" : view === "months" ? "years" : "months")} aria-label={view === "days" ? "Choose month" : "Choose year"}>{view === "days" ? monthLabel(cursor) : view === "months" ? cursor.y : `${yearStart}–${yearStart + 11}`}</button>
            <button
              type="button"
              className="ui-date-nav"
              onClick={() => shift(1)}
              aria-label={view === "days" ? "Next month" : view === "months" ? "Next year" : "Next 12 years"}
            >
              <ChevronRight size={15} />
            </button>
          </div>

          {view === "days" && <><div className="ui-date-weekdays" aria-hidden="true">
            {WEEKDAYS.map((day) => (
              <span key={day}>{day}</span>
            ))}
          </div>

          <div className="ui-date-grid">
            {days.map((day) => {
              const isSelected = selected !== null && compare(day, selected) === 0;
              return (
                <button
                  key={`${day.y}-${day.m}-${day.d}`}
                  type="button"
                  className={`ui-date-day ${day.m !== cursor.m ? "is-outside" : ""} ${
                    isSelected ? "is-selected" : ""
                  } ${compare(day, now) === 0 ? "is-today" : ""}`}
                  disabled={outOfRange(day)}
                  data-date={serialise(day)}
                  aria-label={longDate(day)}
                  tabIndex={serialise(day) === focused ? 0 : -1}
                  aria-current={isSelected ? "date" : undefined}
                  onClick={() => pick(day)}
                  onKeyDown={(event) => moveDay(event, day)}
                >
                  {day.d}
                </button>
              );
            })}
          </div></>}

          {view === "months" && <div className="ui-date-choices">{Array.from({ length: 12 }, (_, index) => {
            const month = { y: cursor.y, m: index + 1, d: 1 };
            const disabledMonth = (lower && compare({ ...month, d: daysInMonth(month.y, month.m) }, lower) < 0) || (upper && compare(month, upper) > 0);
            return <button type="button" key={index} data-month={index + 1} disabled={Boolean(disabledMonth)} className={`ui-date-choice ${selected?.y === month.y && selected.m === month.m ? "is-selected" : ""}`} aria-label={monthLabel(month)} onClick={() => { if (mode === "month") pick(month); else { setCursor(month); setFocused(serialise(month)); setView("days"); } }}>{new Date(2000, index, 1).toLocaleDateString("en", { month: "short" })}</button>;
          })}</div>}
          {view === "years" && <div className="ui-date-choices">{Array.from({ length: 12 }, (_, index) => yearStart + index).map((year) => <button type="button" key={year} data-year={year} className={`ui-date-choice ${selected?.y === year ? "is-selected" : ""}`} disabled={Boolean((lower && year < lower.y) || (upper && year > upper.y))} onClick={() => { setCursor((current) => ({ ...current, y: year })); setView("months"); }}>{year}</button>)}</div>}

          <div className="ui-date-foot">
            <button
              type="button"
              className="ui-date-quick"
              onClick={() => pick(now)}
              disabled={outOfRange(now)}
            >
              {mode === "month" ? "This month" : "Today"}
            </button>
            {clearable && (
              <button
                type="button"
                className="ui-date-quick"
                onClick={() => {
                  onChange("");
                  close();
                }}
              >
                Clear
              </button>
            )}
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

/* ---- dates as three numbers ------------------------------------------------
   Never a `Date` built from a string. `new Date("2026-03-01")` is UTC midnight,
   which is the last day of February in every timezone behind Greenwich — the
   picker would show one day and send another. A `{y, m, d}` triple has no
   timezone to be wrong about. `Date` is used for exactly two things below:
   asking the calendar how long a month is, and asking what today is. */

interface Cal {
  y: number;
  m: number;
  d: number;
}

function parse(value: string | undefined): Cal | null {
  if (!value) return null;
  const match = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/.exec(value);
  if (!match) return null;
  const day = { y: Number(match[1]), m: Number(match[2]), d: Number(match[3] || 1) };
  return day.m >= 1 && day.m <= 12 && day.d >= 1 && day.d <= daysInMonth(day.y, day.m) ? day : null;
}

function serialise(day: Cal): string {
  return `${day.y}-${String(day.m).padStart(2, "0")}-${String(day.d).padStart(2, "0")}`;
}

function today(): Cal {
  const now = new Date();
  return { y: now.getFullYear(), m: now.getMonth() + 1, d: now.getDate() };
}

function compare(a: Cal, b: Cal): number {
  return a.y - b.y || a.m - b.m || a.d - b.d;
}

function startOfMonth(day: Cal): Cal {
  return { y: day.y, m: day.m, d: 1 };
}

function daysInMonth(y: number, m: number): number {
  // Day 0 of the next month is the last day of this one.
  return new Date(y, m, 0).getDate();
}

/** Monday-first weekday index, 0–6. `getDay()` is Sunday-first. */
function weekdayIndex(day: Cal): number {
  return (new Date(day.y, day.m - 1, day.d).getDay() + 6) % 7;
}

/**
 * Six weeks, always. A grid that is five rows in some months and six in others
 * changes height as you page through it, which moves the buttons out from under
 * the pointer mid-click.
 */
function buildGrid(cursor: Cal): Cal[] {
  const lead = weekdayIndex(startOfMonth(cursor));
  const cells: Cal[] = [];
  const prev = cursor.m === 1 ? { y: cursor.y - 1, m: 12 } : { y: cursor.y, m: cursor.m - 1 };
  const prevLength = daysInMonth(prev.y, prev.m);

  for (let i = lead; i > 0; i -= 1) {
    cells.push({ y: prev.y, m: prev.m, d: prevLength - i + 1 });
  }
  const length = daysInMonth(cursor.y, cursor.m);
  for (let d = 1; d <= length; d += 1) cells.push({ y: cursor.y, m: cursor.m, d });

  const next = cursor.m === 12 ? { y: cursor.y + 1, m: 1 } : { y: cursor.y, m: cursor.m + 1 };
  for (let d = 1; cells.length < 42; d += 1) cells.push({ y: next.y, m: next.m, d });

  return cells;
}

function monthLabel(day: Cal): string {
  return new Date(day.y, day.m - 1, 1).toLocaleDateString("en-GB", {
    month: "long",
    year: "numeric",
  });
}

function longDate(day: Cal): string {
  return new Date(day.y, day.m - 1, day.d).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}
