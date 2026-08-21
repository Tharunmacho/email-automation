"use client";

/**
 * The staff member's workspace, laid out like the Overview and the admin
 * console: three things to do, then the numbers that say whether you are
 * keeping up, then the work itself.
 *
 * The same markup as both — `ov-action`, `ov-kpi`, `db-card` — because a
 * reviewer and an administrator are looking at the same records an hour apart,
 * and a screen that looked like a different product would read as one.
 *
 * This screen is the queue and only the queue: filters, the SLA clock, and this
 * reviewer's own turnaround. Opening a profile leaves it for the review screen,
 * which carries the full résumé and the verdict form together.
 *
 * It used to open a split drawer instead, and a separate eye icon opened the
 * executive profile — two views of one candidate, neither complete, and a
 * reviewer had to guess which one they wanted before they had seen either. The
 * row and the eye now do the same thing.
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
  Gauge,
  Inbox,
  Search,
  Sparkles,
  Star,
  Timer,
  Users,
} from "lucide-react";

import { fetchUiConfig, resumeDownloadUrl, type CandidateRecord } from "@/lib/api";
import { compactNumber, formatInt, initialsOf, timeAgo } from "@/lib/format";


/**
 * The single status a row shows.
 *
 * The rings in the chart are independent shares of the total and overlap by
 * design — an unopened profile can also be running out of clock. A row cannot
 * overlap, so it resolves to one badge in priority order: a judged profile is
 * done whatever its clock said, and after that the deadline outranks how far
 * through the read you are.
 */
function statusOf(
  candidate: CandidateRecord,
  slaHours: number,
  now: number,
): "reviewed" | "risk" | "unviewed" | "pending" {
  if (isEvaluated(candidate)) return "reviewed";
  const left = hoursRemaining(candidate, slaHours, now);
  if (left !== null && left <= slaHours * AT_RISK_FRACTION) return "risk";
  return candidate.viewed_at ? "pending" : "unviewed";
}

const STATUS_LABEL: Record<string, string> = {
  reviewed: "Reviewed",
  unviewed: "Unviewed",
  pending: "Pending review",
  risk: "Risk",
};

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

const FILTERS: { id: QueueFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "unviewed", label: "Unviewed" },
  { id: "pending", label: "Pending Review" },
  { id: "evaluated", label: "Evaluated" },
  { id: "at_risk", label: "At risk" },
];

const SORTS: { id: QueueSort; label: string }[] = [
  { id: "sla", label: "Closest to SLA" },
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
  /**
   * Whether the queue box is showing the newest handful or the whole list.
   *
   * Compact by default: this screen answers "what is waiting on me" before it
   * answers "show me all of them", and the full table with its search, sort and
   * five tabs is a lot of furniture to put in front of that first question.
   */
  const [showAll, setShowAll] = useState(false);
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
   * The four rings, and the legend beside them.
   *
   * Each is its own share of the total rather than a slice of one pie: they
   * overlap (an unopened profile can also be at risk), so they are drawn as
   * concentric arcs, which is a shape that does not claim to add up to a whole
   * the way a donut does.
   */
  const segments = [
    { key: "reviewed", label: "Reviewed", value: counts.evaluated, tone: "var(--success)" },
    { key: "unviewed", label: "Unviewed", value: counts.unviewed, tone: "var(--primary)" },
    { key: "pending", label: "Pending review", value: counts.pending, tone: "var(--warning)" },
    { key: "risk", label: "Risk", value: counts.at_risk, tone: "var(--rose)" },
  ];

  /** The newest handful, for the compact view of the queue. */
  const recent = useMemo(() => {
    return [...candidates]
      .sort((a, b) => {
        const at = new Date(a.assigned_at ?? a.ingested_at ?? 0).getTime();
        const bt = new Date(b.assigned_at ?? b.ingested_at ?? 0).getTime();
        return bt - at;
      })
      .slice(0, 5);
  }, [candidates]);

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

  /** The rest of the pace sentence, assembled from whatever there is to say. */
  const pace = [
    performance.withinSla !== null &&
      `${performance.withinSla}% of your evaluations landed inside the ${slaHours}-hour window`,
    performance.shortlistRate !== null &&
      `${performance.shortlistRate}% were shortlisted or taken to interview`,
  ]
    .filter((part): part is string => Boolean(part))
    .join(", and ");

  /** The three things a reviewer comes here to do. */

  /**
   * Four numbers: the whole workload, what is unread, what has been judged, and
   * the risk.
   *
   * "Total candidates" is its own tile rather than a caption on another,
   * because it answers the question a reviewer opens this screen with — how much
   * is mine — and reading it out of the corner of a progress tile made it look
   * like a denominator rather than a figure.
   */

  return (
    <div className="staff-workspace">
      {/* ---- Box 1: the queue in one number, then the four that break it down.
           These used to be four separate tiles in a row above a fourth card.
           Four boxes reporting on one queue is four borders, four shadows and
           four headings for what is a single reading — so they are one box now,
           with the breakdown as columns inside it. ---- */}
      <section className="sq-summary">
        <header className="sq-summary-head">
          <div>
            <h3 className="db-card-title">Queue overview</h3>
            <p className="db-card-sub">Everything allocated to you, judged or not.</p>
          </div>

          {/* The only action left on this screen. The two filter buttons that
              used to sit beside it said what the queue's own tabs already say. */}
          <button
            type="button"
            className="sq-next"
            onClick={() => nextUp && openProfile(nextUp)}
            disabled={!nextUp}
          >
            <Sparkles size={16} />
            <span>{nextUp ? `Review next — ${nameOf(nextUp)}` : "Queue clear"}</span>
            {nextUp && <ArrowRight size={15} />}
          </button>
        </header>

        <div className="sq-headline">
          <span className="sq-headline-value">{compactNumber(counts.all)}</span>
          <span className="sq-headline-chip">{progressPct}% judged</span>
          <span className="sq-headline-label">
            <Users size={14} /> Total candidates
          </span>
        </div>

        <div className="sq-metrics">
          <div className="sq-metric">
            <span className="sq-metric-label">
              <Inbox size={14} /> Unviewed
            </span>
            <strong className="sq-metric-value">{compactNumber(counts.unviewed)}</strong>
            <em className="sq-metric-note">
              {counts.unviewed
                ? `${formatInt(counts.pending)} opened, awaiting a verdict`
                : "Everything allocated has been opened"}
            </em>
          </div>

          <div className="sq-metric">
            <span className="sq-metric-label">
              <CheckCircle2 size={14} /> Evaluated
            </span>
            <strong className="sq-metric-value">{compactNumber(counts.evaluated)}</strong>
            <em className="sq-metric-note">
              {counts.all ? `${formatInt(performance.today)} judged today` : "Nothing allocated yet"}
            </em>
          </div>

          <div className="sq-metric">
            <span className="sq-metric-label">
              <Clock size={14} /> Past the {slaHours}h SLA
            </span>
            <strong className={`sq-metric-value ${counts.overdue ? "is-alert" : ""}`}>
              {formatInt(counts.overdue)}
            </strong>
            <em className="sq-metric-note">
              {counts.overdue
                ? "Already over — open these first"
                : counts.at_risk
                  ? `${formatInt(counts.at_risk)} approaching the deadline`
                  : "Every profile is inside the window"}
            </em>
          </div>

          {/* The performance note used to be a loose banner under the tiles. It
              is the same reading as the three beside it — how the queue is
              going — so it belongs in the same row rather than below it. */}
          <div className="sq-metric">
            <span className="sq-metric-label">
              <Gauge size={14} /> Avg turnaround
            </span>
            <strong className="sq-metric-value">
              {performance.turnaround !== null ? formatHours(performance.turnaround) : "—"}
            </strong>
            <em className="sq-metric-note">
              {performance.turnaround !== null
                ? pace || "Across everything you have judged"
                : "Nothing judged yet to average"}
            </em>
          </div>
        </div>
      </section>

      <div className="sq-row">
        {/* ---- Box 2: the split, as concentric arcs. ---- */}
        <section className="sq-chart">
          <header className="db-card-head">
            <h3 className="db-card-title">Candidates</h3>
          </header>

          <p className="sq-chart-total">
            <span className="sq-chart-total-label">Total candidates</span>
            <span className="sq-chart-total-value">{compactNumber(counts.all)}</span>
          </p>

          <div className="sq-chart-body">
            <ul className="sq-legend">
              {segments.map((segment) => (
                <li key={segment.key} className="sq-legend-row">
                  <span className="sq-legend-dot" style={{ background: segment.tone }} />
                  <span className="sq-legend-label">{segment.label}</span>
                  <span className="sq-legend-value">{formatInt(segment.value)}</span>
                </li>
              ))}
            </ul>

            <svg className="sq-rings" viewBox="0 0 180 180" role="img"
              aria-label={segments.map((x) => `${x.label} ${x.value}`).join(", ")}>
              {segments.map((segment, index) => {
                const radius = 76 - index * 18;
                const circumference = 2 * Math.PI * radius;
                const share = counts.all ? segment.value / counts.all : 0;
                return (
                  <g key={segment.key}>
                    <circle className="sq-ring-track" cx="90" cy="90" r={radius} />
                    {/* A round cap on a zero-length arc still paints a dot, so
                        an empty metric drew a mark the size of a small one. */}
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

      {/* ---- Box 3: the queue itself, compact until asked otherwise. ---- */}
      <section className="sq-recent">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">
              {showAll ? "All candidates" : "Recent candidates"}
            </h3>
            <p className="db-card-sub">
              {showAll
                ? "Open a profile to read the résumé and record your evaluation."
                : `The ${recent.length} most recently allocated to you.`}
            </p>
          </div>

          <button type="button" className="sq-detail" onClick={() => setShowAll((on) => !on)}>
            {showAll ? "Show recent" : "View details"}
            <ArrowRight size={14} />
          </button>
        </header>

        {/* Compact: no tools, no tabs, one badge each. The full view below is
            the same records with everything needed to work through them. */}
        {!showAll && (
          recent.length === 0 ? (
            <div className="db-empty">
              <p className="db-empty-title">Nothing allocated to you yet</p>
              <p className="db-empty-sub">
                New résumés are allocated automatically as they are ingested.
              </p>
            </div>
          ) : (
            <ul className="sq-recent-list">
              {recent.map((candidate) => {
                const name = nameOf(candidate);
                const status = statusOf(candidate, slaHours, now);
                return (
                  <li key={candidate.id}>
                    <button
                      type="button"
                      className="sq-recent-row"
                      onClick={() => openProfile(candidate)}
                    >
                      <span className="staff-avatar">{initialsOf(name)}</span>
                      <span className="sq-recent-identity">
                        <strong>{name}</strong>
                        <em>
                          {candidate.profile?.current_designation ??
                            candidate.profile?.email ??
                            "No designation parsed"}
                        </em>
                      </span>
                      <span className="sq-recent-when">
                        {candidate.assigned_at ? timeAgo(candidate.assigned_at) : "—"}
                      </span>
                      <span className={`db-pill is-${status}`}>{STATUS_LABEL[status]}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )
        )}

        {showAll && (
        <>
        <div className="queue-tools">
            <label className="queue-search">
              <Search size={14} />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Search name, skill, title…"
                aria-label="Search your queue"
              />
            </label>
            <select
              className="queue-sort"
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

        <div className="db-tabs" role="tablist">
          {FILTERS.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={filter === tab.id}
              className={`db-tab ${filter === tab.id ? "is-on" : ""} ${
                tab.id === "at_risk" && counts.at_risk > 0 ? "is-urgent" : ""
              }`}
              onClick={() => setFilter(tab.id)}
            >
              {tab.label}
              <span className="db-tab-count">{counts[tab.id]}</span>
            </button>
          ))}
        </div>

        {visible.length === 0 ? (
          <div className="db-empty">
            <p className="db-empty-title">
              {query
                ? "Nothing matches that search"
                : filter === "all"
                  ? "Nothing allocated to you yet"
                  : "Nothing in this view"}
            </p>
            <p className="db-empty-sub">
              {query
                ? "Try a different name, skill or job title."
                : filter === "all"
                  ? "New résumés are allocated automatically as they are ingested."
                  : "Try another tab."}
            </p>
          </div>
        ) : (
          <div className="queue-list">
            {visible.map((candidate) => {
              const name = nameOf(candidate);
              const evaluated = isEvaluated(candidate);
              const left = hoursRemaining(candidate, slaHours, now);
              const isNew = arrivedIds?.has(candidate.id) ?? false;
              return (
                // A row, not a <button>: the eye is a second action inside it,
                // and a button may not contain a button. The role and the two
                // keys put back exactly what the element gave up.
                <div
                  key={candidate.id}
                  role="button"
                  tabIndex={0}
                  className={`queue-row ${!candidate.viewed_at ? "is-unviewed" : ""} ${
                    left !== null && left <= 0 ? "is-breached" : ""
                  }`}
                  onClick={() => openProfile(candidate)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      openProfile(candidate);
                    }
                  }}
                >
                  <span className="staff-avatar">{initialsOf(name)}</span>

                  <span className="queue-identity">
                    <strong>
                      {name}
                      {isNew && <span className="queue-new">New</span>}
                    </strong>
                    <em>
                      {candidate.profile?.current_designation ??
                        candidate.profile?.email ??
                        "No designation parsed"}
                    </em>
                  </span>

                  <span className="queue-skills">
                    {(candidate.profile?.skills ?? []).slice(0, 3).map((skill) => (
                      <span key={skill} className="db-chip">
                        {skill}
                      </span>
                    ))}
                  </span>

                  <span className="queue-when">
                    {left === null ? (
                      candidate.assigned_at ? (
                        timeAgo(candidate.assigned_at)
                      ) : (
                        "—"
                      )
                    ) : (
                      <span className={`queue-sla ${left <= 0 ? "is-over" : left <= slaHours * AT_RISK_FRACTION ? "is-soon" : ""}`}>
                        <Clock size={11} />
                        {left <= 0 ? `${formatHours(left)} over` : `${formatHours(left)} left`}
                      </span>
                    )}
                  </span>

                  <span
                    className={`db-pill is-${evaluated ? candidate.evaluation_status : candidate.viewed_at ? "pending" : "unviewed"}`}
                  >
                    {evaluated
                      ? (candidate.evaluation_status ?? "").replace("_", " ")
                      : candidate.viewed_at
                        ? "pending"
                        : "unviewed"}
                  </span>

                  {evaluated && candidate.evaluation_score ? (
                    <span className="queue-score" title={`${candidate.evaluation_score} of 5`}>
                      {Array.from({ length: candidate.evaluation_score }).map((_, index) => (
                        <Star key={index} size={12} fill="currentColor" />
                      ))}
                    </span>
                  ) : (
                    <span className="queue-score" />
                  )}

                  {candidate.resume?.storage_key && (
                    <a
                      className="queue-eye"
                      href={resumeDownloadUrl(candidate.id)}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={`View ${name}'s original resume`}
                      aria-label={`View ${name}'s original resume`}
                      onClick={(event) => event.stopPropagation()}
                    >
                      <FileText size={14} />
                    </a>
                  )}

                  <button
                    type="button"
                    className="queue-eye"
                    title={`Open ${name}'s profile and evaluation`}
                    aria-label={`Open ${name}'s profile and evaluation`}
                    onClick={(event) => {
                      // The row underneath would fire too, opening it twice.
                      event.stopPropagation();
                      openProfile(candidate);
                    }}
                  >
                    <Eye size={14} />
                  </button>
                </div>
              );
            })}
          </div>
        )}
        </>
        )}
      </section>
      </div>
    </div>
  );
}
