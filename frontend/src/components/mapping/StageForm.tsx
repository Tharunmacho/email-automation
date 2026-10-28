"use client";

/**
 * The fields one step asks for, and its Save.
 *
 * Checked here the same way the API checks them (`build_entry` in
 * `app/mapping_pipeline.py`), so a missing flight number is pointed at beside
 * the field rather than coming back as a toast. The API still decides: this
 * only saves a round trip.
 */

import { useId, useState } from "react";
import { AlertTriangle, Check, Loader2, Lock } from "lucide-react";

import Select from "@/components/ui/Select";
import DatePicker from "@/components/ui/DatePicker";
import TimePicker from "@/components/ui/TimePicker";
import DateTimePicker from "@/components/ui/DateTimePicker";
import type { MappingStageEntry, MappingStageKey, MappingStagePayload } from "@/lib/api";

import { MEDICAL_OPTIONS, PASSING_ANSWER, STAGE_META, STAMPING_OPTIONS } from "./stages";

interface Props {
  stage: MappingStageKey;
  /** The answer already on file, when the step is being changed. */
  entry?: MappingStageEntry | null;
  busy: boolean;
  onSave: (payload: MappingStagePayload) => void;
  /** Present when changing an answered step: puts the answer back as it was. */
  onCancel?: () => void;
}

/** An ISO instant as the picker's local `yyyy-mm-ddTHH:mm`. */
function toLocalInput(value?: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (part: number) => String(part).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

const CLOSING: Partial<Record<MappingStageKey, string>> = {
  selection: "rejected",
  offer: "rejected",
};

export default function StageForm({ stage, entry, busy, onSave, onCancel }: Props) {
  const meta = STAGE_META[stage];
  const fieldId = useId();
  const prior = entry && !entry.skipped ? entry : null;

  const [answer, setAnswer] = useState(prior?.answer ?? "");
  const [interviewAt, setInterviewAt] = useState(toLocalInput(prior?.interview_at));
  const [stamping, setStamping] = useState<string>(prior?.stamping ?? "");
  const [medical, setMedical] = useState<string>(prior?.medical ?? "");
  const [travelDate, setTravelDate] = useState(prior?.travel_date ?? "");
  const [travelTime, setTravelTime] = useState(prior?.travel_time ?? "");
  const [flight, setFlight] = useState(prior?.flight_number ?? "");
  const [airline, setAirline] = useState(prior?.airline ?? "");
  const [notes, setNotes] = useState(prior?.notes ?? "");
  const [error, setError] = useState("");

  const closes = Boolean(answer) && CLOSING[stage] === answer;
  const opensNext =
    stage === "visa"
      ? stamping === "completed" && medical === "fit"
      : Boolean(answer) && PASSING_ANSWER[stage] === answer;

  const submit = () => {
    const payload: MappingStagePayload = { notes: notes.trim() };
    if (stage === "visa") {
      if (!stamping) return setError("Say whether visa stamping is completed.");
      if (!medical) return setError("Choose the medical result.");
      Object.assign(payload, { stamping, medical });
    } else {
      if (!answer) return setError(`Choose an answer for “${meta.title}”.`);
      payload.answer = answer;
      if (stage === "interview" && answer === "scheduled") {
        if (!interviewAt) return setError("Choose the interview date and time.");
        payload.interview_at = new Date(interviewAt).toISOString();
      }
      if (stage === "travel" && answer === "travelled") {
        if (!travelDate) return setError("Choose the travel date.");
        if (!/^\d{2}:\d{2}$/.test(travelTime)) return setError("Choose the flight's departure time.");
        if (!/^[A-Za-z0-9][A-Za-z0-9 -]{1,19}$/.test(flight.trim())) {
          return setError("Enter the flight number, for example EK 543.");
        }
        Object.assign(payload, {
          travel_date: travelDate,
          travel_time: travelTime,
          flight_number: flight.trim().toUpperCase(),
          airline: airline.trim(),
        });
      }
    }
    setError("");
    onSave(payload);
  };

  return (
    <form
      className="map-form"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <p className="map-form-question">{meta.question}</p>

      {stage === "visa" ? (
        <div className="map-form-grid">
          <div className="map-field">
            <label className="map-label" htmlFor={`${fieldId}-stamping`}>Visa stamping</label>
            <Select
              id={`${fieldId}-stamping`}
              value={stamping}
              options={STAMPING_OPTIONS}
              onChange={setStamping}
              placeholder="Is the visa stamped?"
              disabled={busy}
            />
          </div>
          <div className="map-field">
            <label className="map-label" htmlFor={`${fieldId}-medical`}>Medical</label>
            <Select
              id={`${fieldId}-medical`}
              value={medical}
              options={MEDICAL_OPTIONS}
              onChange={setMedical}
              placeholder="Fit or unfit?"
              disabled={busy}
            />
          </div>
        </div>
      ) : (
        <div className="map-form-grid">
          <div className="map-field">
            <label className="map-label" htmlFor={`${fieldId}-answer`}>Answer</label>
            <Select
              id={`${fieldId}-answer`}
              value={answer}
              options={meta.options ?? []}
              onChange={setAnswer}
              placeholder="Choose an answer"
              disabled={busy}
            />
          </div>

          {stage === "interview" && answer === "scheduled" && (
            <div className="map-field is-wide">
              <span className="map-label">Interview date and time</span>
              <DateTimePicker value={interviewAt} onChange={setInterviewAt} ariaLabel="Interview" disabled={busy} />
            </div>
          )}

          {stage === "travel" && answer === "travelled" && (
            <>
              <div className="map-field">
                <label className="map-label" htmlFor={`${fieldId}-date`}>Travel date</label>
                <DatePicker id={`${fieldId}-date`} value={travelDate} onChange={setTravelDate} disabled={busy} ariaLabel="Travel date" />
              </div>
              <div className="map-field">
                <span className="map-label">Departure time</span>
                <TimePicker value={travelTime} onChange={setTravelTime} disabled={busy} ariaLabel="Departure time" />
              </div>
              <div className="map-field">
                <label className="map-label" htmlFor={`${fieldId}-flight`}>Flight number</label>
                <input
                  id={`${fieldId}-flight`}
                  className="map-input is-code"
                  value={flight}
                  onChange={(event) => setFlight(event.target.value)}
                  placeholder="EK 543"
                  maxLength={20}
                  autoComplete="off"
                  disabled={busy}
                />
              </div>
              <div className="map-field">
                <label className="map-label" htmlFor={`${fieldId}-airline`}>
                  Airline <span className="map-optional">optional</span>
                </label>
                <input
                  id={`${fieldId}-airline`}
                  className="map-input"
                  value={airline}
                  onChange={(event) => setAirline(event.target.value)}
                  placeholder="Emirates"
                  maxLength={80}
                  disabled={busy}
                />
              </div>
            </>
          )}
        </div>
      )}

      <div className="map-field">
        <label className="map-label" htmlFor={`${fieldId}-notes`}>
          Notes <span className="map-optional">optional</span>
        </label>
        <textarea
          id={`${fieldId}-notes`}
          className="map-input map-textarea"
          rows={2}
          maxLength={1000}
          value={notes}
          onChange={(event) => setNotes(event.target.value)}
          placeholder="Anything the next person should know"
          disabled={busy}
        />
      </div>

      {closes && (
        <p className="map-callout is-bad" role="status">
          <AlertTriangle size={15} aria-hidden="true" />
          <span>
            This closes the mapping. The candidate returns to the available pool and can be mapped to
            another job order; this one stays on record as closed.
          </span>
        </p>
      )}
      {!closes && (answer || stage === "visa") && !opensNext && (stamping || medical || answer) && (
        <p className="map-callout" role="status">
          <Lock size={14} aria-hidden="true" />
          <span>
            {stage === "visa" && medical === "unfit"
              ? "A medically unfit result holds travel. Update it when the result changes; a manager or admin can skip the step."
              : "Saving this keeps the step open — the next step unlocks once the answer is yes."}
          </span>
        </p>
      )}

      {error && <p className="map-form-error" role="alert">{error}</p>}

      <div className="map-form-actions">
        {onCancel && (
          <button type="button" className="ds-ghost-btn" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
        )}
        <button type="submit" className={`ds-primary-btn ${closes ? "is-danger" : ""}`} disabled={busy}>
          {busy ? <Loader2 size={15} className="icon-spin" /> : <Check size={15} />}
          {busy
            ? "Saving…"
            : closes
              ? "Save and close mapping"
              : opensNext
                ? stage === "travel" ? "Save and complete" : "Save and open next step"
                : "Save"}
        </button>
      </div>
    </form>
  );
}
