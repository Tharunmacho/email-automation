"use client";

/**
 * The Super Admin's console: who is on the team, what each of them is holding,
 * and which profiles have gone quiet.
 *
 * Built on the Overview's layout, block for block, because it is the same kind
 * of screen and an admin moves between the two constantly: three things to do,
 * three numbers that say whether the team is keeping up, then the detail. The
 * markup is the Overview's own — `ov-action`, `ov-kpi`, `db-card` — so the two
 * screens cannot drift apart the next time either is touched.
 *
 * The order is the order the questions get asked. The tiles say whether the
 * team as a whole is keeping up. The matrix says who is not. The directory is
 * where a profile gets moved. Nothing here is a mode — every block stays on
 * screen, because the answer to "who should this go to?" is the row directly
 * above the dropdown you are about to use.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  Clock,
  Inbox,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  Scale,
  Search,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
  Wand2,
  X,
} from "lucide-react";

import { compactNumber, formatInt, initialsOf, timeAgo } from "@/lib/format";
import {
  assignCandidate,
  autoAssignCandidate,
  createStaff,
  deleteStaff,
  fetchSlaBreaches,
  fetchStaffWorkload,
  rebalanceCandidates,
  rehomeOrphans,
  runSlaScan,
  updateStaff,
  type CandidateRecord,
  type SlaAlert,
  type StaffWorkloadResponse,
  type StaffWorkloadRow,
} from "@/lib/api";

interface AdminStaffManagementProps {
  candidates: CandidateRecord[];
  /** Bumped by the parent when a WebSocket event says the data moved. */
  refreshNonce: number;
  onToast: (message: string, type?: "info" | "success" | "error") => void;
  onCandidatesChanged: () => void;
  onOpenCandidate: (candidate: CandidateRecord) => void;
}

const EMPTY_FORM = { email: "", password: "", name: "" };

/** The directory is the long block; it opens on one page and grows on request. */
const PAGE_SIZE = 25;

type AllocFilter = "all" | "unallocated" | "orphaned" | "unviewed" | "overdue" | "evaluated";

const ALLOC_FILTERS: { id: AllocFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "unallocated", label: "Unallocated" },
  { id: "orphaned", label: "Orphaned" },
  { id: "unviewed", label: "Unviewed" },
  { id: "overdue", label: "Overdue" },
  { id: "evaluated", label: "Evaluated" },
];

export default function AdminStaffManagement({
  candidates,
  refreshNonce,
  onToast,
  onCandidatesChanged,
  onOpenCandidate,
}: AdminStaffManagementProps) {
  const [workload, setWorkload] = useState<StaffWorkloadResponse | null>(null);
  const [breaches, setBreaches] = useState<SlaAlert[]>([]);
  // Replaced by the real value the moment /sla/breaches answers.
  const [thresholdHours, setThresholdHours] = useState(24);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [creating, setCreating] = useState(false);
  /**
   * Whether the roster box is showing the summary or everything.
   *
   * Summary by default, matching My Candidates: this screen answers "is the
   * work spread evenly and is anything late" before it answers "show me every
   * account, every breach and every allocation".
   */
  const [showAll, setShowAll] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [submitting, setSubmitting] = useState(false);
  const [rebalancing, setRebalancing] = useState(false);
  const [rehoming, setRehoming] = useState(false);
  const [scanning, setScanning] = useState(false);
  /** Which candidate row is mid-move, so only its own controls lock. */
  const [movingId, setMovingId] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<AllocFilter>("all");
  const [visible, setVisible] = useState(PAGE_SIZE);

  // Reloading is a request, not a call: bumping this re-runs the one effect
  // below, so every refresh — a push event, a create, a rebalance — goes
  // through the same path and gets the same cancellation.
  const [reloadToken, setReloadToken] = useState(0);
  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const [matrix, sla] = await Promise.all([fetchStaffWorkload(), fetchSlaBreaches()]);
        // A reply that lands after the screen moved on is discarded rather
        // than written: two refreshes in flight together would otherwise let
        // the slower, older one overwrite the newer matrix.
        if (cancelled) return;
        setWorkload(matrix);
        setBreaches(sla.items);
        setThresholdHours(sla.threshold_hours);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Could not load the staff matrix.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [refreshNonce, reloadToken]);

  // Narrowing the directory takes you back to its first page; keeping the old
  // offset would open a two-result filter on an empty page.
  useEffect(() => setVisible(PAGE_SIZE), [filter, query]);

  const staff = useMemo(() => workload?.items ?? [], [workload]);
  const activeStaff = useMemo(() => staff.filter((member) => member.active), [staff]);
  const totals = workload?.totals;

  const pool = (totals?.assigned ?? 0) + (totals?.unassigned ?? 0);
  const evaluatedPct =
    totals && totals.assigned > 0 ? Math.round((totals.evaluated / totals.assigned) * 100) : 0;

  /**
   * How far the roster is from level, and how much of the gap can be closed.
   *
   * A correctly balanced roster differs by at most one, so anything wider means
   * profiles arrived before an account existed, or the roster changed while the
   * API was not running the levelling code. Neither self-corrects: new intake
   * goes to whoever is behind, but a pile that is already sitting in the wrong
   * place stays there until a rebalance moves it.
   *
   * `movable` is what makes the banner honest — reviewed work is pinned to its
   * owner by design, so a spread made entirely of evaluated profiles is not a
   * problem and must not nag.
   */
  const imbalance = useMemo(() => {
    if (activeStaff.length < 2) return null;
    const loads = activeStaff.map((member) => member.assigned);
    const spread = Math.max(...loads) - Math.min(...loads);
    if (spread <= 1) return null;

    // Everything not yet opened or judged can still be moved.
    const movable = activeStaff.reduce(
      (total, member) => total + Math.min(member.assigned, member.unviewed),
      0,
    );
    if (movable === 0) return null;

    const busiest = activeStaff.reduce((a, b) => (a.assigned >= b.assigned ? a : b));
    const lightest = activeStaff.reduce((a, b) => (a.assigned <= b.assigned ? a : b));
    return { spread, busiest, lightest };
  }, [activeStaff]);

  /** Breaches per staff member, so the matrix can flag a row directly. */
  const breachesByStaff = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const alert of breaches) {
      const id = alert.assigned_staff_id;
      if (id) counts[id] = (counts[id] ?? 0) + 1;
    }
    return counts;
  }, [breaches]);

  /** Candidate ids the sweep is currently reporting, for the directory filter. */
  const overdueIds = useMemo(
    () => new Set(breaches.map((alert) => alert.candidate_id)),
    [breaches],
  );

  /**
   * Every account that still exists, deactivated ones included.
   *
   * The test for "orphaned" is membership of this, not of `staff` — a
   * deactivated colleague still owns their queue and those profiles are
   * perfectly visible to them. Falls back to the active roster only when an
   * older API build sends no `roster_ids`, which over-reports rather than
   * under-reports: a deactivated account's work would show as needing a home,
   * and re-homing it is harmless.
   */
  const rosterIds = useMemo(
    () => new Set(workload?.roster_ids ?? staff.map((member) => member.id)),
    [workload, staff],
  );

  const isOrphan = useCallback(
    (candidate: CandidateRecord) =>
      Boolean(candidate.assigned_staff_id) && !rosterIds.has(candidate.assigned_staff_id!),
    [rosterIds],
  );

  /** The heaviest active queue — the scale every load bar is drawn against. */
  const heaviest = useMemo(
    () => Math.max(1, ...staff.map((member) => member.assigned)),
    [staff],
  );

  const matches = useCallback(
    (candidate: CandidateRecord, id: AllocFilter) => {
      const status = candidate.evaluation_status ?? "pending";
      switch (id) {
        case "unallocated":
          return !candidate.assigned_staff_id;
        case "orphaned":
          return isOrphan(candidate);
        case "unviewed":
          return Boolean(candidate.assigned_staff_id) && !candidate.viewed_at;
        case "overdue":
          return overdueIds.has(candidate.id);
        case "evaluated":
          return status !== "pending";
        default:
          return true;
      }
    },
    [isOrphan, overdueIds],
  );


  /**
   * One ring per reviewer, sized by the share of the allocated pool they are
   * holding. This is a staff screen, so the chart answers "is the work spread
   * evenly" rather than "what state are the candidates in" — which is the
   * candidate screen's question and was being asked twice.
   *
   * Capped at four: a fifth arc lands inside a radius too small to read, and
   * the roster below is where a full list belongs. The remainder is counted
   * rather than dropped silently.
   */
  const staffSegments = useMemo(() => {
    const tones = [
      "var(--primary)",
      "var(--success)",
      "var(--warning)",
      "var(--rose)",
    ];
    const ranked = [...activeStaff].sort((a, b) => b.assigned - a.assigned);
    return {
      shown: ranked.slice(0, 4).map((member, index) => ({
        key: member.id,
        label: member.name || member.email,
        value: member.assigned,
        tone: tones[index],
      })),
      rest: Math.max(0, ranked.length - 4),
      restLoad: ranked.slice(4).reduce((sum, member) => sum + member.assigned, 0),
    };
  }, [activeStaff]);

  /** How many reviewers currently have at least one profile past the window. */
  const reviewersBehind = Object.keys(breachesByStaff).length;

  /** Mean queue depth across the active roster. */
  const averageLoad = activeStaff.length
    ? Math.round((totals?.assigned ?? 0) / activeStaff.length)
    : 0;

  const filterCounts = useMemo(() => {
    const counts = {} as Record<AllocFilter, number>;
    for (const { id } of ALLOC_FILTERS) {
      counts[id] = candidates.filter((candidate) => matches(candidate, id)).length;
    }
    return counts;
  }, [candidates, matches]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return candidates.filter((candidate) => {
      if (!matches(candidate, filter)) return false;
      if (!needle) return true;
      const haystack = [
        candidate.profile?.full_name,
        candidate.profile?.email,
        candidate.profile?.current_designation,
        candidate.assigned_staff_name,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [candidates, filter, matches, query]);

  // ---- actions ---------------------------------------------------------- //
  const handleCreate = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!form.email.trim() || !form.password) {
      onToast("An email address and a password are required.", "error");
      return;
    }
    setSubmitting(true);
    try {
      const result = await createStaff({
        email: form.email.trim(),
        password: form.password,
        name: form.name.trim(),
      });
      // Nothing was reallocated, and the toast says so: an admin who wants the
      // existing pile levelled across the new account has to ask for it, and
      // this is where they find out that is a separate step.
      onToast(
        `${result.staff.name} added to the roster. New arrivals will start routing to them.`,
        "success",
      );
      setForm(EMPTY_FORM);
      setCreating(false);
      reload();
      onCandidatesChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not create the account.", "error");
    } finally {
      setSubmitting(false);
    }
  };

  const handleToggleActive = async (member: StaffWorkloadRow) => {
    try {
      await updateStaff(member.id, { active: !member.active });
      onToast(
        member.active
          ? `${member.name} deactivated — they keep their profiles but receive no new ones.`
          : `${member.name} reactivated.`,
        "success",
      );
      reload();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not update the account.", "error");
    }
  };

  const handleDelete = async (member: StaffWorkloadRow) => {
    // Say which half of their queue does what before it happens. "Will be
    // redistributed" was true of only the unread part, and an admin who had
    // just deleted an account was surprised to find the evaluated profiles
    // still pointing at it.
    const reviewed = Math.max(0, member.assigned - member.unviewed);
    const warning =
      member.assigned > 0
        ? `Delete ${member.name}?\n\n` +
          `${member.unviewed} unread profile${member.unviewed === 1 ? "" : "s"} will go to the ` +
          `rest of the team.` +
          (reviewed > 0
            ? `\n${reviewed} already reviewed will keep their evaluation and wait for you to ` +
              `re-home them.`
            : "")
        : `Delete ${member.name}?`;
    if (!window.confirm(warning)) return;

    try {
      const result = await deleteStaff(member.id, true);
      onToast(
        result.orphaned > 0
          ? `${member.name} removed. ${result.reallocated} reallocated, ${result.orphaned} reviewed ` +
              `profile${result.orphaned === 1 ? "" : "s"} waiting to be re-homed.`
          : `${member.name} removed and their ${result.reallocated} profile${
              result.reallocated === 1 ? "" : "s"
            } redistributed.`,
        result.orphaned > 0 ? "info" : "success",
      );
      reload();
      onCandidatesChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not delete the account.", "error");
    }
  };

  const handleRebalance = async () => {
    if (activeStaff.length === 0) {
      onToast("There is no active staff account to rebalance across.", "error");
      return;
    }
    setRebalancing(true);
    try {
      const result = await rebalanceCandidates();
      onToast(
        result.moved === 0
          ? "Already level — nothing needed moving."
          : `${result.moved} profile${result.moved === 1 ? "" : "s"} rebalanced.` +
              (result.locked > 0 ? ` ${result.locked} left in place (already reviewed).` : ""),
        "success",
      );
      reload();
      onCandidatesChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not rebalance.", "error");
    } finally {
      setRebalancing(false);
    }
  };

  /**
   * Clear the orphan backlog: give every stranded profile a live owner.
   *
   * Not `handleRebalance`, which the banner used to call and which could never
   * have worked — an orphan is orphaned because it has been reviewed, and a
   * rebalance is defined by leaving reviewed profiles alone. The banner
   * therefore stayed on screen no matter how many times it was pressed.
   */
  const handleRehome = async () => {
    setRehoming(true);
    try {
      const result = await rehomeOrphans();
      onToast(
        result.rehomed === 0
          ? "Nothing left to re-home."
          : `${result.rehomed} profile${result.rehomed === 1 ? "" : "s"} re-homed with their ` +
              `evaluations intact.`,
        "success",
      );
      reload();
      onCandidatesChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not re-home those profiles.", "error");
    } finally {
      setRehoming(false);
    }
  };

  const handleReassign = async (candidateId: string, staffId: string) => {
    if (!staffId) return;
    setMovingId(candidateId);
    try {
      await assignCandidate(candidateId, staffId);
      const name = staff.find((member) => member.id === staffId)?.name ?? "the selected staff member";
      onToast(`Reassigned to ${name}.`, "success");
      reload();
      onCandidatesChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not reassign.", "error");
    } finally {
      setMovingId(null);
    }
  };

  /** Place one profile the way ingestion would: with whoever is holding fewest. */
  const handleAutoAssign = async (candidateId: string) => {
    setMovingId(candidateId);
    try {
      const result = await autoAssignCandidate(candidateId);
      onToast(`Allocated to ${result.assigned_staff_name ?? "the least-loaded staff member"}.`, "success");
      reload();
      onCandidatesChanged();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not auto-allocate.", "error");
    } finally {
      setMovingId(null);
    }
  };

  /**
   * Run the sweep now rather than waiting for the beat timer.
   *
   * The scheduled sweep is what raises alerts and notifies; this is the same
   * code path on demand, for an admin who has just chased something up and
   * wants the board to reflect it.
   */
  const handleScan = async () => {
    setScanning(true);
    try {
      const result = await runSlaScan();
      const parts = [`${formatInt(result.in_breach)} in breach`];
      if (result.new_alerts) parts.push(`${formatInt(result.new_alerts)} newly alerted`);
      if (result.resolved) parts.push(`${formatInt(result.resolved)} resolved`);
      onToast(`SLA sweep complete — ${parts.join(", ")}.`, result.in_breach ? "info" : "success");
      reload();
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not run the sweep.", "error");
    } finally {
      setScanning(false);
    }
  };

  // ---- render ----------------------------------------------------------- //
  if (loading) {
    return (
      <section className="db-card">
        <span className="app-boot-spinner" />
      </section>
    );
  }

  if (error) {
    return (
      <section className="db-card">
        <h3 className="db-card-title">Could not load staff management</h3>
        <p className="db-card-sub">{error}</p>
        <button type="button" className="db-btn" onClick={reload}>
          Try again
        </button>
      </section>
    );
  }

  /** The three things an admin comes to this screen to do. */

  /** Three numbers: the pile, the throughput, the risk. */

  return (
    <div className="staff-admin">
      {/* ---- Box 1: the pool in one number, with the breakdown as columns.
           Three action cards and three stat tiles used to open this screen —
           six bordered objects before the first piece of actual work. They are
           one box now, the same shape My Candidates uses. ---- */}
      <section className="sq-summary">
        <header className="sq-summary-head">
          <div>
            <h3 className="db-card-title">Allocation overview</h3>
            <p className="db-card-sub">Everything ingested, and who is holding it.</p>
          </div>

          {/* The three actions, as controls rather than as cards. Their old
              subtitles are gone: a sentence explaining what Rebalance does
              belongs in a tooltip, not in a permanent third of the screen. */}
          <div className="sq-actions">
            <button type="button" className="sq-next" onClick={() => setCreating(true)}>
              <UserPlus size={16} />
              <span>Add staff member</span>
              <ArrowRight size={15} />
            </button>

            <button
              type="button"
              className="sq-action"
              onClick={() => void handleRebalance()}
              disabled={rebalancing || activeStaff.length === 0}
              title="Level untouched profiles. Anything reviewed stays with its owner."
            >
              {rebalancing ? <Loader2 size={15} className="icon-spin" /> : <Scale size={15} />}
              <span>{rebalancing ? "Rebalancing…" : "Rebalance"}</span>
            </button>

            <button
              type="button"
              className="sq-action"
              onClick={() => void handleScan()}
              disabled={scanning}
              title={`Re-check every allocation against the ${thresholdHours}-hour window.`}
            >
              {scanning ? <Loader2 size={15} className="icon-spin" /> : <ShieldCheck size={15} />}
              <span>{scanning ? "Sweeping…" : "Run SLA sweep"}</span>
            </button>
          </div>
        </header>

        <div className="sq-headline">
          <span className="sq-headline-value">{formatInt(activeStaff.length)}</span>
          <span className={`sq-headline-chip ${imbalance ? "is-warn" : ""}`}>
            {imbalance ? "Uneven load" : "Balanced"}
          </span>
          <span className="sq-headline-label">
            <Users size={14} /> Active reviewers
          </span>
        </div>

        <div className="sq-metrics">
          <div className="sq-metric">
            <span className="sq-metric-label">
              <UserPlus size={14} /> Accounts
            </span>
            <strong className="sq-metric-value">{formatInt(staff.length)}</strong>
            <em className="sq-metric-note">
              {formatInt(activeStaff.length)} active ·{" "}
              {formatInt(Math.max(0, staff.length - activeStaff.length))} deactivated
            </em>
          </div>

          <div className="sq-metric">
            <span className="sq-metric-label">
              <Inbox size={14} /> Average queue
            </span>
            <strong className="sq-metric-value">{formatInt(averageLoad)}</strong>
            <em className="sq-metric-note">
              {activeStaff.length
                ? `${formatInt(totals?.assigned ?? 0)} profiles across the roster`
                : "No active reviewer to allocate to"}
            </em>
          </div>

          <div className="sq-metric">
            <span className="sq-metric-label">
              <Scale size={14} /> Load spread
            </span>
            <strong className={`sq-metric-value ${imbalance ? "is-alert" : ""}`}>
              {formatInt(imbalance?.spread ?? 0)}
            </strong>
            <em className="sq-metric-note">
              {imbalance
                ? `${imbalance.busiest.name || imbalance.busiest.email} is holding the most — rebalance to level it`
                : "Every reviewer is within one profile of the others"}
            </em>
          </div>

          <div className="sq-metric">
            <span className="sq-metric-label">
              <Clock size={14} /> Reviewers behind
            </span>
            <strong className={`sq-metric-value ${reviewersBehind ? "is-alert" : ""}`}>
              {formatInt(reviewersBehind)}
            </strong>
            <em className="sq-metric-note">
              {reviewersBehind
                ? `${formatInt(breaches.length)} profiles past the ${thresholdHours}h window`
                : "Nobody is past the review window"}
            </em>
          </div>
        </div>

        {totals && totals.orphaned > 0 && (
          <div className="staff-note is-warn">
            <AlertTriangle size={15} />
            <span>
              <strong>
                {formatInt(totals.orphaned)} profile{totals.orphaned === 1 ? " is" : "s are"} still
                assigned to a deleted account and nobody can see{" "}
                {totals.orphaned === 1 ? "it" : "them"}.
              </strong>{" "}
              {totals.orphaned === 1 ? "It was" : "They were"} already reviewed, so re-homing keeps
              the evaluation — or use the Orphaned filter below to place{" "}
              {totals.orphaned === 1 ? "it" : "them"} yourself.
            </span>
            <button
              type="button"
              className="db-btn is-primary"
              onClick={() => void handleRehome()}
              disabled={rehoming || activeStaff.length === 0}
              title={
                activeStaff.length === 0
                  ? "There is no active account to re-home these to"
                  : "Spread them across the active roster, verdicts intact"
              }
            >
              {rehoming ? <Loader2 size={15} className="icon-spin" /> : <Users size={15} />}
              {rehoming ? "Re-homing…" : "Re-home now"}
            </button>
          </div>
        )}
      </section>

      <div className="sq-row">
        {/* ---- Box 2: where the pool stands, as concentric arcs. ---- */}
        <section className="sq-chart">
          <header className="db-card-head">
            <h3 className="db-card-title">Workload split</h3>
          </header>

          <p className="sq-chart-total">
            <span className="sq-chart-total-label">Allocated across the roster</span>
            <span className="sq-chart-total-value">
              {compactNumber(totals?.assigned ?? 0)}
            </span>
          </p>

          <div className="sq-chart-body">
            <ul className="sq-legend">
              {staffSegments.shown.length === 0 ? (
                <li className="sq-legend-row">
                  <span className="sq-legend-label">No active reviewers</span>
                </li>
              ) : (
                staffSegments.shown.map((segment) => (
                  <li key={segment.key} className="sq-legend-row">
                    <span className="sq-legend-dot" style={{ background: segment.tone }} />
                    <span className="sq-legend-label">{segment.label}</span>
                    <span className="sq-legend-value">{formatInt(segment.value)}</span>
                  </li>
                ))
              )}

              {/* Never let the cap hide people: the arcs stop at four, the
                  count does not. */}
              {staffSegments.rest > 0 && (
                <li className="sq-legend-row is-rest">
                  <span className="sq-legend-dot" style={{ background: "var(--tint-3)" }} />
                  <span className="sq-legend-label">
                    +{formatInt(staffSegments.rest)} more
                  </span>
                  <span className="sq-legend-value">{formatInt(staffSegments.restLoad)}</span>
                </li>
              )}
            </ul>

            <svg
              className="sq-rings"
              viewBox="0 0 180 180"
              role="img"
              aria-label={staffSegments.shown
                .map((x) => `${x.label} ${x.value}`)
                .join(", ")}
            >
              {staffSegments.shown.map((segment, index) => {
                const radius = 76 - index * 18;
                const circumference = 2 * Math.PI * radius;
                const allocated = totals?.assigned ?? 0;
                const share = allocated ? segment.value / allocated : 0;
                return (
                  <g key={segment.key}>
                    <circle className="sq-ring-track" cx="90" cy="90" r={radius} />
                    {share > 0 && (
                      <circle
                        className="sq-ring-arc"
                        cx="90"
                        cy="90"
                        r={radius}
                        stroke={segment.tone}
                        strokeDasharray={`${circumference * share} ${circumference}`}
                      />
                    )}
                  </g>
                );
              })}
            </svg>
          </div>
        </section>

        {/* ---- Box 3: the roster, compact until asked otherwise. Expanded it
             carries the three cards this screen used to end with: the workload
             matrix, the SLA breaches and the allocation directory. ---- */}
        <section className="sq-recent">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">
                {showAll ? "Workload, SLA and allocation" : "Staff workload"}
              </h3>
              <p className="db-card-sub">
                {showAll
                  ? "Every account, every breach, and who owns each profile."
                  : `${activeStaff.length} active · new profiles go to whoever is holding the fewest.`}
              </p>
            </div>

            <button type="button" className="sq-detail" onClick={() => setShowAll((on) => !on)}>
              {showAll ? "Show summary" : "View details"}
              <ArrowRight size={14} />
            </button>
          </header>

          {!showAll &&
            (activeStaff.length === 0 ? (
              <div className="db-empty">
                <p className="db-empty-title">No active staff accounts</p>
                <p className="db-empty-sub">
                  Add one and new profiles will start being allocated automatically.
                </p>
              </div>
            ) : (
              <ul className="sq-recent-list">
                {activeStaff.map((member) => {
                  const opened = Math.max(0, member.assigned - member.unviewed);
                  const pct = member.assigned
                    ? Math.round((member.evaluated / member.assigned) * 100)
                    : 0;
                  return (
                    <li key={member.id}>
                      <div className="sq-recent-row is-static">
                        <span className="staff-avatar">
                          {initialsOf(member.name || member.email)}
                        </span>
                        <span className="sq-recent-identity">
                          <strong>{member.name || member.email}</strong>
                          <em>{member.email}</em>
                        </span>
                        <span className="sq-recent-when">
                          {formatInt(member.assigned)} allocated · {formatInt(opened)} opened
                        </span>
                        <span className="sq-staff-pct">{pct}%</span>
                      </div>
                    </li>
                  );
                })}
              </ul>
            ))}

          {showAll && (
          <>
      {/* ---- Workload matrix ---- */}
      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">Staff workload matrix</h3>
            <p className="db-card-sub">
              {activeStaff.length === 0
                ? "No active accounts — ingested résumés will stay unallocated."
                : `${formatInt(activeStaff.length)} active${
                    staff.length !== activeStaff.length
                      ? ` · ${formatInt(staff.length - activeStaff.length)} deactivated`
                      : ""
                  } · new profiles go to whoever is holding the fewest.`}
            </p>
          </div>
          <div className="staff-head-actions">
            <button
              type="button"
              className="db-btn"
              onClick={() => void handleRebalance()}
              disabled={rebalancing || activeStaff.length === 0}
              title="Level untouched profiles across the active roster"
            >
              {rebalancing ? <Loader2 size={15} className="icon-spin" /> : <Scale size={15} />}
              {rebalancing ? "Rebalancing…" : "Rebalance"}
            </button>
            <button type="button" className="db-btn is-primary" onClick={() => setCreating(true)}>
              <Plus size={15} />
              Create Staff Member
            </button>
          </div>
        </header>

        {staff.length === 0 ? (
          <div className="db-empty">
            <Users size={22} />
            <p className="db-empty-title">No staff accounts yet</p>
            <p className="db-empty-sub">
              Ingested résumés stay unallocated until there is someone to allocate them to.
            </p>
            <button type="button" className="db-btn is-primary" onClick={() => setCreating(true)}>
              <UserPlus size={15} />
              Create the first account
            </button>
          </div>
        ) : (
          <div className="staff-matrix">
            {staff.map((member) => {
              const overdue = breachesByStaff[member.id] ?? 0;
              const share = Math.round((member.assigned / heaviest) * 100);
              return (
                <article
                  key={member.id}
                  className={`staff-row ${member.active ? "" : "is-inactive"}`}
                >
                  <div className="staff-identity">
                    <span className="staff-avatar">{initialsOf(member.name || member.email)}</span>
                    <div className="staff-identity-text">
                      <span className="staff-name">
                        {member.name}
                        {!member.active && <em className="staff-flag">deactivated</em>}
                        {overdue > 0 && (
                          <em className="staff-flag is-overdue" title={`${overdue} past the SLA`}>
                            {overdue} overdue
                          </em>
                        )}
                      </span>
                      <span className="staff-mail">{member.email}</span>
                    </div>
                  </div>

                  {/* Three figures in the same order on every row, so the
                      column reads down as well as across. */}
                  <div className="staff-metrics">
                    <span className="staff-metric">
                      <em>{formatInt(member.assigned)}</em>Allocated
                    </span>
                    <span className="staff-metric">
                      <em>{formatInt(member.unviewed)}</em>Unviewed
                    </span>
                    <span className="staff-metric">
                      <em>{formatInt(member.pending)}</em>Pending
                    </span>
                  </div>

                  <div className="staff-progress">
                    <div className="db-bar-row">
                      <span className="db-bar-label">
                        {formatInt(member.evaluated)} / {formatInt(member.assigned)} evaluated
                      </span>
                      <span className="db-bar-value">{member.progress}%</span>
                    </div>
                    <div className="db-bar-track">
                      <span
                        className={`db-bar-fill ${member.progress >= 100 ? "is-success" : ""}`}
                        style={{ width: `${member.progress}%` }}
                      />
                    </div>
                    {/* The load bar is drawn against the heaviest queue, so the
                        matrix shows at a glance who is carrying the team. */}
                    <div className="staff-load" title={`${formatInt(member.assigned)} allocated`}>
                      <span className="staff-load-fill" style={{ width: `${share}%` }} />
                    </div>
                  </div>

                  <div className="staff-actions">
                    <button
                      type="button"
                      className="db-btn"
                      onClick={() => void handleToggleActive(member)}
                      title={
                        member.active
                          ? "Stop routing new profiles here; keeps their existing work"
                          : "Start routing new profiles here again"
                      }
                    >
                      <KeyRound size={14} />
                      {member.active ? "Deactivate" : "Reactivate"}
                    </button>
                    <button
                      type="button"
                      className="db-btn is-danger"
                      onClick={() => void handleDelete(member)}
                      title="Delete the account and redistribute its profiles"
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      {/* ---- SLA breaches ---- */}
      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className={`db-card-title ${breaches.length > 0 ? "is-alert" : ""}`}>
              {breaches.length > 0 ? <AlertTriangle size={16} /> : <ShieldCheck size={16} />}
              Past the {thresholdHours}-hour SLA
            </h3>
            <p className="db-card-sub">
              Allocated over {thresholdHours} hours ago and still not opened, or opened and never
              judged.
            </p>
          </div>
          <div className="staff-head-actions">
            <button type="button" className="db-btn" onClick={() => void handleScan()} disabled={scanning}>
              {scanning ? <Loader2 size={14} className="icon-spin" /> : <ShieldCheck size={14} />}
              {scanning ? "Sweeping…" : "Run sweep"}
            </button>
            <button type="button" className="db-btn" onClick={reload}>
              <RefreshCw size={14} />
              Refresh
            </button>
          </div>
        </header>

        {breaches.length === 0 ? (
          <div className="db-empty is-compact">
            <CheckCircle2 size={20} />
            <p className="db-empty-title">Everything is inside the window</p>
            <p className="db-empty-sub">
              No allocated profile has sat unopened or unjudged for {thresholdHours} hours.
            </p>
          </div>
        ) : (
          <div className="sla-list">
            {breaches.map((alert) => (
              <div key={alert.candidate_id} className="sla-row">
                <span className="sla-hours">{Math.round(alert.hours_overdue)}h</span>
                <div className="sla-body">
                  <span className="sla-name">{alert.full_name ?? alert.candidate_name}</span>
                  <span className="sla-meta">
                    {alert.assigned_staff_name} ·{" "}
                    {alert.reason === "unviewed" ? "never opened" : "opened, not evaluated"}
                  </span>
                </div>
                <span className={`db-pill ${alert.reason === "unviewed" ? "is-failed" : "is-pending"}`}>
                  {alert.reason === "unviewed" ? "Unviewed" : "Unevaluated"}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* ---- Candidate directory with reassignment ---- */}
      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">Candidate allocation</h3>
            <p className="db-card-sub">
              Every profile and who owns it. Reassigning restarts that profile&apos;s SLA clock and
              clears any evaluation.
            </p>
          </div>
          <div className="search-input-wrapper staff-search">
            <Search size={15} />
            <input
              className="search-input"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search a candidate or an owner…"
              aria-label="Search the allocation directory"
            />
          </div>
        </header>

        <div className="staff-filters">
          <div className="db-tabs" role="tablist" aria-label="Allocation filter">
            {ALLOC_FILTERS.map(({ id, label }) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={filter === id}
                className={`db-tab ${filter === id ? "is-on" : ""}`}
                onClick={() => setFilter(id)}
              >
                {label}
                <span className="db-tab-count">{formatInt(filterCounts[id])}</span>
              </button>
            ))}
          </div>
          <span className="result-count">
            {formatInt(filtered.length)} of {formatInt(candidates.length)} shown
          </span>
        </div>

        {filtered.length === 0 ? (
          <div className="db-empty is-compact">
            <p className="db-empty-title">
              {candidates.length === 0 ? "No candidates yet" : "Nothing matches that"}
            </p>
            <p className="db-empty-sub">
              {candidates.length === 0
                ? "Run a sync from the header and allocated profiles will appear here."
                : "Try another search term, or clear the filter."}
            </p>
          </div>
        ) : (
          <>
            <div className="alloc-table" role="table">
              <div className="alloc-head" role="row">
                <span role="columnheader">Candidate</span>
                <span role="columnheader">Allocated</span>
                <span role="columnheader">State</span>
                <span role="columnheader">Assign to</span>
              </div>
              {filtered.slice(0, visible).map((candidate) => {
                const name = candidate.profile?.full_name || candidate.profile?.email || "Unnamed";
                const status = candidate.evaluation_status ?? "pending";
                const busy = movingId === candidate.id;
                const overdue = overdueIds.has(candidate.id);
                const orphaned = isOrphan(candidate);
                return (
                  <div
                    key={candidate.id}
                    className={`alloc-row ${overdue ? "is-overdue" : ""}`}
                    role="row"
                  >
                    <button
                      type="button"
                      className="alloc-name"
                      onClick={() => onOpenCandidate(candidate)}
                      title="Open this profile"
                    >
                      <span className="staff-avatar is-small">{initialsOf(name)}</span>
                      <span>
                        <strong>{name}</strong>
                        <em>{candidate.profile?.current_designation ?? candidate.profile?.email}</em>
                      </span>
                    </button>

                    {/* An orphan still carries the name of whoever owned it,
                        which on its own reads as "allocated, fine". Saying the
                        account is gone is the whole point of the row. */}
                    <span className="alloc-owner">
                      {candidate.assigned_staff_name ?? <em className="alloc-none">unallocated</em>}
                      {orphaned ? (
                        <em className="alloc-none">account deleted</em>
                      ) : (
                        candidate.assigned_at && (
                          <em className="alloc-when">{timeAgo(candidate.assigned_at)}</em>
                        )
                      )}
                    </span>

                    <span className={`db-pill is-${status}`}>
                      {candidate.viewed_at ? status.replace("_", " ") : "unviewed"}
                    </span>

                    <div className="alloc-assign">
                      <select
                        className="modal-select alloc-select"
                        value={candidate.assigned_staff_id ?? ""}
                        disabled={busy || activeStaff.length === 0}
                        onChange={(event) => void handleReassign(candidate.id, event.target.value)}
                      >
                        <option value="">
                          {activeStaff.length === 0 ? "No active staff" : "Select staff…"}
                        </option>
                        {activeStaff.map((member) => (
                          <option key={member.id} value={member.id}>
                            {member.name} ({member.assigned})
                          </option>
                        ))}
                      </select>
                      {/* Only offered where it changes something: an allocated
                          profile already has the owner the balancer would pick. */}
                      {!candidate.assigned_staff_id && (
                        <button
                          type="button"
                          className="db-btn alloc-auto"
                          disabled={busy || activeStaff.length === 0}
                          onClick={() => void handleAutoAssign(candidate.id)}
                          title="Allocate to whoever is holding the fewest"
                        >
                          {busy ? <Loader2 size={14} className="icon-spin" /> : <Wand2 size={14} />}
                          Auto
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {filtered.length > visible && (
              <div className="alloc-more">
                <button
                  type="button"
                  className="db-btn"
                  onClick={() => setVisible((count) => count + PAGE_SIZE)}
                >
                  Show {formatInt(Math.min(PAGE_SIZE, filtered.length - visible))} more
                </button>
              </div>
            )}
          </>
        )}
      </section>

          </>
          )}
        </section>
      </div>

      {/* ---- Create staff modal ---- */}
      <div
        className={`modal-overlay ${creating ? "active" : ""}`}
        onClick={() => !submitting && setCreating(false)}
      >
        <div
          className="modal-container is-narrow"
          role="dialog"
          aria-modal="true"
          aria-label="Create staff member"
          onClick={(event) => event.stopPropagation()}
        >
          <form onSubmit={handleCreate}>
            <div className="modal-header">
              <div>
                <h3 className="modal-title">Create staff member</h3>
                <p className="modal-subtitle">
                  They sign in with these credentials and see only what they are allocated.
                </p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setCreating(false)}
                aria-label="Close"
              >
                <X size={18} />
              </button>
            </div>

            <div className="modal-body">
              <div className="modal-row-2">
                <label className="field-group">
                  <span className="modal-label">Full name</span>
                  <input
                    className="modal-input"
                    value={form.name}
                    onChange={(event) => setForm({ ...form, name: event.target.value })}
                    placeholder="Priya Raman"
                  />
                </label>
                <label className="field-group">
                  <span className="modal-label">Email</span>
                  <input
                    className="modal-input"
                    type="email"
                    required
                    value={form.email}
                    onChange={(event) => setForm({ ...form, email: event.target.value })}
                    placeholder="priya@company.com"
                  />
                </label>
              </div>

              <label className="field-group">
                <span className="modal-label">Password</span>
                <input
                  className="modal-input"
                  type="password"
                  required
                  value={form.password}
                  onChange={(event) => setForm({ ...form, password: event.target.value })}
                  placeholder="Set an initial password"
                />
              </label>

              <p className="modal-hint">
                Candidates are allocated purely by workload — whoever is holding the fewest gets
                the next résumé — so there is nothing else to configure. Nothing already allocated
                moves: this account starts empty and fills up as résumés arrive. To level the
                existing pile across it, use <strong>Rebalance Workload</strong>.
              </p>
            </div>

            <div className="modal-footer">
              <button
                type="button"
                className="modal-cancel-btn"
                onClick={() => setCreating(false)}
                disabled={submitting}
              >
                Cancel
              </button>
              {/* "Create & rebalance" until this named two actions, and only one
                  of them was wanted. Creating an account is now exactly that. */}
              <button type="submit" className="modal-submit-btn" disabled={submitting}>
                {submitting ? <Loader2 size={15} className="icon-spin" /> : <UserPlus size={15} />}
                {submitting ? "Creating…" : "Create staff member"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
