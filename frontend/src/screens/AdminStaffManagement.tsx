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
  ChevronRight,
  Clock,
  KeyRound,
  Loader2,
  Phone,
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
import SplitDonut from "@/components/dashboard/SplitDonut";
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
  /**
   * Where "Add staff" goes.
   *
   * An account is an account: the one place that creates them, with the role
   * and the page grants that come with them, is User Management. When the shell
   * passes this, the button hands the intent over to that screen instead of
   * opening the cut-down form below — which stays only for an admin whose
   * grants do not include the User Management page.
   */
  onCreateStaff?: () => void;
}

const EMPTY_FORM = { email: "", password: "", name: "", phone: "" };

/** A queue opens on one page and grows on request. */
const PAGE_SIZE = 25;

type AllocFilter = "all" | "unallocated" | "orphaned" | "unviewed" | "overdue" | "evaluated";

/**
 * The tabs inside one queue.
 *
 * "Unallocated" and "Orphaned" are not among them: a queue is already defined
 * by whose it is, so narrowing it to "belongs to nobody" can only ever come
 * back empty. Those two are queues in their own right — see the sentinels.
 */
const QUEUE_FILTERS: { id: AllocFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "unviewed", label: "Unviewed" },
  { id: "overdue", label: "Overdue" },
  { id: "evaluated", label: "Evaluated" },
];

/** The two piles that belong to nobody, addressed the way a staff id is. */
const UNALLOCATED = "__unallocated";
const ORPHANED = "__orphaned";

export default function AdminStaffManagement({
  candidates,
  refreshNonce,
  onToast,
  onCandidatesChanged,
  onOpenCandidate,
  onCreateStaff,
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
  /** Whose queue is open: a staff id, or one of the two bucket sentinels. */
  const [detailId, setDetailId] = useState<string | null>(null);
  /** The roster, so a KPI tile can bring it into view. */
  const rosterRef = useRef<HTMLElement | null>(null);

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

  // Narrowing the queue takes you back to its first page; keeping the old
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

  /** The two piles nobody owns, counted here so card and queue never disagree. */
  const buckets = useMemo(
    () => ({
      unallocated: candidates.filter((candidate) => !candidate.assigned_staff_id).length,
      orphaned: candidates.filter((candidate) => isOrphan(candidate)).length,
    }),
    [candidates, isOrphan],
  );

  /**
   * Everything the open queue holds, before its tab and its search box.
   *
   * A staff queue excludes orphans deliberately: a profile pointing at a
   * deleted account keeps the id it was assigned to, and if that id is ever
   * reissued it would otherwise surface in a stranger's queue.
   */
  const queueBase = useMemo(() => {
    if (!detailId) return [] as CandidateRecord[];
    if (detailId === UNALLOCATED) return candidates.filter((c) => !c.assigned_staff_id);
    if (detailId === ORPHANED) return candidates.filter((c) => isOrphan(c));
    return candidates.filter((c) => c.assigned_staff_id === detailId && !isOrphan(c));
  }, [candidates, detailId, isOrphan]);

  const filterCounts = useMemo(() => {
    const counts = {} as Record<AllocFilter, number>;
    for (const { id } of QUEUE_FILTERS) {
      counts[id] = queueBase.filter((candidate) => matches(candidate, id)).length;
    }
    return counts;
  }, [queueBase, matches]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return queueBase.filter((candidate) => {
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
  }, [filter, matches, query, queueBase]);

  /**
   * Open one queue, always from the top of it.
   *
   * The tab and the search box are shared by every queue, so they are cleared
   * on the way in: a filter left over from the last person's pile silently
   * hides most of the next person's.
   */
  const openQueue = useCallback((id: string) => {
    setDetailId(id);
    setFilter("all");
    setQuery("");
    setVisible(PAGE_SIZE);
  }, []);

  const closeQueue = useCallback(() => setDetailId(null), []);

  useEffect(() => {
    if (!detailId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeQueue();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closeQueue, detailId]);

  /** Who the open queue belongs to — null for either of the two buckets. */
  const queueMember = useMemo(
    () => (detailId ? staff.find((member) => member.id === detailId) ?? null : null),
    [detailId, staff],
  );

  const queueTitle = queueMember
    ? queueMember.name || queueMember.email
    : detailId === ORPHANED
      ? "Orphaned profiles"
      : "Unallocated profiles";

  const queueSubtitle = queueMember
    ? queueMember.email
    : detailId === ORPHANED
      ? "Assigned to an account that no longer exists — re-homing keeps the evaluation."
      : "Nobody owns these yet, so nobody can see them.";

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
        phone: form.phone.trim(),
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
  /** Allocated but never opened — the outer slice of the ring. */
  const unopenedTotal = staff.reduce((sum, member) => sum + member.unviewed, 0);

  const kpis = [
    {
      label: "Candidate pool",
      value: compactNumber(pool),
      caption: `${formatInt(totals?.assigned ?? 0)} allocated · ${formatInt(
        totals?.unassigned ?? 0,
      )} unowned`,
      icon: Users,
      alert: false,
      jump: null as string | null,
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
      jump: null as string | null,
    },
    {
      label: `Past the ${thresholdHours}h SLA`,
      value: formatInt(breaches.length),
      // The tile is the route to the rows it counts. It lands on the roster,
      // where every card carries its own overdue badge, and opening that card
      // lands on the Overdue tab of that person's queue — which is where a
      // breach gets acted on rather than merely read.
      caption: breaches.length ? "Not opened, or opened and not judged" : "All inside the window",
      icon: Clock,
      alert: breaches.length > 0,
      jump: "roster" as string | null,
    },
  ];

  /** The most urgent thing wrong with the roster, or nothing. See below. */
  const advisory = totals && totals.orphaned > 0 ? "orphans" : imbalance ? "imbalance" : null;

  /** Hand the request to User Management when it is reachable, else open the local form. */
  const startCreate = () => (onCreateStaff ? onCreateStaff() : setCreating(true));

  /**
   * Take the eye to the roster from a KPI tile.
   *
   * Without the scroll the tile is a control whose entire effect happens a
   * screen below the click — press "Past the 24h SLA" and, as far as anything
   * visible goes, nothing has happened.
   */
  const showRoster = () => {
    rosterRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
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
        <button type="button" className="ds-primary-btn" onClick={startCreate}>
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

      <div className="ds-stats is-three">
        {/* The evaluated share is the ring's job — it says the same percentage
            with the breakdown behind it, so the flat card for it comes off. */}
        {kpis
          .filter((kpi) => kpi.label !== "Evaluated")
          .map((kpi) => {
          const Icon = kpi.icon;
          return (
            <button
              key={kpi.label}
              type="button"
              className="ds-stat"
              onClick={() => kpi.jump && showRoster()}
              disabled={!kpi.jump}
            >
              <span className="ds-stat-top">
                <span className="ds-stat-label">{kpi.label}</span>
                <span className={`ds-stat-icon ${kpi.alert ? "is-alert" : ""}`} aria-hidden="true">
                  <Icon size={16} strokeWidth={2} />
                </span>
              </span>
              <span className={`ds-stat-value ${kpi.alert ? "is-alert" : ""}`}>{kpi.value}</span>
              <span className="ds-stat-foot">{kpi.caption}</span>
            </button>
          );
          })}

        {/* The ring says what the percentage beside it is a percentage of —
            "8% evaluated" reads very differently against thirteen profiles
            than against nine hundred. */}
        <section className="ds-stat is-static">
          <span className="ds-stat-top">
            <span className="ds-stat-label">Review progress</span>
          </span>
          <SplitDonut
            size={104}
            centre={`${evaluatedPct}%`}
            slices={[
              { label: "Evaluated", value: totals?.evaluated ?? 0, color: "var(--success)" },
              {
                label: "Opened, not judged",
                value: Math.max(
                  0,
                  (totals?.assigned ?? 0) - (totals?.evaluated ?? 0) - unopenedTotal,
                ),
                color: "rgb(var(--primary-rgb))",
              },
              { label: "Unopened", value: unopenedTotal, color: "var(--warning)" },
            ]}
          />
        </section>
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

      {/* ---- Roster ----------------------------------------------------- //
          A card per person rather than a row per person. The table this
          replaces gave four numeric columns the full width of the screen to
          say "0", and buried the one thing an admin comes here to do — look
          inside somebody's pile — behind no affordance at all. The card is
          the affordance: the whole of it opens that person's queue. */}
      <section className="db-card" ref={rosterRef}>
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">Staff roster</h3>
            <p className="db-card-sub">
              {activeStaff.length === 0
                ? "No active accounts — ingested résumés will stay unallocated."
                : `${formatInt(activeStaff.length)} active${
                    staff.length !== activeStaff.length
                      ? ` · ${formatInt(staff.length - activeStaff.length)} deactivated`
                      : ""
                  } · new profiles go to whoever is holding the fewest. Open a card to see and move what it holds.`}
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
            <button type="button" className="db-btn is-primary" onClick={startCreate}>
              <UserPlus size={15} />
              Create the first account
            </button>
          </div>
        ) : (
          <div className="staff-grid">
            {staff.map((member) => {
              const overdue = breachesByStaff[member.id] ?? 0;
              return (
                <article
                  key={member.id}
                  className={`staff-card ${member.active ? "" : "is-inactive"} ${
                    overdue > 0 ? "is-overdue" : ""
                  }`}
                >
                  {/* The hit area is laid over the card rather than wrapped
                      around it: a <button> may not contain the list and the bar
                      below it, and the two real buttons in the footer have to
                      stay clickable in their own right. */}
                  <button
                    type="button"
                    className="staff-card-hit"
                    onClick={() => openQueue(member.id)}
                    aria-label={`Open the ${formatInt(member.assigned)} profiles allocated to ${
                      member.name || member.email
                    }`}
                  />

                  <div className="staff-card-top">
                    <span className="ds-avatar" aria-hidden="true">
                      {initialsOf(member.name || member.email)}
                    </span>
                    <span className="staff-card-id">
                      <strong>{member.name || member.email}</strong>
                      <small>{member.email}</small>
                    </span>
                    <ChevronRight size={16} className="staff-card-chev" aria-hidden="true" />
                  </div>

                  {(!member.active || overdue > 0) && (
                    <div className="staff-card-flags">
                      {!member.active && <em className="staff-flag">deactivated</em>}
                      {overdue > 0 && (
                        <em className="staff-flag is-overdue">
                          {formatInt(overdue)} past the {thresholdHours}h SLA
                        </em>
                      )}
                    </div>
                  )}

                  <dl className="staff-card-stats">
                    <div>
                      <dt>Allocated</dt>
                      <dd>{formatInt(member.assigned)}</dd>
                    </div>
                    <div>
                      <dt>Unviewed</dt>
                      <dd className={member.unviewed > 0 ? "is-warn" : ""}>
                        {formatInt(member.unviewed)}
                      </dd>
                    </div>
                    <div>
                      <dt>Pending</dt>
                      <dd>{formatInt(member.pending)}</dd>
                    </div>
                    <div>
                      <dt>Judged</dt>
                      <dd className={member.evaluated > 0 ? "is-good" : ""}>
                        {formatInt(member.evaluated)}
                      </dd>
                    </div>
                  </dl>

                  {/* The figure and the bar together: the percentage on its own
                      says nothing about how much work it is a percentage of. */}
                  <div className="staff-card-progress">
                    <span className="staff-progress-line">
                      {formatInt(member.evaluated)} of {formatInt(member.assigned)} judged
                      <em>{member.progress}%</em>
                    </span>
                    <span className="db-bar-track">
                      <span
                        className={`db-bar-fill ${member.progress >= 100 ? "is-success" : ""}`}
                        style={{ width: `${member.progress}%` }}
                      />
                    </span>
                  </div>

                  <footer className="staff-card-foot">
                    {/* Above the hit area, so it dials rather than opening the
                        queue — chasing somebody about their pile usually means
                        ringing them, not reading it again. */}
                    {member.phone ? (
                      <a
                        className="staff-card-phone"
                        href={`tel:${member.phone.replace(/\s+/g, "")}`}
                        title={`Call ${member.name || member.email}`}
                      >
                        <Phone size={13} />
                        {member.phone}
                      </a>
                    ) : (
                      <span className="staff-card-phone is-empty">No number</span>
                    )}
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
                  </footer>
                </article>
              );
            })}

            {/* The two piles that belong to nobody. They are cards for the same
                reason the people are: without them, the only route to a profile
                nobody owns would be a blanket rebalance. */}
            {buckets.unallocated > 0 && (
              <article className="staff-card is-bucket">
                <button
                  type="button"
                  className="staff-card-hit"
                  onClick={() => openQueue(UNALLOCATED)}
                  aria-label={`Open the ${formatInt(buckets.unallocated)} unallocated profiles`}
                />
                <div className="staff-card-top">
                  <span className="ds-avatar is-bucket" aria-hidden="true">
                    <Users size={15} />
                  </span>
                  <span className="staff-card-id">
                    <strong>Unallocated</strong>
                    <small>Waiting for an owner</small>
                  </span>
                  <ChevronRight size={16} className="staff-card-chev" aria-hidden="true" />
                </div>
                <p className="staff-card-bucket">
                  <strong>{formatInt(buckets.unallocated)}</strong>
                  <em>
                    Invisible to every staff dashboard until somebody is holding them. Allocate
                    them one at a time here, or level the whole pile with Rebalance.
                  </em>
                </p>
              </article>
            )}

            {buckets.orphaned > 0 && (
              <article className="staff-card is-bucket is-overdue">
                <button
                  type="button"
                  className="staff-card-hit"
                  onClick={() => openQueue(ORPHANED)}
                  aria-label={`Open the ${formatInt(buckets.orphaned)} orphaned profiles`}
                />
                <div className="staff-card-top">
                  <span className="ds-avatar is-bucket is-alert" aria-hidden="true">
                    <AlertTriangle size={15} />
                  </span>
                  <span className="staff-card-id">
                    <strong>Orphaned</strong>
                    <small>The owning account is gone</small>
                  </span>
                  <ChevronRight size={16} className="staff-card-chev" aria-hidden="true" />
                </div>
                <p className="staff-card-bucket">
                  <strong>{formatInt(buckets.orphaned)}</strong>
                  <em>
                    Still carrying whoever used to own them, and nobody can see them. Re-homing
                    keeps the evaluation.
                  </em>
                </p>
              </article>
            )}
          </div>
        )}
      </section>

      {/* The SLA breach list used to be its own card between the matrix and the
          directory — a third long block naming the same candidates the
          directory named, with no way to act on any of them. It is gone. The
          hours are on the queue row itself under the Overdue tab, which is
          where reassigning actually happens, and the KPI tile above is the
          route to it. */}
      {/* ---- One queue, opened from a roster card ----------------------- //
          This used to be a permanent block at the foot of the screen listing
          every candidate in the system at once — the same names the roster
          summarises, a scroll and a half of them, with the owner repeated on
          every row. It is the inside of a card now: you ask whose pile you
          want to look at, and that is the only pile you get. */}
      <div
        className={`modal-overlay ${detailId ? "active" : ""}`}
        onClick={() => !movingId && closeQueue()}
      >
        <div
          className="modal-container is-queue"
          role="dialog"
          aria-modal="true"
          aria-label={queueTitle}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="modal-header">
            <div className="queue-head">
              <span
                className={`ds-avatar ${queueMember ? "" : "is-bucket"} ${
                  detailId === ORPHANED ? "is-alert" : ""
                }`}
                aria-hidden="true"
              >
                {queueMember ? (
                  initialsOf(queueMember.name || queueMember.email)
                ) : detailId === ORPHANED ? (
                  <AlertTriangle size={15} />
                ) : (
                  <Users size={15} />
                )}
              </span>
              <div>
                <h3 className="modal-title">{queueTitle}</h3>
                <p className="modal-subtitle">
                  {queueSubtitle}
                  {queueMember?.phone && (
                    <>
                      {" · "}
                      <a href={`tel:${queueMember.phone.replace(/\s+/g, "")}`}>
                        {queueMember.phone}
                      </a>
                    </>
                  )}
                </p>
              </div>
            </div>
            <button type="button" className="modal-close" onClick={closeQueue} aria-label="Close">
              <X size={18} />
            </button>
          </div>

          <div className="modal-body is-flush">
            {/* The member's own numbers, so closing the card to check one is
                never necessary while you are moving their work around. */}
            {queueMember && (
              <div className="queue-stats">
                <span>
                  <em>Allocated</em>
                  <strong>{formatInt(queueMember.assigned)}</strong>
                </span>
                <span>
                  <em>Unviewed</em>
                  <strong className={queueMember.unviewed > 0 ? "is-warn" : ""}>
                    {formatInt(queueMember.unviewed)}
                  </strong>
                </span>
                <span>
                  <em>Pending</em>
                  <strong>{formatInt(queueMember.pending)}</strong>
                </span>
                <span>
                  <em>Judged</em>
                  <strong className={queueMember.evaluated > 0 ? "is-good" : ""}>
                    {formatInt(queueMember.evaluated)}
                  </strong>
                </span>
                <span>
                  <em>Progress</em>
                  <strong>{queueMember.progress}%</strong>
                </span>
              </div>
            )}

            <div className="queue-controls">
              {/* Tabs only where there is more than one state to sort — the
                  unallocated pile is by definition unviewed and unjudged. */}
              {queueMember ? (
                <div className="db-tabs" role="tablist" aria-label="Filter this queue">
                  {QUEUE_FILTERS.map(({ id, label }) => (
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
              ) : (
                <span className="result-count">
                  {formatInt(queueBase.length)} profile{queueBase.length === 1 ? "" : "s"}
                </span>
              )}

              <div className="search-input-wrapper staff-search">
                <Search size={15} />
                <input
                  className="search-input"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search this queue…"
                  aria-label="Search this queue"
                />
              </div>
            </div>

            <p className="queue-note">Reassigning restarts the SLA clock and clears any evaluation.</p>

            {filtered.length === 0 ? (
              <div className="db-empty is-compact">
                <p className="db-empty-title">
                  {queueBase.length === 0 ? "Nothing in this queue" : "Nothing matches that"}
                </p>
                <p className="db-empty-sub">
                  {queueBase.length === 0
                    ? "New profiles arrive here as they are allocated."
                    : "Try another search term, or another tab."}
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
                    const name =
                      candidate.profile?.full_name || candidate.profile?.email || "Unnamed";
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
                            <em>
                              {candidate.profile?.current_designation ?? candidate.profile?.email}
                            </em>
                          </span>
                        </button>

                        {/* An orphan still carries the name of whoever owned it,
                            which on its own reads as "allocated, fine". Saying
                            the account is gone is the whole point of the row. */}
                        <span className="alloc-owner">
                          {candidate.assigned_staff_name ?? (
                            <em className="alloc-none">unallocated</em>
                          )}
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
                          {overdue && overdueHours[candidate.id] !== undefined && (
                            <em className="alloc-overdue">
                              {formatInt(overdueHours[candidate.id])}h over
                            </em>
                          )}
                        </span>

                        <div className="alloc-assign">
                          <Select
                            size="sm"
                            value={candidate.assigned_staff_id ?? ""}
                            disabled={busy || activeStaff.length === 0}
                            onChange={(staffId) => void handleReassign(candidate.id, staffId)}
                            placeholder={
                              activeStaff.length === 0 ? "No active staff" : "Select staff…"
                            }
                            ariaLabel={`Assign ${name} to a staff member`}
                            options={activeStaff.map((member) => ({
                              value: member.id,
                              label: member.name,
                              // What the native `<select>` could only fit inside
                              // the label as "(12)". Saying what the number is
                              // turns a count into the thing you choose on.
                              hint: `holding ${formatInt(member.assigned)}`,
                            }))}
                          />
                          {/* Only offered where it changes something: an
                              allocated profile already has the owner the
                              balancer would pick. */}
                          {!candidate.assigned_staff_id && (
                            <button
                              type="button"
                              className="db-btn alloc-auto"
                              disabled={busy || activeStaff.length === 0}
                              onClick={() => void handleAutoAssign(candidate.id)}
                              title="Allocate to whoever is holding the fewest"
                            >
                              {busy ? (
                                <Loader2 size={14} className="icon-spin" />
                              ) : (
                                <Wand2 size={14} />
                              )}
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
          </div>
        </div>
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

              <div className="modal-row-2">
                <label className="field-group">
                  <span className="modal-label">Mobile number</span>
                  <input
                    className="modal-input"
                    type="tel"
                    value={form.phone}
                    onChange={(event) => setForm({ ...form, phone: event.target.value })}
                    placeholder="+91 98765 43210"
                  />
                </label>
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
              </div>

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
