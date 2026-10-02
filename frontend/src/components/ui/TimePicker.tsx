"use client";

import Select from "./Select";

const HOURS = Array.from({ length: 24 }, (_, hour) => ({ value: String(hour).padStart(2, "0"), label: String(hour).padStart(2, "0") }));
const MINUTES = Array.from({ length: 60 }, (_, minute) => ({ value: String(minute).padStart(2, "0"), label: String(minute).padStart(2, "0") }));

interface TimePickerProps {
  /** Local 24-hour time, HH:mm. */
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  id?: string;
  ariaLabel?: string;
}

export default function TimePicker({ value, onChange, disabled, id, ariaLabel = "Time" }: TimePickerProps) {
  const [hour = "", minute = ""] = value.slice(0, 5).split(":");
  return <div className="ui-time" role="group" aria-label={`${ariaLabel} (24-hour)`}>
    <Select id={id} value={hour} options={HOURS} placeholder="HH" disabled={disabled} ariaLabel={`${ariaLabel} hour`} onChange={(next) => onChange(`${next}:${minute || "00"}`)} />
    <span className="ui-time-separator" aria-hidden="true">:</span>
    <Select value={minute} options={MINUTES} placeholder="MM" disabled={disabled} ariaLabel={`${ariaLabel} minute`} onChange={(next) => onChange(`${hour || "00"}:${next}`)} />
  </div>;
}
