"use client";

/**
 * The staff member's workspace, in the same register as the Overview and the
 * Candidates screen: what is mine, how much of it is late, then the work
 * itself.
 *
 * It used to say every figure three times over — four KPI cards, a row of
 * three shortcut cards, and a row of filter tabs, all carrying the same counts
 * a click apart. The readings and the filter are now one control: five cards,
 * each the number and the tab that selects it. What is left of the shortcuts
 * is the one that was not a filter — "Review next", which opens the profile
 * closest to its deadline, and belongs in the page head as the single primary
 * action on the screen.
 *
 * This screen is the queue and only the queue: filters, the SLA clock, and this
 * reviewer's own turnaround. Opening a profile leaves it for the review screen,
 * which carries the full résumé and the verdict form together.
 *
 * There is no ingestion control anywhere on it — syncing the mailbox is the
 * admin's, and offering it here would be offering a 403.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  CheckCircle2,
  Clock,
  Eye,
  FileText,
  Inbox,
  Search,
  Sparkles,
  Star,
  Timer,
  Users,
  type LucideIcon,
} from "lucide-react";

import { fetchUiConfig, resumeDownloadUrl, type CandidateRecord } from "@/lib/api";
import { formatInt, initialsOf, timeAgo } from "@/lib/format";

interface StaffDashboardProps {
  /** Already scoped to this user — the API only ever returns their own. */
  candidates: CandidateRecord[];
  /** Ids that arrived over the socket this session, marked NEW in the queue. */
  arrivedIds?: Set<string>;
  /** A profile the notification bell asked this screen to open. */
  focusCandidateId?: string | null;
  onFocusHandled?: () => void;
  onToast: (message: string, type?: "info" | "success" | "error") => void;
  /**
   * Leave the queue for the unified review screen.
   *
   * The single way out of this screen, and every route to a candidate goes
   * through it: the row, the eye, "Review next", and a notification the bell
   * handed over. One destination means a reviewer never has to know which of
   * two views they are about to get.
   */
  onOpenCandidate: (candidate: CandidateRecord) => void;
}

type QueueFilter = "all" | "unviewed" | "pending" | "evaluated" | "at_risk";
type QueueSort = "sla" | "newest" | "name";

/** One outlined mark per reading, in the order the cards are laid out. */
const FILTERS: { id: QueueFilter; label: string; icon: LucideIcon }[] = [
  { id: "all", label: "Allocated to me", icon: Users },
  { id: "unviewed", label: "Unviewed", icon: Inbox },
  { id: "pending", label: "Pending review", icon: Eye },
  { id: "evaluated", label: "Evaluated", icon: CheckCircle2 },
  { id: "at_risk", label: "At risk", icon: Timer },
];

const SORTS: { id: QueueSort; label: string }[] = [
  { id: "sla", label: "Least time remaining" },
  { id: "newest", label: "Newest first" },
  { id: "name", label: "Name (A–Z)" },
];

/** Only until `/config` answers with the deployment's real threshold. */
const DEFAULT_SLA_HOURS = 24;
const AT_RISK_FRACTION = 0.25;

function isEvaluated(candidate: CandidateRecord): boolean {
  const status = candidate.evaluation_status ?? "pending";
  return status !== "pending";
}

function hoursBetween(from: string | null | undefined, to: number): number | null {
  if (!from) return null;
  const started = new Date(from).getTime();
  if (Number.isNaN(started)) return null;
  return (to - started) / 3600000;
}

function hoursRemaining(
  candidate: CandidateRecord,
  slaHours: number,
  now: number,
): number | null {
  if (candidate.viewed_at || isEvaluated(candidate)) return null;
  const elapsed = hoursBetween(candidate.assigned_at, now);
  if (elapsed === null) return null;
  return slaHours - elapsed;
}

function formatHours(hours: number): string {
  const total = Math.abs(hours);
  if (total < 1) return `${Math.max(1, Math.round(total * 60))}m`;
  if (total < 24) return `${total < 10 ? total.toFixed(1) : Math.round(total)}h`;
  return `${Math.round(total / 24)}d`;
}

function nameOf(candidate: CandidateRecord): string {
  return candidate.profile?.full_name || candidate.profile?.email || "Unnamed";
}

/**
 * Where a profile stands, and the tone that carries it.
 *
 * A dot and a word, the same pair the Overview and the Candidates table use.
 * The verdicts a reviewer can record are more than three, so they collapse to
 * the four tones the palette has: cleared, in flight, waiting, turned down.
 */
const VERDICT_TONE: Record<string, string> = {
  shortlisted: "ok",
  hired: "ok",
  interviewing: "info",
  on_hold: "warn",
  rejected: "bad",
};

function statusOf(candidate: CandidateRecord): { label: string; tone: string } {
  if (isEvaluated(candidate)) {
    const verdict = candidate.evaluation_status ?? "";
    return {
      label: verdict.replace(/_/g, " ") || "Evaluated",
      tone: VERDICT_TONE[verdict] ?? "info",
    };
  }
  if (!candidate.viewed_at) return { label: "Unviewed", tone: "warn" };
  return { label: "Pending", tone: "info" };
}

export default function StaffDashboard({
  candidates,
  arrivedIds,
  focusCandidateId,
  onFocusHandled,
  onToast,
  onOpenCandidate,
}: StaffDashboardProps) {
  const [filter, setFilter] = useState<QueueFilter>("all");
  const [sort, setSort] = useState<QueueSort>("sla");
  const [query, setQuery] = useState("");
  const [slaHours, setSlaHours] = useState(DEFAULT_SLA_HOURS);

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void fetchUiConfig()
      .then((config) => {
        if (!cancelled && config.sla_threshold_hours > 0) {
          setSlaHours(config.sla_threshold_hours);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const counts = useMemo(() => {
    const atRisk = candidates.filter((candidate) => {
      const left = hoursRemaining(candidate, slaHours, now);
      return left !== null && left <= slaHours * AT_RISK_FRACTION;
    }).length;
    // Already past the window rather than merely close to it — the subset of
    // `at_risk` that is no longer a warning.
    const overdue = candidates.filter((candidate) => {
      const left = hoursRemaining(candidate, slaHours, now);
      return left !== null && left <= 0;
    }).length;
    return {
      all: candidates.length,
      unviewed: candidates.filter((c) => !c.viewed_at).length,
      pending: candidates.filter((c) => c.viewed_at && !isEvaluated(c)).length,
      evaluated: candidates.filter(isEvaluated).length,
      at_risk: atRisk,
      overdue,
    };
  }, [candidates, slaHours, now]);

  const performance = useMemo(() => {
    const evaluated = candidates.filter(isEvaluated);
    const startOfDay = new Date();
    startOfDay.setHours(0, 0, 0, 0);

    const today = evaluated.filter((candidate) => {
      if (!candidate.evaluated_at) return false;
      return new Date(candidate.evaluated_at).getTime() >= startOfDay.getTime();
    }).length;

    const turnarounds = evaluated
      .map((candidate) => {
        if (!candidate.assigned_at || !candidate.evaluated_at) return null;
        const hours =
          (new Date(candidate.evaluated_at).getTime() -
            new Date(candidate.assigned_at).getTime()) /
          3600000;
        return Number.isFinite(hours) && hours >= 0 ? hours : null;
      })
      .filter((value): value is number => value !== null);

    const shortlisted = evaluated.filter(
      (candidate) =>
        candidate.evaluation_status === "shortlisted" ||
        candidate.evaluation_status === "interviewing",
    ).length;

    return {
      today,
      turnaround: turnarounds.length
        ? turnarounds.reduce((sum, value) => sum + value, 0) / turnarounds.length
        : null,
      shortlistRate: evaluated.length
        ? Math.round((shortlisted / evaluated.length) * 100)
        : null,
      withinSla: turnarounds.length
        ? Math.round(
            (turnarounds.filter((hours) => hours <= slaHours).length / turnarounds.length) * 100,
          )
        : null,
    };
  }, [candidates, slaHours]);

  const visible = useMemo(() => {
    const term = query.trim().toLowerCase();

    const matches = candidates.filter((candidate) => {
      switch (filter) {
        case "unviewed":
          if (candidate.viewed_at) return false;
          break;
        case "pending":
          if (!candidate.viewed_at || isEvaluated(candidate)) return false;
          break;
        case "evaluated":
          if (!isEvaluated(candidate)) return false;
          break;
        case "at_risk": {
          const left = hoursRemaining(candidate, slaHours, now);
          if (left === null || left > slaHours * AT_RISK_FRACTION) return false;
          break;
        }
        default:
          break;
      }
      if (!term) return true;
      const haystack = [
        candidate.profile?.full_name,
        candidate.profile?.email,
        candidate.profile?.current_designation,
        candidate.profile?.location,
        ...(candidate.profile?.skills ?? []),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(term);
    });

    const sorted = [...matches];
    sorted.sort((a, b) => {
      if (sort === "name") return nameOf(a).localeCompare(nameOf(b));
      if (sort === "newest") {
        return (
          new Date(b.assigned_at ?? b.created_at).getTime() -
          new Date(a.assigned_at ?? a.created_at).getTime()
        );
      }
      const leftA = hoursRemaining(a, slaHours, now);
      const leftB = hoursRemaining(b, slaHours, now);
      if (leftA !== null && leftB !== null) return leftA - leftB;
      if (leftA !== null) return -1;
      if (leftB !== null) return 1;
      return (
        new Date(b.assigned_at ?? b.created_at).getTime() -
        new Date(a.assigned_at ?? a.created_at).getTime()
      );
    });
    return sorted;
  }, [candidates, filter, query, sort, slaHours, now]);

  const nextUp = useMemo(() => {
    const waiting = candidates
      .filter((candidate) => !candidate.viewed_at)
      .map((candidate) => ({
        candidate,
        left: hoursRemaining(candidate, slaHours, now) ?? Number.POSITIVE_INFINITY,
      }))
      .sort((a, b) => a.left - b.left);
    return waiting[0]?.candidate ?? null;
  }, [candidates, slaHours, now]);

  /**
   * The one way into a candidate from this screen.
   *
   * Marking it viewed is the parent's job, not this one's: the review screen is
   * what actually shows the profile, and stamping "opened" from here would say
   * the reviewer had seen a screen that had not finished loading.
   */
  const openProfile = useCallback(
    (candidate: CandidateRecord) => onOpenCandidate(candidate),
    [onOpenCandidate],
  );

  const focusHandledRef = useRef<string | null>(null);
  useEffect(() => {
    if (!focusCandidateId || focusHandledRef.current === focusCandidateId) return;
    focusHandledRef.current = focusCandidateId;
    const target = candidates.find((candidate) => candidate.id === focusCandidateId);
    if (target) {
      openProfile(target);
    } else {
      onToast("That profile is no longer in your queue.", "info");
    }
    onFocusHandled?.();
  }, [focusCandidateId, candidates, openProfile, onFocusHandled, onToast]);

  const progressPct = counts.all ? Math.round((counts.evaluated / counts.all) * 100) : 0;

  /** The line under each reading — what the figure above it means today. */
  const captionFor = (id: QueueFilter): string => {
    switch (id) {
      case "unviewed":
        return counts.unviewed ? "Not yet opened" : "Everything has been opened";
      case "pending":
        return counts.pending ? "Opened, awaiting a verdict" : "No verdict outstanding";
      case "evaluated":
        return counts.all
          ? `${progressPct}% of your queue${performance.today ? ` · ${formatInt(performance.today)} today` : ""}`
          : "Nothing allocated yet";
      case "at_risk":
        return counts.overdue
          ? `${formatInt(counts.overdue)} already past the ${slaHours}h window`
          : counts.at_risk
            ? `Inside the last quarter of the ${slaHours}h window`
            : "Everything is well inside the window";
      case "all":
      default:
        return counts.all ? "Everything allocated to you" : "Nothing allocated yet";
    }
  };

  /** The pace sentence, assembled from whatever there is to say. */
  const pace = [
    performance.turnaround !== null && `avg ${formatHours(performance.turnaround)} turnaround`,
    performance.shortlistRate !== null && `${performance.shortlistRate}% shortlisted`,
  ]
    .filter((part): part is string => Boolean(part))
    .join(" · ");

  const activeLabel = FILTERS.find((f) => f.id === filter)?.label ?? "Allocated to me";

  return (
    <div className="ds-page">
      {/* ── Page head ─────────────────────────────────────────────────── */}
      {/* The title and subtitle come from the shell's page head (lib/nav.ts),
          so this strip carries only the action. */}
      <header className="ds-head ds-head--actions-only">
        {/* The one shortcut that was not a filter, and so the one that is left:
            it opens the unopened profile closest to its deadline. */}
        <div className="ds-head-actions">
          <button
            type="button"
            className="ds-primary-btn"
            disabled={!nextUp}
            onClick={() => nextUp && openProfile(nextUp)}
            title={
              nextUp
                ? "Open the unopened profile closest to its deadline"
                : "Nothing is waiting on a first read"
            }
          >
            <Sparkles size={15} />
            {nextUp ? `Review next — ${nameOf(nextUp)}` : "Queue clear"}
            {nextUp && <ArrowRight size={15} />}
          </button>
        </div>
      </header>

      {/* ── Five readings, which are also the filter ──────────────────── */}
      {/* Five cards above a row of five tabs would be the same five numbers
          twice, a click apart. Each card is the reading and the tab. */}
      <div className="ds-stats is-five" role="tablist" aria-label="Filter your queue">
        {FILTERS.map(({ id, label, icon: Icon }) => {
          // The only figure on the screen allowed a second hue, and only when
          // there is something actually wrong to report.
          const alert = id === "at_risk" && counts.at_risk > 0;
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
              <span className={`ds-stat-value ${alert ? "is-alert" : ""}`}>
                {formatInt(counts[id])}
              </span>
              <span className="ds-stat-foot">{captionFor(id)}</span>
            </button>
          );
        })}
      </div>

      {/* ── How you are keeping up ────────────────────────────────────── */}
      {/* One line and a hairline bar rather than the full-width panel this used
          to be: it is a single percentage, and it was taking a band of the
          screen the size of the queue's first four rows. */}
      {performance.withinSla !== null && (
        <div className="ds-meter">
          <div className="ds-meter-line">
            <span className="ds-meter-label">On-time reviews</span>
            <span className="ds-meter-value">{performance.withinSla}%</span>
            <span className="ds-meter-note">
              of evaluations inside the {slaHours}h window
              {pace && ` · ${pace}`}
            </span>
          </div>
          <div className="ds-meter-track">
            <div
              className={`ds-meter-fill ${
                performance.withinSla >= 80 ? "is-ok" : performance.withinSla >= 50 ? "" : "is-warn"
              }`}
              style={{ width: `${performance.withinSla}%` }}
            />
          </div>
        </div>
      )}

      {/* ── The queue itself ──────────────────────────────────────────── */}
      <section className="ds-panel">
        <div className="ds-panel-head is-split">
          <div>
            <h2 className="ds-panel-title">{activeLabel}</h2>
            <p className="ds-panel-sub">
              Open a profile to read the résumé and record your evaluation.
            </p>
          </div>

          <div className="ds-panel-tools">
            <label className="ds-search">
              <Search size={15} />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search name, skill or title"
                aria-label="Search your queue"
              />
            </label>
            <select
              className="ds-select"
              value={sort}
              onChange={(event) => setSort(event.target.value as QueueSort)}
              aria-label="Sort the queue"
            >
              {SORTS.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
        </div>

        {visible.length === 0 ? (
          <div className="ds-empty-state">
            <Inbox size={30} />
            <h3>
              {query
                ? "Nothing matches that search"
                : filter === "all"
                  ? "Nothing allocated to you yet"
                  : "Nothing in this view"}
            </h3>
            <p>
              {query
                ? "Try a different name, skill or job title."
                : filter === "all"
                  ? "New résumés are allocated automatically as they are ingested."
                  : "Try another reading above."}
            </p>
            {query && (
              <button type="button" className="ds-ghost-btn" onClick={() => setQuery("")}>
                Clear search
              </button>
            )}
          </div>
        ) : (
          <div className="ds-table-wrap is-ruled">
            <table className="ds-table is-ruled">
              <thead>
                <tr>
                  <th>Candidate</th>
                  <th>Skills</th>
                  <th>Time left</th>
                  <th>Status</th>
                  <th>Score</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {visible.map((candidate) => {
                  const name = nameOf(candidate);
                  const left = hoursRemaining(candidate, slaHours, now);
                  const isNew = arrivedIds?.has(candidate.id) ?? false;
                  const status = statusOf(candidate);
                  const skills = (candidate.profile?.skills ?? []).slice(0, 3);

                  return (
                    <tr key={candidate.id} onClick={() => openProfile(candidate)}>
                      <td>
                        <span className="ds-who">
                          <span className="ds-avatar" aria-hidden="true">
                            {initialsOf(name)}
                          </span>
                          <span className="ds-who-text">
                            <strong title={name}>
                              {name}
                              {isNew && <em className="ds-badge">New</em>}
                            </strong>
                            <small>
                              {candidate.profile?.current_designation ??
                                candidate.profile?.email ??
                                "No designation parsed"}
                            </small>
                          </span>
                        </span>
                      </td>

                      <td>
                        {skills.length > 0 ? (
                          <span className="ds-chips">
                            {skills.map((skill) => (
                              <span key={skill} className="ds-chip">
                                {skill}
                              </span>
                            ))}
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>

                      {/* The clock only runs while a profile is unread; once it
                          is opened the column says when it landed instead. */}
                      <td>
                        {left === null ? (
                          <span className="ds-quiet">
                            {candidate.assigned_at ? timeAgo(candidate.assigned_at) : "—"}
                          </span>
                        ) : (
                          <span
                            className={`ds-sla ${
                              left <= 0
                                ? "is-over"
                                : left <= slaHours * AT_RISK_FRACTION
                                  ? "is-soon"
                                  : ""
                            }`}
                          >
                            <Clock size={12} />
                            {left <= 0 ? `${formatHours(left)} over` : `${formatHours(left)} left`}
                          </span>
                        )}
                      </td>

                      <td>
                        <span className={`ds-status is-${status.tone}`}>
                          <i aria-hidden="true" />
                          {status.label}
                        </span>
                      </td>

                      <td>
                        {candidate.evaluation_score ? (
                          <span
                            className="ds-score"
                            title={`${candidate.evaluation_score} of 5`}
                            aria-label={`${candidate.evaluation_score} of 5`}
                          >
                            {Array.from({ length: candidate.evaluation_score }).map((_, index) => (
                              <Star key={index} size={12} fill="currentColor" />
                            ))}
                          </span>
                        ) : (
                          <span className="ds-quiet">—</span>
                        )}
                      </td>

                      <td onClick={(event) => event.stopPropagation()}>
                        <div className="ds-acts">
                          {candidate.resume?.storage_key && (
                            <a
                              className="ds-act"
                              href={resumeDownloadUrl(candidate.id)}
                              target="_blank"
                              rel="noopener noreferrer"
                              title={`Open ${name}'s original résumé`}
                              aria-label={`Open ${name}'s original résumé`}
                              onClick={(event) => event.stopPropagation()}
                            >
                              <FileText size={15} />
                            </a>
                          )}
                          <button
                            type="button"
                            className="ds-act"
                            title={`Open ${name}'s profile and evaluation`}
                            aria-label={`Open ${name}'s profile and evaluation`}
                            onClick={() => openProfile(candidate)}
                          >
                            <Eye size={15} />
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

        <div className="ds-panel-foot">
          <span>
            Showing <strong>{formatInt(visible.length)}</strong> of{" "}
            <strong>{formatInt(counts.all)}</strong> allocated to you
          </span>
          {counts.overdue > 0 && (
            <span className="ds-status is-bad">
              <i aria-hidden="true" />
              {formatInt(counts.overdue)} past the {slaHours}h window
            </span>
          )}
        </div>
      </section>
    </div>
  );
}
