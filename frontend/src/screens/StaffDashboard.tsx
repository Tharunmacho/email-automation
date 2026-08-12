"use client";

/**
 * The staff member's workspace, laid out exactly like the Overview and the
 * admin console: three things to do, three numbers that say whether you are
 * keeping up, then the work itself.
 *
 * The same markup as both — `ov-action`, `ov-kpi`, `db-card` — because a
 * reviewer and an administrator are looking at the same records an hour apart,
 * and a screen that looked like a different product would read as one.
 *
 * Everything a staff member can do lives here: the queue, its filters, the SLA
 * clock, their own turnaround, and the split-screen evaluation drawer. There is
 * no ingestion control anywhere on it — syncing the mailbox is the admin's, and
 * offering it here would be offering a 403.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  CheckCircle2,
  Clock,
  FileText,
  Gauge,
  Inbox,
  Loader2,
  Mail,
  MapPin,
  Phone,
  Search,
  Sparkles,
  Star,
  Timer,
  X,
} from "lucide-react";

import {
  evaluateCandidate,
  fetchUiConfig,
  getCandidate,
  markCandidateViewed,
  resumeDownloadUrl,
  type CandidateRecord,
  type EvaluationStatus,
} from "@/lib/api";
import { compactNumber, formatInt, initialsOf, timeAgo } from "@/lib/format";
import CandidateProfileScreen from "./CandidateProfileScreen";

interface StaffDashboardProps {
  /** Already scoped to this user — the API only ever returns their own. */
  candidates: CandidateRecord[];
  /** Ids that arrived over the socket this session, marked NEW in the queue. */
  arrivedIds?: Set<string>;
  /** A profile the notification bell asked this screen to open. */
  focusCandidateId?: string | null;
  onFocusHandled?: () => void;
  onToast: (message: string, type?: "info" | "success" | "error") => void;
  onCandidatesChanged: () => void;
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

const STATUS_CHOICES: { value: EvaluationStatus; label: string }[] = [
  { value: "shortlisted", label: "Shortlisted" },
  { value: "interviewing", label: "Interviewing" },
  { value: "rejected", label: "Rejected" },
];

const DEFAULT_SLA_HOURS = 10;
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
  onCandidatesChanged,
}: StaffDashboardProps) {
  const [filter, setFilter] = useState<QueueFilter>("all");
  const [sort, setSort] = useState<QueueSort>("sla");
  const [query, setQuery] = useState("");
  const [slaHours, setSlaHours] = useState(DEFAULT_SLA_HOURS);

  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CandidateRecord | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

  const [score, setScore] = useState<number>(0);
  const [status, setStatus] = useState<EvaluationStatus>("shortlisted");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

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

  const openDrawer = useCallback(
    async (candidate: CandidateRecord) => {
      setOpenId(candidate.id);
      setDetail(null);
      setDetailError(null);
      setScore(candidate.evaluation_score ?? 0);
      setStatus(
        candidate.evaluation_status && candidate.evaluation_status !== "pending"
          ? candidate.evaluation_status
          : "shortlisted",
      );
      setNotes(candidate.evaluation_notes ?? "");

      try {
        const record = await getCandidate(candidate.id);
        setDetail(record);
        setScore(record.evaluation_score ?? 0);
        setNotes(record.evaluation_notes ?? "");
      } catch (err) {
        setDetailError(err instanceof Error ? err.message : "Could not open this profile.");
        return;
      }

      try {
        const result = await markCandidateViewed(candidate.id);
        if (result.first_view) onCandidatesChanged();
      } catch {}
    },
    [onCandidatesChanged],
  );

  const closeDrawer = useCallback(() => {
    setOpenId(null);
    setDetail(null);
    setDetailError(null);
  }, []);

  const focusHandledRef = useRef<string | null>(null);
  useEffect(() => {
    if (!focusCandidateId || focusHandledRef.current === focusCandidateId) return;
    focusHandledRef.current = focusCandidateId;
    const target = candidates.find((candidate) => candidate.id === focusCandidateId);
    if (target) {
      void openDrawer(target);
    } else {
      onToast("That profile is no longer in your queue.", "info");
    }
    onFocusHandled?.();
  }, [focusCandidateId, candidates, openDrawer, onFocusHandled, onToast]);

  useEffect(() => {
    if (!openId) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeDrawer();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openId, closeDrawer]);

  const save = async (advance: boolean) => {
    if (!openId) return;
    setSaving(true);
    try {
      await evaluateCandidate(openId, {
        status,
        score: score > 0 ? score : null,
        notes: notes.trim() || null,
      });
      onCandidatesChanged();

      const following = advance
        ? candidates.find(
            (candidate) => candidate.id !== openId && !candidate.viewed_at,
          ) ?? null
        : null;

      if (following) {
        onToast(`Saved. Opening ${nameOf(following)}.`, "success");
        await openDrawer(following);
      } else {
        onToast(advance ? "Saved — nothing else is waiting." : "Evaluation saved.", "success");
        closeDrawer();
      }
    } catch (err) {
      onToast(err instanceof Error ? err.message : "Could not save the evaluation.", "error");
    } finally {
      setSaving(false);
    }
  };

  const profile = detail?.profile;

  // Check if resume file is an image for preview
  const resumeMime = detail?.resume?.mime_type || "";
  const resumeName = (detail?.resume?.original_filename || "").toLowerCase();
  const isImageResume =
    resumeMime.startsWith("image/") ||
    resumeName.endsWith(".jpg") ||
    resumeName.endsWith(".jpeg") ||
    resumeName.endsWith(".png") ||
    resumeName.endsWith(".webp");

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
  const actions = [
    {
      id: "next",
      icon: Sparkles,
      title: nextUp ? `Review next — ${nameOf(nextUp)}` : "Queue clear",
      subtitle: nextUp
        ? "The unopened profile closest to its deadline."
        : "Nothing is waiting on a first read.",
      accent: true,
      arrow: true,
      on: false,
      disabled: !nextUp,
      onClick: () => nextUp && void openDrawer(nextUp),
    },
    {
      id: "unviewed",
      icon: Inbox,
      title: `Unopened (${formatInt(counts.unviewed)})`,
      subtitle: counts.unviewed
        ? "Allocated to you and not yet read."
        : "Everything allocated has been opened.",
      accent: false,
      arrow: false,
      on: filter === "unviewed",
      disabled: false,
      onClick: () => setFilter(filter === "unviewed" ? "all" : "unviewed"),
    },
    {
      id: "at_risk",
      icon: Timer,
      title: `Due soon (${formatInt(counts.at_risk)})`,
      subtitle: counts.at_risk
        ? `Inside the last quarter of the ${slaHours}-hour window.`
        : `Every profile is well inside the ${slaHours}-hour window.`,
      accent: false,
      arrow: false,
      on: filter === "at_risk",
      disabled: false,
      onClick: () => setFilter(filter === "at_risk" ? "all" : "at_risk"),
    },
  ];

  /** Three numbers: the pile, the throughput, the risk. */
  const kpis = [
    {
      label: "Allocated to you",
      value: compactNumber(counts.all),
      caption: `${formatInt(counts.unviewed)} unopened · ${formatInt(
        counts.pending,
      )} awaiting a verdict`,
      icon: Inbox,
      alert: false,
    },
    {
      label: "Evaluation progress",
      value: `${progressPct}%`,
      caption: counts.all
        ? `${formatInt(counts.evaluated)} of ${formatInt(counts.all)} judged · ${formatInt(
            performance.today,
          )} today`
        : "Nothing allocated yet",
      icon: CheckCircle2,
      alert: false,
    },
    {
      label: `Past the ${slaHours}h SLA`,
      value: formatInt(counts.overdue),
      caption: counts.overdue
        ? "Already over — open these first"
        : counts.at_risk
          ? `${formatInt(counts.at_risk)} approaching the deadline`
          : "Every profile is inside the window",
      icon: Clock,
      alert: counts.overdue > 0,
    },
  ];

  return (
    <div className="staff-workspace">
      <section className="ov-actions is-three">
        {actions.map((action) => {
          const Icon = action.icon;
          return (
            <button
              key={action.id}
              type="button"
              className={`ov-action ${action.accent || action.on ? "is-accent" : ""}`}
              onClick={action.onClick}
              disabled={action.disabled}
              aria-pressed={action.accent ? undefined : action.on}
            >
              <span className="ov-action-icon" aria-hidden="true">
                <Icon size={19} strokeWidth={2} />
              </span>
              <span className="ov-action-text">
                <span className="ov-action-title">
                  {action.title}
                  {action.arrow && <ArrowRight size={14} />}
                </span>
                <span className="ov-action-sub">{action.subtitle}</span>
              </span>
            </button>
          );
        })}
      </section>

      <section className="ov-kpis is-three">
        {kpis.map((kpi) => {
          const Icon = kpi.icon;
          return (
            <article key={kpi.label} className="ov-kpi">
              <p className="ov-kpi-label">
                {kpi.label}
                <Icon size={15} strokeWidth={2} />
              </p>
              <p className={`ov-kpi-value ${kpi.alert ? "is-alert" : ""}`}>{kpi.value}</p>
              <p className="ov-kpi-caption">{kpi.caption}</p>
            </article>
          );
        })}
      </section>

      {/* Your own turnaround, which is the half of the SLA the tiles cannot
          show: they report what is outstanding now, this reports how you have
          been clearing it. Hidden until there is something to average. */}
      {performance.turnaround !== null && (
        <div className="staff-note">
          <Gauge size={16} />
          <span>
            <strong>Average turnaround {formatHours(performance.turnaround)}.</strong>
            {pace && ` ${pace}.`}
          </span>
        </div>
      )}

      {/* ---- The queue ---- */}
      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">My candidates</h3>
            <p className="db-card-sub">
              Open a profile to read the résumé and record your evaluation.
            </p>
          </div>

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
        </header>

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
                <button
                  key={candidate.id}
                  type="button"
                  className={`queue-row ${!candidate.viewed_at ? "is-unviewed" : ""} ${
                    left !== null && left <= 0 ? "is-breached" : ""
                  }`}
                  onClick={() => void openDrawer(candidate)}
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
                </button>
              );
            })}
          </div>
        )}
      </section>

      {/* ---- Split-screen evaluation drawer ---- */}
      {openId && (
        <div className="eval-scrim" onClick={closeDrawer}>
          <aside
            className="eval-drawer"
            role="dialog"
            aria-modal="true"
            aria-label="Evaluate candidate"
            onClick={(event) => event.stopPropagation()}
          >
            <header className="eval-head">
              <div className="eval-head-id">
                <span className="staff-avatar">
                  {initialsOf(profile?.full_name ?? profile?.email ?? "?")}
                </span>
                <div>
                  <h3>{profile?.full_name ?? "Loading…"}</h3>
                  <p>
                    {profile?.current_designation ?? profile?.email ?? ""}
                    {profile?.total_experience_years
                      ? ` · ${profile.total_experience_years} yrs`
                      : ""}
                  </p>
                </div>
              </div>
              <button type="button" className="modal-close" onClick={closeDrawer} aria-label="Close">
                <X size={18} />
              </button>
            </header>

            {detailError ? (
              <div className="db-empty">
                <p className="db-empty-title">Could not open this profile</p>
                <p className="db-empty-sub">{detailError}</p>
              </div>
            ) : !detail ? (
              <div className="eval-loading">
                <span className="app-boot-spinner" />
              </div>
            ) : (
              <div className="eval-split">
                {/* Left: the PDF/Image evidence preview. */}
                <div className="eval-pane eval-pdf">
                  {detail.resume?.storage_key ? (
                    isImageResume ? (
                      <div className="eval-img-frame" style={{ width: "100%", height: "100%", overflow: "auto", display: "flex", justifyContent: "center", alignItems: "flex-start", padding: "12px", background: "#f8fafc" }}>
                        <img
                          src={resumeDownloadUrl(detail.id)}
                          alt={detail.resume.original_filename ?? "Résumé"}
                          style={{ maxWidth: "100%", height: "auto", borderRadius: "8px", boxShadow: "0 2px 8px rgba(0,0,0,0.1)" }}
                        />
                      </div>
                    ) : (
                      <iframe
                        src={resumeDownloadUrl(detail.id)}
                        title={detail.resume.original_filename ?? "Résumé PDF"}
                        className="eval-pdf-frame"
                        style={{ width: "100%", height: "100%", border: "none", minHeight: "550px", borderRadius: "8px" }}
                      />
                    )
                  ) : (
                    <div className="db-empty">
                      <p className="db-empty-title">No résumé file stored</p>
                      <p className="db-empty-sub">
                        This profile was parsed from the email body.
                      </p>
                    </div>
                  )}
                </div>

                {/* Right: Verdict Evaluation Form + Full Executive Candidate Profile View (Image 2). */}
                <div className="eval-pane eval-form" style={{ padding: "16px", overflowY: "auto" }}>
                  <div className="eval-verdict" style={{ marginBottom: "24px", border: "2px solid #e2e8f0", padding: "16px", borderRadius: "12px", background: "#ffffff" }}>
                    <h4 className="eval-section-title">Your Verdict</h4>

                    <div className="eval-stars">
                      {[1, 2, 3, 4, 5].map((value) => (
                        <button
                          key={value}
                          type="button"
                          className={`eval-star ${score >= value ? "is-on" : ""}`}
                          onClick={() => setScore(score === value ? 0 : value)}
                          aria-label={`Rate ${value} star${value === 1 ? "" : "s"}`}
                        >
                          <Star size={20} fill={score >= value ? "currentColor" : "none"} />
                        </button>
                      ))}
                      <span className="eval-score-label">
                        {score > 0 ? `${score} of 5` : "No rating"}
                      </span>
                    </div>

                    <div className="eval-choices" role="radiogroup" aria-label="Evaluation status">
                      {STATUS_CHOICES.map((choice) => (
                        <button
                          key={choice.value}
                          type="button"
                          role="radio"
                          aria-checked={status === choice.value}
                          className={`eval-choice is-${choice.value} ${
                            status === choice.value ? "is-on" : ""
                          }`}
                          onClick={() => setStatus(choice.value)}
                        >
                          {choice.label}
                        </button>
                      ))}
                    </div>

                    <div className="eval-field">
                      <label htmlFor="eval-notes">Notes</label>
                      <textarea
                        id="eval-notes"
                        rows={3}
                        value={notes}
                        onChange={(event) => setNotes(event.target.value)}
                        placeholder="What stood out, and what would you ask them."
                      />
                    </div>

                    <div className="eval-actions">
                      <button type="button" className="db-btn" onClick={closeDrawer} disabled={saving}>
                        Cancel
                      </button>
                      <button
                        type="button"
                        className="db-btn"
                        onClick={() => void save(false)}
                        disabled={saving}
                      >
                        {saving ? <Loader2 size={14} className="icon-spin" /> : <CheckCircle2 size={14} />}
                        Save verdict
                      </button>
                      <button
                        type="button"
                        className="db-btn is-primary"
                        onClick={() => void save(true)}
                        disabled={saving}
                      >
                        {saving ? <Loader2 size={14} className="icon-spin" /> : <ArrowRight size={14} />}
                        Save & next
                      </button>
                    </div>
                  </div>

                  {/* Full Executive Profile View (Image 2) */}
                  <CandidateProfileScreen
                    candidate={detail}
                    verifying={false}
                    onBack={closeDrawer}
                    onVerify={() => {}}
                  />
                </div>
              </div>
            )}
          </aside>
        </div>
      )}
    </div>
  );
}
