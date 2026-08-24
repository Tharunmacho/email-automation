"use client";

import type { ReactNode } from "react";
import { Check, Lock, Minus } from "lucide-react";

interface CheckboxProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: ReactNode;
  /** One short line under the label. Use it for consequence, not for restating. */
  hint?: ReactNode;
  /** Neither on nor off — a group control whose children disagree. */
  indeterminate?: boolean;
  disabled?: boolean;
  /**
   * Ticked, and not yours to untick. Drawn with a padlock rather than a greyed
   * tick, because "on because the role says so" and "off and unavailable" are
   * two different facts and a disabled checkbox states them identically.
   */
  locked?: boolean;
  id?: string;
  className?: string;
}

/**
 * The product's checkbox.
 *
 * The native control is still the control — it keeps the label association, the
 * space bar, the tab order and whatever a screen reader wants from it. It is
 * only moved out of sight, and the box beside it is drawn in CSS from
 * `:checked` and `:focus-visible`, so nothing about the behaviour is
 * reimplemented in JavaScript. That is the whole trick: `appearance: none` on
 * the real input, not a `<div>` pretending to be one.
 */
export default function Checkbox({
  checked,
  onChange,
  label,
  hint,
  indeterminate = false,
  disabled = false,
  locked = false,
  id,
  className = "",
}: CheckboxProps) {
  const isOn = locked || checked;
  const isFixed = disabled || locked;

  return (
    <label
      className={`ui-check ${isOn ? "is-on" : ""} ${isFixed ? "is-fixed" : ""} ${
        locked ? "is-locked" : ""
      } ${className}`.trim()}
    >
      <input
        id={id}
        type="checkbox"
        className="ui-check-input"
        checked={isOn}
        disabled={isFixed}
        // `aria-checked` carries the third state; `checked` cannot express it.
        aria-checked={indeterminate ? "mixed" : isOn}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="ui-check-box" aria-hidden="true">
        {locked ? (
          <Lock size={10} strokeWidth={2.75} />
        ) : indeterminate ? (
          <Minus size={12} strokeWidth={3.25} />
        ) : (
          <Check size={12} strokeWidth={3.25} />
        )}
      </span>
      <span className="ui-check-text">
        <span className="ui-check-label">{label}</span>
        {hint && <span className="ui-check-hint">{hint}</span>}
      </span>
    </label>
  );
}
