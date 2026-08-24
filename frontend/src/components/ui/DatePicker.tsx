"use client";

import { useMemo, useState } from "react";
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
}: DatePickerProps) {
  // A six-week grid plus its header and footer is about 330px tall, so it needs
  // more room under the field than a list does before it flips upward.
  const { open, up, anchorRef, panelRef, style, setOpen, close } = usePopover(340);
  const selected = useMemo(() => parse(value), [value]);
  /** Which month the grid shows. Not the selection — you browse away from it. */
  const [cursor, setCursor] = useState(() => startOfMonth(selected ?? today()));

  /**
   * Re-opening returns to the month the value is in, however far the last visit
   * browsed away from it.
   *
   * Set on the way in rather than from an effect keyed on `open`: an effect
   * would run after the calendar had already painted the month left over from
   * last time, so the grid would visibly jump to the right one a frame later.
   */
  const openCalendar = () => {
    setCursor(startOfMonth(selected ?? today()));
    setOpen(true);
  };

  const days = useMemo(() => buildGrid(cursor), [cursor]);
  const lower = parse(min);
  const upper = parse(max);
  const now = today();

  const outOfRange = (day: Cal): boolean =>
    (lower !== null && compare(day, lower) < 0) || (upper !== null && compare(day, upper) > 0);

  const pick = (day: Cal) => {
    if (outOfRange(day)) return;
    onChange(serialise(day));
    close();
  };

  const shiftMonth = (delta: number) => {
    setCursor((current) => {
      const month = current.m + delta;
      return { y: current.y + Math.floor((month - 1) / 12), m: ((month - 1 + 12) % 12) + 1, d: 1 };
    });
  };

  return (
    <div className={`ui-date ${className}`.trim()}>
      <button
        id={id}
        ref={anchorRef}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={ariaLabel}
        className={`ui-date-trigger ${open ? "is-open" : ""} ${selected ? "" : "is-empty"}`}
        disabled={disabled}
        onClick={() => (open ? close(false) : openCalendar())}
      >
        <CalendarDays size={15} className="ui-date-icon" aria-hidden="true" />
        <span className="ui-date-value">{selected ? longDate(selected) : placeholder}</span>
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
          className={`ui-pop ui-date-panel ${up ? "is-up" : ""}`}
          style={style}
          role="dialog"
          aria-label={ariaLabel ?? "Choose a date"}
        >
          <div className="ui-date-head">
            <button
              type="button"
              className="ui-date-nav"
              onClick={() => shiftMonth(-1)}
              aria-label="Previous month"
            >
              <ChevronLeft size={15} />
            </button>
            <span className="ui-date-month">{monthLabel(cursor)}</span>
            <button
              type="button"
              className="ui-date-nav"
              onClick={() => shiftMonth(1)}
              aria-label="Next month"
            >
              <ChevronRight size={15} />
            </button>
          </div>

          <div className="ui-date-weekdays" aria-hidden="true">
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
                  aria-current={isSelected ? "date" : undefined}
                  onClick={() => pick(day)}
                >
                  {day.d}
                </button>
              );
            })}
          </div>

          <div className="ui-date-foot">
            <button
              type="button"
              className="ui-date-quick"
              onClick={() => pick(now)}
              disabled={outOfRange(now)}
            >
              Today
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
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return null;
  return { y: Number(match[1]), m: Number(match[2]), d: Number(match[3]) };
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
