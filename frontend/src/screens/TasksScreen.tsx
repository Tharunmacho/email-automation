"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlarmClock,
  CalendarClock,
  CheckCircle2,
  ListChecks,
  Loader2,
  Phone,
  RefreshCw,
  Search,
  TriangleAlert,
  UserCheck,
} from "lucide-react";

import Select from "@/components/ui/Select";
import { listTasksAPI, type CallbackTask, type CallbackTasksResponse } from "@/lib/api";
import { initialsOf, timeAgo } from "@/lib/format";

/**
 * The follow-up list: every candidate somebody rang who did not pick up.
 *
 * One screen, read two ways. A staff member opens it to see what they owe
 * today; an admin opens the same screen and sees every desk, which is why the
 * owner column and the per-staff breakdown appear only for them rather than
 * being a second screen that would have to be kept in step with this one.
 *
 * Nothing is ticked off here. A callback is cleared by recording the next
 * verdict on the candidate, so every row's action is to open that candidate —
 * a checkbox would let the list disagree with the record it describes.
 */

type StateKey = CallbackTask["state"];

interface TasksScreenProps {
  /** Open a candidate's own screen. Absent when there is nowhere to go. */
  onOpenCandidate?: (candidateId: string) => void;
  /** How often to re-read, in milliseconds. */
  refreshMs?: number;
  /**
   * Bumped by the shell when something may have changed what is outstanding —
   * a verdict saved elsewhere, most of all. A candidate whose verdict is no
   * longer "callback" is no longer a task, and this is how that row leaves the
   * screen at the moment it stops being true rather than at the next poll.
   */
  refreshNonce?: number;
}

const STATE_META: Record<StateKey, { label: string; tone: string; blurb: string }> = {
  overdue: {
    label: "Overdue",
    tone: "bad",
    blurb: "Marked more than 48 hours ago and still not called back.",
  },
  due: {
    label: "Due now",
    tone: "warn",
    blurb: "The 24-hour wait has passed — these are ready to call.",
  },
  scheduled: {
    label: "Scheduled",
    tone: "info",
    blurb: "Called recently. They come due 24 hours after the verdict.",
  },
};

const FILTERS: { id: "all" | StateKey; label: string }[] = [
  { id: "all", label: "All tasks" },
  { id: "overdue", label: "Overdue" },
  { id: "due", label: "Due now" },
  { id: "scheduled", label: "Scheduled" },
];

/** "3d 4h", "6h", "40m" — the shape a person reads a waiting time in. */
function formatSpan(hours: number): string {
  const total = Math.max(0, hours);
  if (total < 1) return `${Math.max(1, Math.round(total * 60))}m`;
  if (total < 24) return `${Math.round(total)}h`;
  const days = Math.floor(total / 24);
  const rest = Math.round(total % 24);
  return rest ? `${days}d ${rest}h` : `${days}d`;
}

export default function TasksScreen({
  onOpenCandidate,
  refreshMs = 60_000,
  refreshNonce = 0,
}: TasksScreenProps) {
  const [data, setData] = useState<CallbackTasksResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | StateKey>("all");
  const [owner, setOwner] = useState("ALL");
  const [query, setQuery] = useState("");

  /**
   * Bumped to ask for a fresh read. The fetch lives in an effect keyed on this
   * rather than in a callback the timer and the button both await, so a reload
   * in flight is cancelled by the cleanup when the screen closes.
   */
  const [reloadKey, setReloadKey] = useState(0);
  const reload = useCallback(() => {
    setRefreshing(true);
    setReloadKey((key) => key + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;
    listTasksAPI()
      .then((response) => {
        if (cancelled) return;
        setData(response);
        setError(null);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        setError(reason instanceof Error ? reason.message : "Could not load tasks.");
      })
      .finally(() => {
        if (cancelled) return;
        setLoading(false);
        setRefreshing(false);
      });
    return () => { cancelled = true; };
  }, [reloadKey, refreshNonce]);

  // Re-read on a timer, because a task becomes due and then overdue while the
  // screen is sitting open — the states are the whole point of the list, and a
  // list that only tells the truth at the moment it was opened is worse than
  // one that admits it is stale.
  useEffect(() => {
    if (!refreshMs) return;
    const timer = window.setInterval(reload, refreshMs);
    return () => window.clearInterval(timer);
  }, [reload, refreshMs]);

  const items = useMemo(() => data?.items ?? [], [data]);
  const isAdminView = data?.scope === "all";

  /** The staff members who actually have work outstanding, for the picker. */
  const owners = useMemo(() => {
    const seen = new Map<string, string>();
    for (const task of items) {
      if (task.staff_id) seen.set(task.staff_id, task.staff_name || "Unnamed staff");
    }
    return [...seen.entries()].sort((a, b) => a[1].localeCompare(b[1]));
  }, [items]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return items.filter((task) => {
      if (filter !== "all" && task.state !== filter) return false;
      if (owner !== "ALL" && task.staff_id !== owner) return false;
      if (!needle) return true;
      return [
        task.candidate_name,
        task.candidate_code,
        task.phone,
        task.email,
        task.job_title,
        task.remarks,
        task.staff_name,
      ]
        .filter(Boolean)
        .some((field) => field.toLowerCase().includes(needle));
    });
  }, [items, filter, owner, query]);

  /** Who is sitting on what — the admin's reason for opening this screen. */
  const byOwner = useMemo(() => {
    if (!isAdminView) return [];
    const rows = new Map<string, { name: string; total: number; overdue: number; due: number }>();
    for (const task of items) {
      const key = task.staff_id || "unassigned";
      const row = rows.get(key) ?? {
        name: task.staff_name || "Unallocated",
        total: 0,
        overdue: 0,
        due: 0,
      };
      row.total += 1;
      if (task.state === "overdue") row.overdue += 1;
      if (task.state === "due") row.due += 1;
      rows.set(key, row);
    }
    return [...rows.entries()]
      // Worst desk first: the list is read top-down and the person furthest
      // behind is the one a supervisor is looking for.
      .sort((a, b) => b[1].overdue - a[1].overdue || b[1].total - a[1].total)
      .map(([id, row]) => ({ id, ...row }));
  }, [items, isAdminView]);

  const counts = data?.counts ?? { total: 0, overdue: 0, due: 0, scheduled: 0 };

  if (loading) {
    return (
      <div className="ds-page">
        <div className="ds-empty-state">
          <Loader2 size={28} className="icon-spin" />
          <h3>Loading tasks</h3>
          <p>Reading the callbacks outstanding on your candidates.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="ds-page tasks-page">
      {error && <div className="cscreen-error" role="alert">{error}</div>}

      {/* ── The four readings, which are also the filter ───────────────── */}
      <div className="ds-stats" role="tablist" aria-label="Filter tasks by state">
        {FILTERS.map(({ id, label }) => {
          const value = id === "all" ? counts.total : counts[id];
          const alert = id === "overdue" && counts.overdue > 0;
          const Icon =
            id === "all" ? ListChecks : id === "overdue" ? TriangleAlert : id === "due" ? AlarmClock : CalendarClock;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={filter === id}
              className={`ds-stat ${filter === id ? "is-on" : ""}`}
              onClick={() => setFilter(id)}
            >
              <span className="ds-stat-top">
                <span className="ds-stat-label">{label}</span>
                <span className={`ds-stat-icon ${alert ? "is-alert" : ""}`} aria-hidden="true">
                  <Icon size={16} strokeWidth={2} />
                </span>
              </span>
              <span className={`ds-stat-value ${alert ? "is-alert" : ""}`}>{value}</span>
              <span className="ds-stat-foot">
                {id === "all"
                  ? isAdminView
                    ? "Across every staff member"
                    : "On candidates allocated to you"
                  : STATE_META[id].blurb}
              </span>
            </button>
          );
        })}
      </div>

      {/* ── Who is sitting on what. Admin only: a staff member is the only
             person on their own list, so the breakdown would be one row
             repeating the numbers directly above it. ─────────────────── */}
      {isAdminView && byOwner.length > 0 && (
        <section className="ds-panel tasks-owners">
          <div className="ds-panel-head is-split">
            <div>
              <h2 className="ds-panel-title">By staff member</h2>
              <p className="ds-panel-sub">Most overdue first. Click a row to see only their tasks.</p>
            </div>
          </div>
          <div className="tasks-owner-grid">
            {byOwner.map((row) => (
              <button
                key={row.id}
                type="button"
                className={`tasks-owner ${owner === row.id ? "is-on" : ""}`}
                aria-pressed={owner === row.id}
                onClick={() => setOwner((current) => (current === row.id ? "ALL" : row.id))}
              >
                <span className="ds-avatar" aria-hidden="true">{initialsOf(row.name)}</span>
                <span className="tasks-owner-text">
                  <strong>{row.name}</strong>
                  <small>
                    {row.total} task{row.total === 1 ? "" : "s"}
                    {row.due > 0 && ` · ${row.due} due`}
                  </small>
                </span>
                {row.overdue > 0 ? (
                  <span className="tasks-owner-flag is-bad">{row.overdue} overdue</span>
                ) : (
                  <span className="tasks-owner-flag">
                    <CheckCircle2 size={13} /> On track
                  </span>
                )}
              </button>
            ))}
          </div>
        </section>
      )}

      {/* ── The tasks themselves ───────────────────────────────────────── */}
      <section className="ds-panel">
        <div className="ds-panel-head is-split">
          <div>
            <h2 className="ds-panel-title">
              {FILTERS.find((entry) => entry.id === filter)?.label ?? "All tasks"}
            </h2>
            <p className="ds-panel-sub">
              A callback clears when the next verdict is recorded on the candidate.
              {refreshing && " Refreshing…"}
            </p>
          </div>

          <div className="ds-panel-tools">
            <label className="ds-search">
              <Search size={15} />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search name, phone or remark"
                aria-label="Search tasks"
              />
            </label>
            {isAdminView && owners.length > 0 && (
              <Select
                className="ds-select-control"
                size="sm"
                value={owner}
                options={[
                  { value: "ALL", label: "All staff" },
                  ...owners.map(([id, name]) => ({ value: id, label: name })),
                ]}
                onChange={setOwner}
                ariaLabel="Filter by staff member"
              />
            )}
            <button
              type="button"
              className="ds-ghost-btn is-sm"
              onClick={reload}
              disabled={refreshing}
            >
              {refreshing ? <Loader2 size={14} className="icon-spin" /> : <RefreshCw size={14} />}
              Refresh
            </button>
          </div>
        </div>

        {visible.length === 0 ? (
          <div className="ds-empty-state">
            <CheckCircle2 size={30} />
            <h3>{items.length === 0 ? "No callbacks outstanding" : "Nothing in this view"}</h3>
            <p>
              {items.length === 0
                ? "A task appears here the moment a candidate is marked for a callback."
                : "Clear the search or pick a different state to see the rest."}
            </p>
          </div>
        ) : (
          <div className="ds-table-wrap is-ruled">
            <table className="ds-table is-ruled">
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Contact</th>
                  <th>Why it is open</th>
                  <th>Waiting</th>
                  <th>State</th>
                  {isAdminView && <th>Owner</th>}
                  <th className="is-actions" aria-label="Actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((task) => {
                  const meta = STATE_META[task.state];
                  return (
                    <tr
                      key={task.candidate_id}
                      className={onOpenCandidate ? "is-clickable" : ""}
                      onClick={onOpenCandidate ? () => onOpenCandidate(task.candidate_id) : undefined}
                    >
                      <td>
                        <span className="ds-who">
                          <span className="ds-avatar" aria-hidden="true">
                            {initialsOf(task.candidate_name)}
                          </span>
                          <span className="ds-who-text">
                            <strong title={task.candidate_name}>{task.candidate_name}</strong>
                            {task.candidate_code && (
                              <small className="crm-record-id">Candidate ID · {task.candidate_code}</small>
                            )}
                            <small>
                              {[task.job_title, task.destination_country].filter(Boolean).join(" · ") ||
                                "No job preference recorded"}
                            </small>
                          </span>
                        </span>
                      </td>

                      {/* The number is the task. It is a link rather than text
                          so the call can be placed from the row on a phone. */}
                      <td onClick={(event) => event.stopPropagation()}>
                        {task.phone ? (
                          <a className="tasks-phone" href={`tel:${task.phone.replace(/\s+/g, "")}`}>
                            <Phone size={12} />
                            {task.phone}
                          </a>
                        ) : (
                          <span className="ds-quiet">No number on file</span>
                        )}
                        {task.email && <small className="tasks-sub">{task.email}</small>}
                      </td>

                      <td className="is-wrap">
                        {task.remarks ? (
                          <span className="tasks-remark" title={task.remarks}>{task.remarks}</span>
                        ) : (
                          <span className="ds-quiet">No remark was left</span>
                        )}
                      </td>

                      <td>
                        <span className={`ds-sla ${task.state === "overdue" ? "is-over" : task.state === "due" ? "is-soon" : ""}`}>
                          <AlarmClock size={12} />
                          {task.age_hours === null ? "Unknown" : formatSpan(task.age_hours)}
                        </span>
                        <small className="tasks-sub">
                          {task.marked_at ? `Marked ${timeAgo(task.marked_at)}` : "No timestamp"}
                        </small>
                      </td>

                      <td>
                        <span className={`ds-status is-${meta.tone}`}>
                          <i aria-hidden="true" />
                          {meta.label}
                        </span>
                      </td>

                      {isAdminView && (
                        <td>
                          {task.staff_name ? (
                            <span className="tasks-owner-cell">
                              <UserCheck size={13} />
                              {task.staff_name}
                            </span>
                          ) : (
                            <span className="ds-quiet">Unallocated</span>
                          )}
                        </td>
                      )}

                      <td className="is-actions" onClick={(event) => event.stopPropagation()}>
                        <div className="ds-acts">
                          {onOpenCandidate && (
                            <button
                              type="button"
                              className="ds-act"
                              title={`Open ${task.candidate_name}'s profile`}
                              onClick={() => onOpenCandidate(task.candidate_id)}
                            >
                              <UserCheck size={14} />
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
