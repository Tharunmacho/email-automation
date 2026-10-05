"use client";

import { useState } from "react";
import DatePicker from "./DatePicker";
import TimePicker from "./TimePicker";

interface DateTimePickerProps {
  /** Local date and time, yyyy-mm-ddTHH:mm, without a timezone conversion. */
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
  ariaLabel?: string;
}

export default function DateTimePicker({ value, onChange, disabled, ariaLabel = "Date and time" }: DateTimePickerProps) {
  const [date = "", time = ""] = value.split("T");
  const [draft, setDraft] = useState({ date, time });
  const [lastValue, setLastValue] = useState(value);
  if (value !== lastValue) {
    setLastValue(value);
    setDraft({ date, time });
  }
  const change = (field: "date" | "time", next: string) => {
    const updated = { ...draft, [field]: next };
    setDraft(updated);
    onChange(updated.date && updated.time ? `${updated.date}T${updated.time}` : "");
  };
  return <div className="ui-datetime" role="group" aria-label={ariaLabel}>
    <DatePicker value={draft.date} ariaLabel={`${ariaLabel} date`} disabled={disabled} onChange={(next) => change("date", next)} />
    <TimePicker value={draft.time} ariaLabel={`${ariaLabel} time`} disabled={disabled} onChange={(next) => change("time", next)} />
  </div>;
}
