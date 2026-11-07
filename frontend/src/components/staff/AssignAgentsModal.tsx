"use client";

import { useMemo, useState } from "react";
import { AlertTriangle, Search, UserCog, X } from "lucide-react";

import Select from "@/components/ui/Select";
import { useModalFocus } from "@/components/ui/useModalFocus";
import { assignAgentsHandlingStaffAPI } from "@/lib/api";
import type { SourcingClientRecord } from "@/types";
import { staffOptions, useAssignableStaff } from "./HandlingStaffField";

/** "N/A" is how the Sourcing Hub stores a blank phone. */
function shown(value: string | undefined): string {
  const text = (value || "").trim();
  return text && text.toUpperCase() !== "N/A" ? text : "";
}

/** The agent's primary contact, from `contacts` or the older top-level fields. */
function primaryContact(agent: SourcingClientRecord): string {
  const first = agent.contacts?.[0];
  const name = shown(first?.name ?? agent.contact);
  const phone = shown(first?.phone ?? agent.phone);
  return [name, phone].filter(Boolean).join(" · ") || "No contact on file";
}

interface AssignAgentsModalProps {
  /** Every Sourcing Hub agent. */
  agents: SourcingClientRecord[];
  onClose: () => void;
  /** The agents as saved; the parent merges them in and closes. */
  onSaved: (updated: SourcingClientRecord[]) => void;
}

/**
 * Assign agents to the staff members who handle them, all in one window.
 *
 * Every agent has one staff member, whose name and number go on the job orders
 * the agent is sent on WhatsApp. Agents with nobody yet are listed first, and
 * only the rows that were changed are saved.
 */
export default function AssignAgentsModal({ agents, onClose, onSaved }: AssignAgentsModalProps) {
  const { people, error: staffError, retry } = useAssignableStaff();
  const assignedId = (agent: SourcingClientRecord) => agent.handlingStaff?.id ?? "";

  const [draft, setDraft] = useState<Record<string, string>>(
    () => Object.fromEntries(agents.map((agent) => [agent.id, assignedId(agent)])),
  );
  const [query, setQuery] = useState("");
  const unassignedCount = agents.filter((agent) => !assignedId(agent)).length;
  // Opens on the agents that still need someone, when there are any.
  const [unassignedOnly, setUnassignedOnly] = useState(unassignedCount > 0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  const close = () => {
    if (!saving) onClose();
  };
  const dialogRef = useModalFocus<HTMLDivElement>(true, close);

  const changed = agents.filter((agent) => draft[agent.id] && draft[agent.id] !== assignedId(agent));

  // Filtered on what is saved, not on the draft, so a row does not vanish
  // from "Unassigned only" the moment someone is picked for it.
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    return agents
      .filter((agent) => !unassignedOnly || !assignedId(agent))
      .filter((agent) => !q || [
        agent.name,
        primaryContact(agent),
        agent.handlingStaff?.name ?? "",
      ].some((text) => text.toLowerCase().includes(q)))
      .sort((a, b) =>
        Number(Boolean(assignedId(a))) - Number(Boolean(assignedId(b))) || a.name.localeCompare(b.name),
      );
  }, [agents, query, unassignedOnly]);

  const save = async () => {
    if (!changed.length) return;
    setSaving(true);
    setSaveError("");
    try {
      const { updated } = await assignAgentsHandlingStaffAPI(
        changed.map((agent) => ({ agent_id: agent.id, staff_id: draft[agent.id] })),
      );
      onSaved(updated);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Could not save the assignments. Try again.");
      setSaving(false);
    }
  };

  return (
    <div className="cm-overlay active" onClick={close}>
      <div
        ref={dialogRef}
        className="cm-dialog sh-modal sh-assign-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="sh-assign-title"
        tabIndex={-1}
      >
        <div className="sh-modal-head">
          <div>
            <h2 className="sh-modal-title" id="sh-assign-title">Assign agents to staff</h2>
            <p className="sh-modal-sub">
              Every agent is handled by one staff member. Job orders sent to an agent on WhatsApp give
              that person&rsquo;s name and number to contact.
            </p>
          </div>
          <button type="button" className="sh-modal-close" onClick={close} aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <div className="modal-body sh-modal-body sh-assign-body">
          <div className="sh-assign-tools">
            <div className="sh-search">
              <Search size={16} className="sh-search-icon" />
              <input
                type="text"
                className="sh-search-input"
                placeholder="Search agent, contact or staff…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Search agents"
              />
            </div>
            <button
              type="button"
              className={`sh-toggle ${unassignedOnly ? "active" : ""}`}
              onClick={() => setUnassignedOnly((prev) => !prev)}
              aria-pressed={unassignedOnly}
            >
              Unassigned only
            </button>
            <span className={`sh-assign-summary${unassignedCount ? " is-warn" : ""}`}>
              {unassignedCount
                ? `${unassignedCount} of ${agents.length} agent${agents.length === 1 ? "" : "s"} without staff`
                : `All ${agents.length} agent${agents.length === 1 ? "" : "s"} assigned`}
            </span>
          </div>

          {staffError && (
            <p className="sh-form-error" role="alert">
              <AlertTriangle size={14} />
              <span>
                {staffError}{" "}
                <button type="button" className="sh-inline-retry" onClick={retry}>Try again</button>
              </span>
            </p>
          )}
          {!staffError && people !== null && people.length === 0 && (
            <p className="sh-form-error" role="alert">
              <AlertTriangle size={14} />
              No active staff to assign. Add staff in the Staff Directory first.
            </p>
          )}

          {rows.length === 0 ? (
            <p className="sh-assign-empty">
              {agents.length === 0
                ? "No agents yet. Add one with New client and choose their staff member there."
                : unassignedOnly && !query.trim()
                  ? "Every agent has a staff member."
                  : "No agent matches this search."}
            </p>
          ) : (
            <ul className="sh-assign-list" aria-label="Agents">
              {rows.map((agent) => {
                const isChanged = Boolean(draft[agent.id]) && draft[agent.id] !== assignedId(agent);
                return (
                  <li key={agent.id} className={`sh-assign-row${isChanged ? " is-changed" : ""}`}>
                    <span className="sh-assign-agent">
                      <strong title={agent.name}>{agent.name}</strong>
                      <small>
                        {primaryContact(agent)}
                        {agent.status !== "ACTIVE" && ` · ${agent.status === "PENDING" ? "Pending" : "Inactive"}`}
                      </small>
                    </span>
                    <Select
                      size="sm"
                      value={draft[agent.id] ?? ""}
                      options={staffOptions(people, agent.handlingStaff)}
                      onChange={(staffId) => setDraft((prev) => ({ ...prev, [agent.id]: staffId }))}
                      placeholder={people === null && !staffError ? "Loading staff…" : "Select staff"}
                      ariaLabel={`Staff handling ${agent.name}`}
                      disabled={saving}
                    />
                  </li>
                );
              })}
            </ul>
          )}

          {saveError && (
            <p className="sh-form-error" role="alert">
              <AlertTriangle size={14} />
              {saveError}
            </p>
          )}
        </div>

        <div className="modal-footer sh-assign-footer">
          <span className="sh-assign-count">
            <UserCog size={14} />
            {changed.length ? `${changed.length} change${changed.length === 1 ? "" : "s"} to save` : "No changes yet"}
          </span>
          <button type="button" className="modal-cancel-btn" onClick={close} disabled={saving}>
            Cancel
          </button>
          <button
            type="button"
            className="modal-submit-btn"
            onClick={save}
            disabled={!changed.length || saving}
          >
            {saving ? "Saving…" : "Save assignments"}
          </button>
        </div>
      </div>
    </div>
  );
}
