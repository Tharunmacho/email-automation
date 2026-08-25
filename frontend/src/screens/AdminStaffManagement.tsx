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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  KeyRound,
  Loader2,
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

import Select from "@/components/ui/Select";
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
  /** The allocation directory, so a KPI tile can bring it into view. */
  const directoryRef = useRef<HTMLElement | null>(null);

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
   * How far past the window each breached candidate is, keyed by id.
   *
   * This is what the removed SLA card carried, kept so the directory row can
   * state it where the profile is actually reassigned. Rounded once, here,
   * rather than at every render of every row.
   */
  const overdueHours = useMemo(() => {
    const hours: Record<string, number> = {};
    for (const alert of breaches) hours[alert.candidate_id] = Math.round(alert.hours_overdue);
    return hours;
  }, [breaches]);

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

  /** Three numbers: the pile, the throughput, the risk. */
  const kpis = [
    {
      label: "Candidate pool",
      value: compactNumber(pool),
      caption: `${formatInt(totals?.assigned ?? 0)} allocated · ${formatInt(
        totals?.unassigned ?? 0,
      )} unowned`,
      icon: Users,
      alert: false,
      filter: null as AllocFilter | null,
    },
    {
      label: "Evaluated",
      value: `${evaluatedPct}%`,
      caption:
        totals && totals.assigned > 0
          ? `${formatInt(totals.evaluated)} of ${formatInt(totals.assigned)} judged`
          : "Nothing allocated yet",
      icon: CheckCircle2,
      alert: false,
      filter: "evaluated" as AllocFilter,
    },
    {
      label: `Past the ${thresholdHours}h SLA`,
      value: formatInt(breaches.length),
      // The tile is the route to the rows it counts. The screen used to carry a
      // whole second card listing these, which said the same thing twice — the
      // directory below already has an Overdue filter, and that is where a
      // breach gets acted on rather than merely read.
      caption: breaches.length ? "Not opened, or opened and not judged" : "All inside the window",
      icon: Clock,
      alert: breaches.length > 0,
      filter: "overdue" as AllocFilter,
    },
  ];

  /** The most urgent thing wrong with the roster, or nothing. See below. */
  const advisory = totals && totals.orphaned > 0 ? "orphans" : imbalance ? "imbalance" : null;

  /**
   * Narrow the directory from a KPI tile, and take the eye with you.
   *
   * Without the scroll the tile is a control whose entire effect happens a
   * screen and a half below the click — press "Past the 24h SLA" and, as far as
   * anything visible goes, nothing has happened.
   */
  const showInDirectory = (next: AllocFilter) => {
    setFilter(next);
    directoryRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="staff-admin ds-page">
      {/* The screen's actions live in its own head, beside the title they act
          on, rather than in a band of buttons floating under a generic one. */}
      <header className="ds-head">
        <div>
          <h1 className="ds-head-title">Staff &amp; allocation</h1>
          <p className="ds-head-sub">
            Accounts, expertise keywords, workload balance, and the review SLA.
          </p>
        </div>

        <div className="ds-head-actions">
        <button type="button" className="ds-primary-btn" onClick={() => setCreating(true)}>
          <UserPlus size={15} />
          Add staff
        </button>
        <button
          type="button"
          className="ds-ghost-btn"
          onClick={() => void handleRebalance()}
          disabled={rebalancing || activeStaff.length === 0}
          title="Level untouched profiles across the active roster. Reviewed work stays put."
        >
          {rebalancing ? <Loader2 size={15} className="icon-spin" /> : <Scale size={15} />}
          {rebalancing ? "Levelling…" : "Rebalance"}
        </button>
        <button
          type="button"
          className="ds-ghost-btn"
          onClick={() => void handleScan()}
          disabled={scanning}
          title={`Re-check every allocation against the ${thresholdHours}-hour window`}
        >
          {scanning ? <Loader2 size={15} className="icon-spin" /> : <ShieldCheck size={15} />}
          {scanning ? "Sweeping…" : "Run SLA sweep"}
        </button>
        <button type="button" className="ds-ghost-btn" onClick={reload} title="Refresh">
          <RefreshCw size={15} />
        </button>
        </div>
      </header>

      <div className="ds-cards is-three">
        {kpis.map((kpi) => {
          const Icon = kpi.icon;
          const content = (
            <>
              <div className="ds-card-top">
                <span className={`ds-card-icon ${kpi.alert ? "is-alert" : ""}`}>
                  <Icon size={18} strokeWidth={2.1} />
                </span>
                <div>
                  <h2 className="ds-card-title">{kpi.label}</h2>
                  <p className="ds-card-sub">{kpi.caption}</p>
                </div>
              </div>
              <div className={`ds-card-value ${kpi.alert ? "is-alert" : ""}`}>{kpi.value}</div>
            </>
          );
          // A tile that narrows the directory is a button; one that does not is
          // an article. Making them all buttons would promise a click that two
          // of the three cannot honour.
          return kpi.filter ? (
            <button
              key={kpi.label}
              type="button"
              className="ov-kpi-card is-clickable"
              onClick={() => showInDirectory(kpi.filter as AllocFilter)}
              title="Show these in the directory below"
            >
              {content}
            </button>
          ) : (
            <article key={kpi.label} className="ov-kpi-card">
              {content}
            </article>
          );
        })}
      </div>

      {/* One advisory at a time, worst first.
          Both of these used to show together, stacked, each three lines long —
          two paragraphs of explanation above the data they were about. Orphans
          win because they are invisible to every staff dashboard, where an
          uneven roster is merely inefficient. Each is bound to its own count and
          nothing else: fix it and the strip goes on the next read, which is why
          there is no dismiss control. */}
      {advisory === "orphans" && totals && (
        <div className="staff-note is-warn">
          <AlertTriangle size={15} />
          <span>
            <strong>
              {formatInt(totals.orphaned)} reviewed profile
              {totals.orphaned === 1 ? "" : "s"} assigned to a deleted account.
            </strong>{" "}
            Nobody can see {totals.orphaned === 1 ? "it" : "them"}. Re-homing keeps the evaluation.
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
            {rehoming ? "Re-homing…" : "Re-home"}
          </button>
        </div>
      )}

      {advisory === "imbalance" && imbalance && (
        <div className="staff-note">
          <Scale size={16} />
          <span>
            <strong>Workloads are uneven.</strong> {imbalance.busiest.name} holds{" "}
            {formatInt(imbalance.busiest.assigned)}; {imbalance.lightest.name} holds{" "}
            {formatInt(imbalance.lightest.assigned)}. Reviewed profiles stay with their owner.
          </span>
          <button
            type="button"
            className="db-btn is-primary"
            onClick={() => void handleRebalance()}
            disabled={rebalancing}
          >
            {rebalancing ? <Loader2 size={15} className="icon-spin" /> : <Scale size={15} />}
            {rebalancing ? "Levelling…" : "Level now"}
          </button>
        </div>
      )}

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
          {/* No buttons here. Rebalance and Create both used to sit on this
              header AND on the action band above it, so every action on the
              screen appeared twice. They live in the toolbar now, once. */}
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
          <div className="ds-table-wrap">
            <table className="ds-table staff-table">
              <thead>
                <tr>
                  <th>Staff</th>
                  <th>Allocated</th>
                  <th>Unviewed</th>
                  <th>Pending</th>
                  <th>Evaluated</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {staff.map((member) => {
                  const overdue = breachesByStaff[member.id] ?? 0;
                  return (
                    <tr key={member.id} className={member.active ? "" : "is-inactive"}>
                      <td>
                        <span className="ds-who">
                          <span className="ds-avatar" aria-hidden="true">
                            {initialsOf(member.name || member.email)}
                          </span>
                          <span className="ds-who-text">
                            <strong>
                              {member.name}
                              {!member.active && <em className="staff-flag">deactivated</em>}
                              {overdue > 0 && (
                                <em className="staff-flag is-overdue" title={`${overdue} past the SLA`}>
                                  {overdue} overdue
                                </em>
                              )}
                            </strong>
                            <small>{member.email}</small>
                          </span>
                        </span>
                      </td>
                      <td className="is-num">{formatInt(member.assigned)}</td>
                      <td className="is-num">{formatInt(member.unviewed)}</td>
                      <td className="is-num">{formatInt(member.pending)}</td>
                      {/* The figure and the bar in one cell: the percentage on
                          its own says nothing about how much work it is a
                          percentage of. */}
                      <td className="staff-progress-cell">
                        <span className="staff-progress-line">
                          {formatInt(member.evaluated)} / {formatInt(member.assigned)}
                          <em>{member.progress}%</em>
                        </span>
                        <span className="db-bar-track">
                          <span
                            className={`db-bar-fill ${member.progress >= 100 ? "is-success" : ""}`}
                            style={{ width: `${member.progress}%` }}
                          />
                        </span>
                      </td>
                      <td>
                        <div className="staff-actions">
                          <button
                            type="button"
                            className="ds-ghost-btn is-sm"
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
                            className="ds-ghost-btn is-sm is-danger"
                            onClick={() => void handleDelete(member)}
                            title="Delete the account and redistribute its profiles"
                          >
                            <Trash2 size={14} />
                          </button>
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

      {/* The SLA breach list used to be its own card between the matrix and the
          directory — a third long block naming the same candidates the
          directory names, with no way to act on any of them. It is gone. The
          hours are now on the directory row itself under the Overdue filter,
          which is where reassigning actually happens, and the KPI tile above is
          the route to it. */}

      {/* ---- Candidate directory with reassignment ---- */}
      <section className="db-card" ref={directoryRef}>
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">Candidate allocation</h3>
            <p className="db-card-sub">
              Reassigning restarts the SLA clock and clears any evaluation.
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

                    <span className="alloc-state">
                      <span className={`db-pill is-${status}`}>
                        {candidate.viewed_at ? status.replace("_", " ") : "unviewed"}
                      </span>
                    </span>

                    <div className="alloc-assign">
                      <Select
                        size="sm"
                        value={candidate.assigned_staff_id ?? ""}
                        disabled={busy || activeStaff.length === 0}
                        onChange={(staffId) => void handleReassign(candidate.id, staffId)}
                        placeholder={activeStaff.length === 0 ? "No active staff" : "Select staff…"}
                        ariaLabel={`Assign ${name} to a staff member`}
                        options={activeStaff.map((member) => ({
                          value: member.id,
                          label: member.name,
                          // What the native `<select>` could only fit inside the
                          // label as "(12)". Saying what the number is turns a
                          // count into the thing you are choosing on.
                          hint: `holding ${formatInt(member.assigned)}`,
                        }))}
                      />
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
