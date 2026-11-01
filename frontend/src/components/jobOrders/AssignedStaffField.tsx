"use client";

import { useEffect, useMemo, useState } from "react";

import Select from "@/components/ui/Select";
import { listJobOrderAssigneesAPI } from "@/lib/api";
import type { JobOrderStaff } from "@/types";

/** "Zara — +91 98849 49735", or the name alone when no number is recorded. */
export function staffLabel(staff: Pick<JobOrderStaff, "name" | "phone">): string {
  return staff.phone ? `${staff.name} — ${staff.phone}` : `${staff.name} — no number on file`;
}

interface AssignedStaffFieldProps {
  value?: JobOrderStaff;
  onChange: (next: JobOrderStaff | undefined) => void;
  /** A new order cannot be created without one. */
  required?: boolean;
  /** Distinguishes the create and edit dialogs' field ids. */
  id: string;
}

/**
 * "Assigned staff": the staff member responsible for the job order, picked by
 * name and phone number from the active staff and managers.
 */
export default function AssignedStaffField({ value, onChange, required = false, id }: AssignedStaffFieldProps) {
  const [people, setPeople] = useState<JobOrderStaff[] | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    listJobOrderAssigneesAPI()
      .then(({ items }) => {
        if (!active) return;
        setPeople(items.map(({ id: staffId, name, phone }) => ({ id: staffId, name, phone })));
        setError("");
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "Could not load the staff list.");
      });
    return () => {
      active = false;
    };
  }, [attempt]);

  // Someone assigned earlier who is no longer on the list (deactivated, say)
  // still shows as the current choice rather than as a blank.
  const options = useMemo(() => {
    const list = people ?? [];
    const all = value && !list.some((person) => person.id === value.id) ? [value, ...list] : list;
    return all.map((person) => ({ value: person.id, label: staffLabel(person) }));
  }, [people, value]);

  const pick = (staffId: string) => {
    const person = (people ?? []).find((item) => item.id === staffId) ?? (value?.id === staffId ? value : undefined);
    onChange(person ? { ...person } : undefined);
  };

  return (
    <div>
      <span className="modal-label" id={`${id}-label`}>
        Assigned staff{required && <span className="jo-required" aria-hidden="true">*</span>}
      </span>
      <Select
        value={value?.id ?? ""}
        options={options}
        onChange={pick}
        placeholder={people === null && !error ? "Loading staff…" : "Select staff — name and number"}
        ariaLabel={required ? "Assigned staff (required)" : "Assigned staff"}
      />
      {error && (
        <span className="jo-web-error" role="alert">
          {error}{" "}
          <button type="button" className="jo-inline-retry" onClick={() => setAttempt((n) => n + 1)}>
            Try again
          </button>
        </span>
      )}
      {!error && people !== null && people.length === 0 && (
        <span className="jo-web-error">No active staff to assign. Add staff in the Staff Directory first.</span>
      )}
    </div>
  );
}
