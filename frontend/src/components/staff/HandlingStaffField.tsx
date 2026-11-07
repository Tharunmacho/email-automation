"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import Select from "@/components/ui/Select";
import { listJobOrderAssigneesAPI } from "@/lib/api";
import type { StaffContact } from "@/types";

/** "Zara — +91 98849 49735", or the name alone when no number is recorded. */
export function staffLabel(staff: Pick<StaffContact, "name" | "phone">): string {
  return staff.phone ? `${staff.name} — ${staff.phone}` : `${staff.name} — no number on file`;
}

/**
 * The staff members an agent can be handled by: active staff and managers who
 * sign in to the CRM. Loaded once per mount; `people` is null while loading.
 */
export function useAssignableStaff() {
  const [people, setPeople] = useState<StaffContact[] | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    listJobOrderAssigneesAPI()
      .then(({ items }) => {
        if (!active) return;
        setPeople(items.map(({ id, name, phone }) => ({ id, name, phone })));
        setError("");
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Could not load the staff list.");
      });
    return () => {
      active = false;
    };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { people, error, retry };
}

/**
 * Dropdown options for one record. Someone assigned earlier who is no longer
 * on the list (deactivated, say) still shows as the current choice rather
 * than as a blank.
 */
export function staffOptions(people: StaffContact[] | null, current?: StaffContact) {
  const list = people ?? [];
  const all = current?.id && !list.some((person) => person.id === current.id) ? [current, ...list] : list;
  return all.map((person) => ({ value: person.id, label: staffLabel(person) }));
}

/** The staff member with this id, from the list or the record's own copy. */
export function pickStaff(
  people: StaffContact[] | null,
  staffId: string,
  current?: StaffContact,
): StaffContact | undefined {
  const person = (people ?? []).find((item) => item.id === staffId) ?? (current?.id === staffId ? current : undefined);
  return person ? { ...person } : undefined;
}

interface HandlingStaffFieldProps {
  value?: StaffContact;
  onChange: (next: StaffContact | undefined) => void;
  /** A new agent cannot be created without one. */
  required?: boolean;
  id: string;
}

/**
 * "Handling staff": the staff member who handles a Sourcing Hub agent, picked
 * by name and phone number. Their name and number are the contact on every
 * job order the agent is sent on WhatsApp.
 */
export default function HandlingStaffField({ value, onChange, required = false, id }: HandlingStaffFieldProps) {
  const { people, error, retry } = useAssignableStaff();
  const options = useMemo(() => staffOptions(people, value), [people, value]);

  return (
    <div className="sh-field">
      <span className="modal-label" id={`${id}-label`}>
        Handling staff{required && " *"}
      </span>
      <Select
        id={id}
        value={value?.id ?? ""}
        options={options}
        onChange={(staffId) => onChange(pickStaff(people, staffId, value))}
        placeholder={people === null && !error ? "Loading staff…" : "Select staff — name and number"}
        ariaLabel={required ? "Handling staff (required)" : "Handling staff"}
      />
      {error ? (
        <small className="sh-field-hint is-warn" role="alert">
          {error}{" "}
          <button type="button" className="sh-inline-retry" onClick={retry}>
            Try again
          </button>
        </small>
      ) : people !== null && people.length === 0 ? (
        <small className="sh-field-hint is-warn">
          No active staff to assign. Add staff in the Staff Directory first.
        </small>
      ) : (
        <small className={`sh-field-hint${value ? "" : " is-warn"}`}>
          {value
            ? "Job orders sent to this agent on WhatsApp give this person’s name and number to contact."
            : "No staff member yet: job orders sent to this agent will say to contact Adira Enterprises on this WhatsApp number."}
        </small>
      )}
    </div>
  );
}
