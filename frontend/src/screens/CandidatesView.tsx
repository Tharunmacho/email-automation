"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Briefcase,
  Check,
  CheckCircle2,
  Edit3,
  ExternalLink,
  FileSearch,
  LayoutGrid,
  Loader2,
  Plus,
  Rows3,
  Search,
  Trash2,
  UserCheck,
  Users,
  UsersRound,
  X,
  type LucideIcon,
} from "lucide-react";
import { formatInt, formatDateFull, initialsOf } from "@/lib/format";
import Select from "@/components/ui/Select";
import {
  assignCandidate,
  listStaff,
  type CandidateRecord,
  type StaffMember,
} from "@/lib/api";

export type TalentFilter = "all" | "verified" | "pending" | "active";

/** One outlined mark per reading, in the same order the cards are laid out. */
const FILTER_ICONS: Record<TalentFilter, LucideIcon> = {
  all: Users,
  pending: FileSearch,
  active: Briefcase,
  verified: CheckCircle2,
};

interface CandidatesViewProps {
  candidates: CandidateRecord[];
  onAddCandidate: () => void;
  onOpenCandidate: (candidate: CandidateRecord) => void;
  onEditCandidate: (candidate: CandidateRecord) => void;
  onDeleteCandidate: (candidateId: string) => void;
  onAssignmentChanged?: () => void;
  onToast?: (message: string, type?: "info" | "success" | "error") => void;
}

/** Confidence below this reads as "needs a human to look at it". */
const REVIEW_CONFIDENCE = 0.75;

const MAX_NAME_CHARS = 42;
const MAX_NAME_WORDS = 6;
const PLACEHOLDER_NAMES = new Set(["candidate profile", "unnamed", "n/a", "none"]);

function isUsableName(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (PLACEHOLDER_NAMES.has(trimmed.toLowerCase())) return false;
  if (trimmed.length > MAX_NAME_CHARS) return false;
  if (trimmed.split(/\s+/).length > MAX_NAME_WORDS) return false;
  if (/(?:[,;:!?]|\w{2,}\.)\s/.test(trimmed)) return false;
  return true;
}

function nameFromAddress(addr: string): string {
  const userPart = addr.split("@")[0].replace(/\d+/g, "");
  return userPart
    .replace(/[._-]+/g, " ")
    .trim()
    .replace(/\b\w/g, (l) => l.toUpperCase());
}

function getDisplayName(candidate: CandidateRecord): string {
  const profile = candidate.profile ?? {};
  const parsed = profile.full_name ?? "";
  if (isUsableName(parsed)) return parsed.trim();

  const fromName = candidate.source_email?.from_name ?? "";
  if (isUsableName(fromName)) return fromName.trim();

  const addr = profile.email || candidate.source_email?.from_addr || "";
  const derived = addr ? nameFromAddress(addr) : "";
  return derived || "Candidate Profile";
}

function getDesignation(candidate: CandidateRecord): string {
  const profile = candidate.profile ?? {};
  const exps = profile.work_experience ?? [];
  return profile.current_designation || (exps[0]?.designation ?? "") || "";
}

function getIndustry(candidate: CandidateRecord): string {
  const profile = candidate.profile ?? {};
  const skills = profile.skills ?? [];
  const lowerSkills = skills.map((s) => s.toLowerCase()).join(" ");
  if (
    lowerSkills.includes("react") ||
    lowerSkills.includes("node") ||
    lowerSkills.includes("python") ||
    lowerSkills.includes("java") ||
    lowerSkills.includes("software")
  )
    return "IT / Software";
  if (
    lowerSkills.includes("nurse") ||
    lowerSkills.includes("doctor") ||
    lowerSkills.includes("medical") ||
    lowerSkills.includes("health")
  )
    return "Healthcare";
  if (lowerSkills.includes("finance") || lowerSkills.includes("accounting") || lowerSkills.includes("bank"))
    return "Finance";
  if (lowerSkills.includes("marketing") || lowerSkills.includes("seo") || lowerSkills.includes("content"))
    return "Marketing";
  if (lowerSkills.includes("design") || lowerSkills.includes("figma") || lowerSkills.includes("ux"))
    return "Design";
  const designation = getDesignation(candidate).toLowerCase();
  if (designation.includes("engineer") || designation.includes("developer") || designation.includes("software"))
    return "IT / Software";
  if (designation.includes("account") || designation.includes("finance") || designation.includes("audit"))
    return "Finance";
  if (designation.includes("design") || designation.includes("ui") || designation.includes("ux"))
    return "Design";
  return "General";
}

function getExperienceYears(candidate: CandidateRecord): number | null {
  const years = candidate.profile?.total_experience_years;
  return years === null || years === undefined ? null : years;
}

function formatExperience(years: number | null): string {
  if (years === null) return "—";
  if (years <= 0) return "Fresher";
  if (years < 1) {
    const months = Math.max(1, Math.round(years * 12));
    return `${months} mo${months === 1 ? "" : "s"}`;
  }
  const whole = Math.floor(years);
  return `${whole} yr${whole === 1 ? "" : "s"}`;
}

function formatContact(raw: string): string {
  const value = raw.trim().replace(/\s+/g, " ");
  if (/^\d{10}$/.test(value)) return `${value.slice(0, 5)} ${value.slice(5)}`;
  return value;
}

function getContact(candidate: CandidateRecord): string {
  return formatContact(candidate.profile?.phone || candidate.phone_key || "");
}

function getEmail(candidate: CandidateRecord): string {
  return candidate.profile?.email || candidate.email_key || candidate.source_email?.from_addr || "";
}

function getReference(candidate: CandidateRecord): string {
  return candidate.candidate_code || `CAN-${candidate.id.slice(-12).toUpperCase()}`;
}

function getAdded(candidate: CandidateRecord): string {
  if (!candidate.created_at) return "—";
  const date = new Date(candidate.created_at);
  return Number.isNaN(date.getTime()) ? "—" : formatDateFull(date);
}

type StatusKey = "verified" | "review" | "active";

/**
 * The state, and the tone that carries it.
 *
 * Resolved in one place rather than at each of the three that draw it, so the
 * dot in a row, the dot on a card and the word beside either can never
 * disagree about what colour "verified" is.
 */
const STATUS_TONE: Record<StatusKey, string> = {
  verified: "ok",
  review: "warn",
  active: "info",
};

function getStatus(candidate: CandidateRecord): { key: StatusKey; label: string; tone: string } {
  if (candidate.status === "verified")
    return { key: "verified", label: "Verified", tone: STATUS_TONE.verified };
  if ((candidate.profile?.confidence ?? 1) < REVIEW_CONFIDENCE)
    return { key: "review", label: "Needs review", tone: STATUS_TONE.review };
  return { key: "active", label: "Ready to review", tone: STATUS_TONE.active };
}

const EMPTY_COPY: Record<TalentFilter, { title: string; sub: string }> = {
  all: {
    title: "No candidates yet",
    sub: "Run a Gmail sync and parsed résumés will appear here.",
  },
  verified: {
    title: "Nothing verified yet",
    sub: "Open a profile and mark it verified once you have checked it over.",
  },
  pending: {
    title: "Nothing waiting for review",
    sub: "Every parsed résumé scored above the confidence threshold.",
  },
  active: {
    title: "Nothing waiting to be checked",
    sub: "Every parsed résumé has either been verified or flagged for review.",
  },
};

const FILTERS: { id: TalentFilter; label: string }[] = [
  { id: "all", label: "All candidates" },
  { id: "pending", label: "Needs review" },
  { id: "active", label: "Ready to review" },
  { id: "verified", label: "Verified" },
];

type SortKey = "name" | "role" | "experience" | "status" | "added";
type SortDir = "asc" | "desc";

/**
 * Six facts and the row's controls.
 *
 * The old table ran to eight columns and said several things twice: a row
 * number beside a sortable list, an industry column reading "General" for most
 * of the pool, and a phone column beside an email already printed under the
 * name. What is left is what a recruiter scans down — who, what they do, how
 * long they have done it, how to reach them, when they arrived, where they
 * stand.
 */
const COLUMNS: { key: string; label: string; sort?: SortKey; align?: "num" }[] = [
  { key: "name", label: "Candidate", sort: "name" },
  { key: "role", label: "Role", sort: "role" },
  { key: "experience", label: "Experience", sort: "experience", align: "num" },
  { key: "contact", label: "Contact" },
  { key: "added", label: "Added", sort: "added" },
  { key: "owner", label: "Assigned to" },
  { key: "status", label: "Status", sort: "status" },
  { key: "actions", label: "" },
];

/**
 * Every parsed profile, on one screen.
 *
 * Four readings across the top that are also the filter, then one panel
 * holding the profiles — as a table by default, as cards when the width is
 * better spent that way. The same register as the overview: white panels on
 * the light plane, rows as tinted bands, one accent, and the state carried by
 * a dot and a word rather than by a wash across the whole line.
 */
export default function CandidatesView({
  candidates: allCandidates,
  onAddCandidate,
  onOpenCandidate,
  onEditCandidate,
  onDeleteCandidate,
  onAssignmentChanged,
  onToast,
}: CandidatesViewProps) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<TalentFilter>("all");
  const [view, setView] = useState<"table" | "cards">("table");
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);
  const [assigning, setAssigning] = useState<CandidateRecord | null>(null);
  const [staff, setStaff] = useState<StaffMember[]>([]);
  const [selectedStaffId, setSelectedStaffId] = useState("");
  const [staffLoading, setStaffLoading] = useState(false);
  const [assignmentSaving, setAssignmentSaving] = useState(false);
  const [assignmentError, setAssignmentError] = useState("");
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: "added", dir: "desc" });

  useEffect(() => {
    if (!assigning) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !assignmentSaving) setAssigning(null);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [assigning, assignmentSaving]);

  const candidates = useMemo(() => {
    if (filter === "verified") return allCandidates.filter((c) => getStatus(c).key === "verified");
    if (filter === "pending") return allCandidates.filter((c) => getStatus(c).key === "review");
    if (filter === "active") return allCandidates.filter((c) => getStatus(c).key === "active");
    return allCandidates;
  }, [allCandidates, filter]);

  const filterCounts = useMemo(() => {
    const counts = { all: allCandidates.length, verified: 0, pending: 0, active: 0 };
    for (const candidate of allCandidates) {
      const key = getStatus(candidate).key;
      if (key === "verified") counts.verified += 1;
      else if (key === "review") counts.pending += 1;
      else counts.active += 1;
    }
    return counts;
  }, [allCandidates]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return candidates;
    return candidates.filter((c) => {
      const profile = c.profile ?? {};
      const haystack = [
        getDisplayName(c),
        getDesignation(c),
        getIndustry(c),
        profile.location ?? "",
        (profile.skills ?? []).join(" "),
        getContact(c),
        getEmail(c),
        c.source_email?.to_addr ?? "",
        getReference(c),
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [candidates, query]);

  const sorted = useMemo(() => {
    const factor = sort.dir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      switch (sort.key) {
        case "name":
          return factor * getDisplayName(a).localeCompare(getDisplayName(b));
        case "role":
          return factor * getDesignation(a).localeCompare(getDesignation(b));
        case "experience": {
          const ax = getExperienceYears(a);
          const bx = getExperienceYears(b);
          if (ax === null && bx === null) return 0;
          if (ax === null) return 1;
          if (bx === null) return -1;
          return factor * (ax - bx);
        }
        case "status": {
          const order = { review: 0, active: 1, verified: 2 };
          return factor * (order[getStatus(a).key] - order[getStatus(b).key]);
        }
        case "added":
        default: {
          const at = a.created_at ? new Date(a.created_at).getTime() : 0;
          const bt = b.created_at ? new Date(b.created_at).getTime() : 0;
          return factor * (at - bt);
        }
      }
    });
  }, [filtered, sort]);

  /**
   * This month against last, per slice.
   *
   * Measured on `created_at`, so it answers "how many of these arrived
   * recently" rather than "how many exist" — the figure above it already
   * answers that. Null where last month was empty: a rise from nothing has no
   * percentage, and printing one would invent a baseline.
   */
  const filterTrends = useMemo(() => {
    const now = new Date();
    const thisMonth = new Date(now.getFullYear(), now.getMonth(), 1).getTime();
    const lastMonth = new Date(now.getFullYear(), now.getMonth() - 1, 1).getTime();

    const countIn = (rows: CandidateRecord[], from: number, to: number) =>
      rows.filter((row) => {
        const at = row.created_at ? new Date(row.created_at).getTime() : NaN;
        return !Number.isNaN(at) && at >= from && at < to;
      }).length;

    const out = {} as Record<TalentFilter, number | null>;
    for (const { id } of FILTERS) {
      // Measured on the whole pool, not on the current slice: the card for
      // "Verified" has to keep reading verified while "Needs review" is the
      // one selected, or the three cards you are not on go blank.
      const rows =
        id === "all"
          ? allCandidates
          : allCandidates.filter((row) => {
              const key = getStatus(row).key;
              if (id === "verified") return key === "verified";
              if (id === "pending") return key === "review";
              return key === "active";
            });
      const current = countIn(rows, thisMonth, now.getTime());
      const previous = countIn(rows, lastMonth, thisMonth);
      out[id] = previous > 0 ? ((current - previous) / previous) * 100 : null;
    }
    return out;
  }, [allCandidates]);

  const toggleSort = (key: SortKey) =>
    setSort((prev) =>
      prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" },
    );

  const openAssignment = async (candidate: CandidateRecord) => {
    setAssigning(candidate);
    setSelectedStaffId(candidate.assigned_staff_id ?? "");
    setAssignmentError("");
    setStaffLoading(true);
    try {
      const response = await listStaff(false);
      setStaff((response.items ?? []).filter((member) => member.active));
    } catch (error) {
      setAssignmentError(error instanceof Error ? error.message : "Could not load staff accounts.");
    } finally {
      setStaffLoading(false);
    }
  };

  const saveAssignment = async () => {
    if (!assigning || !selectedStaffId) return;
    setAssignmentSaving(true);
    setAssignmentError("");
    try {
      const result = await assignCandidate(assigning.id, selectedStaffId);
      const owner = staff.find((member) => member.id === selectedStaffId);
      const ownerName = owner?.name || owner?.email || "staff";
      onToast?.(
        result.status === "unchanged"
          ? `${getDisplayName(assigning)} is already assigned to ${ownerName}; no new WhatsApp notification was sent.`
          : result.whatsapp_notified === false
          ? `${getDisplayName(assigning)} assigned to ${ownerName}, but the WhatsApp bot did not accept the notification. Check WA_BOT_URL, WA_BOT_API_KEY, and the staff phone number.`
          : `${getDisplayName(assigning)} assigned to ${ownerName}. WhatsApp notification sent.`,
        result.status === "unchanged" || result.whatsapp_notified === false ? "info" : "success",
      );
      setAssigning(null);
      onAssignmentChanged?.();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not assign this candidate.";
      setAssignmentError(message);
      onToast?.(message, "error");
    } finally {
      setAssignmentSaving(false);
    }
  };

  /**
   * The controls a row carries.
   *
   * Opening the profile is the row itself, so there is no magnifier here
   * repeating the click the whole row already answers — that magnifier was the
   * fifth of five icons on every line, which is what made the actions column
   * read as a wall.
   */
  const rowActions = (candidate: CandidateRecord) => {
    const reviewed = candidate.status === "verified";
    return (
      <>
        <button
          type="button"
          className={`ds-review-action ${reviewed ? "is-done" : ""}`}
          title={reviewed ? "Open the completed review" : "Review this candidate"}
          onClick={() => onOpenCandidate(candidate)}
        >
          {reviewed ? <CheckCircle2 size={14} /> : <FileSearch size={14} />}
          <span>{reviewed ? "Reviewed" : "Review"}</span>
        </button>
        <button
          type="button"
          className="ds-review-action is-assign"
          title={`Assign ${getDisplayName(candidate)} to a staff member`}
          onClick={() => void openAssignment(candidate)}
        >
          <UserCheck size={14} />
          <span>Assign</span>
        </button>
        <button
          type="button"
          className="ds-act"
          title="Edit this profile"
          onClick={() => onEditCandidate(candidate)}
        >
          <Edit3 size={15} />
        </button>
        <button
          type="button"
          className="ds-act is-danger"
          title="Delete this profile"
          onClick={() => setDeleteConfirm(candidate.id)}
        >
          <Trash2 size={15} />
        </button>
      </>
    );
  };

  /** The same two-button confirmation wherever a delete is asked for. */
  const confirmDelete = (candidateId: string) => (
    <div className="ds-confirm">
      <span>Delete?</span>
      <button
        type="button"
        className="ds-act is-danger is-solid"
        title="Yes, delete it"
        onClick={() => {
          onDeleteCandidate(candidateId);
          setDeleteConfirm(null);
        }}
      >
        <Check size={15} />
      </button>
      <button type="button" className="ds-act" title="Keep it" onClick={() => setDeleteConfirm(null)}>
        <X size={15} />
      </button>
    </div>
  );

  const activeLabel = FILTERS.find((f) => f.id === filter)?.label ?? "All candidates";

  return (
    <div className="ds-page">
      {/* ── Page head ─────────────────────────────────────────────────── */}
      <header className="ds-head">
        <div>
          <h1 className="ds-head-title">Candidates</h1>
          <p className="ds-head-sub">Every parsed profile, and where each one stands</p>
        </div>

        <div className="ds-head-actions">
          <button type="button" className="ds-ghost-btn" onClick={onAddCandidate}>
            <Plus size={15} /> Add candidate
          </button>
          <button type="button" className="ds-primary-btn">
            <ExternalLink size={15} /> Export CSV
          </button>
        </div>
      </header>

      {/* ── Four readings, which are also the filter ──────────────────── */}
      {/* The readings and the filter are the same control. Four cards above a
          row of four tabs would be the same four numbers twice, a click
          apart. */}
      <div className="ds-stats" role="tablist" aria-label="Filter candidates">
        {FILTERS.map(({ id, label }) => {
          const Icon = FILTER_ICONS[id];
          const trend = filterTrends[id];
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
                <span className="ds-stat-icon" aria-hidden="true">
                  <Icon size={16} strokeWidth={2} />
                </span>
              </span>
              <span className="ds-stat-value">{formatInt(filterCounts[id])}</span>
              <span className="ds-stat-foot">
                {trend === null ? (
                  <em className="ds-stat-quiet">No arrivals this month</em>
                ) : (
                  <>
                    <em className={trend >= 0 ? "is-up" : "is-down"}>
                      {trend >= 0 ? "↑" : "↓"} {Math.abs(trend).toFixed(1)}%
                    </em>
                    since last month
                  </>
                )}
              </span>
            </button>
          );
        })}
      </div>

      {/* ── The profiles themselves ───────────────────────────────────── */}
      <section className="ds-panel">
        <div className="ds-panel-head is-split">
          <div>
            <h2 className="ds-panel-title">{activeLabel}</h2>
            <p className="ds-panel-sub">
              {formatInt(sorted.length)} shown of {formatInt(candidates.length)} on file
            </p>
          </div>

          <div className="ds-panel-tools">
            <label className="ds-search">
              <Search size={15} />
              <input
                type="search"
                value={query}
                placeholder="Search name, role, skills or email"
                onChange={(e) => setQuery(e.target.value)}
                aria-label="Search candidates"
              />
            </label>

            <div className="ds-seg is-quiet" role="group" aria-label="View">
              <button
                type="button"
                className={`ds-seg-btn is-icon ${view === "table" ? "is-on" : ""}`}
                onClick={() => setView("table")}
                title="Table view"
                aria-label="Table view"
              >
                <Rows3 size={15} />
              </button>
              <button
                type="button"
                className={`ds-seg-btn is-icon ${view === "cards" ? "is-on" : ""}`}
                onClick={() => setView("cards")}
                title="Card view"
                aria-label="Card view"
              >
                <LayoutGrid size={15} />
              </button>
            </div>
          </div>
        </div>

        {sorted.length === 0 ? (
          <div className="ds-empty-state">
            <UsersRound size={30} />
            <h3>{query ? "Nothing matches that search" : EMPTY_COPY[filter].title}</h3>
            <p>{query ? "No profile matches what you typed." : EMPTY_COPY[filter].sub}</p>
            {query && (
              <button type="button" className="ds-ghost-btn" onClick={() => setQuery("")}>
                Clear search
              </button>
            )}
          </div>
        ) : view === "cards" ? (
          <div className="ds-grid">
            {sorted.map((candidate) => {
              const displayName = getDisplayName(candidate);
              const status = getStatus(candidate);
              const email = getEmail(candidate);
              return (
                <article
                  key={candidate.id}
                  className="ds-mini"
                  onClick={() => onOpenCandidate(candidate)}
                >
                  <div className="ds-mini-head">
                    <span className="ds-who">
                      <span className="ds-avatar" aria-hidden="true">
                        {initialsOf(displayName)}
                      </span>
                      <span className="ds-who-text">
                        <strong title={displayName}>{displayName}</strong>
                        <small className="crm-record-id">Candidate ID · {getReference(candidate)}</small>
                        <small title={email}>{email || "No email on file"}</small>
                      </span>
                    </span>
                    <span className={`ds-status is-${status.tone}`}>
                      <i aria-hidden="true" />
                      {status.label}
                    </span>
                  </div>

                  <dl className="ds-mini-meta">
                    <div>
                      <dt>Role</dt>
                      <dd>{getDesignation(candidate) || "—"}</dd>
                    </div>
                    <div>
                      <dt>Industry</dt>
                      <dd>{getIndustry(candidate)}</dd>
                    </div>
                    <div>
                      <dt>Experience</dt>
                      <dd>{formatExperience(getExperienceYears(candidate))}</dd>
                    </div>
                    <div>
                      <dt>Contact</dt>
                      <dd>{getContact(candidate) || "—"}</dd>
                    </div>
                    <div>
                      <dt>Assigned to</dt>
                      <dd>{candidate.assigned_staff_name || "Unassigned"}</dd>
                    </div>
                  </dl>

                  <div className="ds-mini-foot" onClick={(e) => e.stopPropagation()}>
                    <span className="ds-mini-when">Added {getAdded(candidate)}</span>
                    <div className="ds-acts">
                      {deleteConfirm === candidate.id
                        ? confirmDelete(candidate.id)
                        : rowActions(candidate)}
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="ds-table-wrap is-ruled">
            <table className="ds-table is-ruled">
              <thead>
                <tr>
                  {COLUMNS.map((col) => (
                    <th
                      key={col.key}
                      className={`${col.align === "num" ? "is-num " : ""}${!col.label ? "is-actions" : ""}`.trim() || undefined}
                      aria-label={col.label ? undefined : "Actions"}
                    >
                      {col.sort ? (
                        <button
                          type="button"
                          className={`ds-sort ${sort.key === col.sort ? "is-on" : ""}`}
                          onClick={() => toggleSort(col.sort!)}
                        >
                          {col.label}
                          {sort.key === col.sort &&
                            (sort.dir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
                        </button>
                      ) : (
                        col.label
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sorted.map((candidate) => {
                  const displayName = getDisplayName(candidate);
                  const designation = getDesignation(candidate);
                  const contact = getContact(candidate);
                  const email = getEmail(candidate);
                  const status = getStatus(candidate);

                  return (
                    <tr className="is-clickable" key={candidate.id} onClick={() => onOpenCandidate(candidate)}>
                      <td>
                        <span className="ds-who">
                          <span className="ds-avatar" aria-hidden="true">
                            {initialsOf(displayName)}
                          </span>
                          <span className="ds-who-text">
                            <strong title={displayName}>{displayName}</strong>
                            <small className="crm-record-id">Candidate ID · {getReference(candidate)}</small>
                            <small title={email}>{email || "No email on file"}</small>
                          </span>
                        </span>
                      </td>

                      {/* The role and the industry it sits in are one fact read
                          two ways, so they share a cell rather than take a
                          column each. */}
                      <td>
                        <span className="ds-cell-main" title={designation || undefined}>
                          {designation || "—"}
                        </span>
                        <span className="ds-cell-sub">{getIndustry(candidate)}</span>
                      </td>

                      <td className="is-num">{formatExperience(getExperienceYears(candidate))}</td>

                      <td title={contact || undefined}>{contact || "—"}</td>

                      <td>{getAdded(candidate)}</td>

                      <td>
                        <span className={`ds-owner ${candidate.assigned_staff_id ? "" : "is-empty"}`}>
                          <UserCheck size={13} />
                          {candidate.assigned_staff_name || "Unassigned"}
                        </span>
                      </td>

                      <td>
                        <span className={`ds-status is-${status.tone}`}>
                          <i aria-hidden="true" />
                          {status.label}
                        </span>
                      </td>

                      <td className="is-actions" onClick={(e) => e.stopPropagation()}>
                        <div className="ds-acts">
                          {deleteConfirm === candidate.id
                            ? confirmDelete(candidate.id)
                            : rowActions(candidate)}
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
            Showing <strong>{formatInt(sorted.length)}</strong> of{" "}
            <strong>{formatInt(candidates.length)}</strong> candidates
          </span>
          <span className="ds-status is-ok">
            <i aria-hidden="true" />
            Live DB sync
          </span>
        </div>
      </section>

      {assigning && (
        <div className="cm-overlay active" onClick={() => !assignmentSaving && setAssigning(null)}>
          <div
            className="cm-dialog candidate-assign-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="candidate-assign-title"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <h3 id="candidate-assign-title" className="modal-title">Assign candidate</h3>
                <p className="modal-subtitle">Choose who will own and review this profile.</p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setAssigning(null)}
                disabled={assignmentSaving}
                aria-label="Close assignment dialog"
              >
                <X size={17} />
              </button>
            </div>

            <div className="modal-body candidate-assign-body">
              <div className="candidate-assign-person">
                <span className="ds-avatar" aria-hidden="true">{initialsOf(getDisplayName(assigning))}</span>
                <span>
                  <strong>{getDisplayName(assigning)}</strong>
                  <small>Current owner: {assigning.assigned_staff_name || "Unassigned"}</small>
                </span>
              </div>

              <div className="field-group">
                <label className="modal-label" htmlFor="candidate-owner">Assign to</label>
                {staffLoading ? (
                  <div className="candidate-assign-loading">
                    <Loader2 size={16} className="icon-spin" /> Loading staff…
                  </div>
                ) : (
                  <Select
                    id="candidate-owner"
                    value={selectedStaffId}
                    options={staff.map((member) => ({
                      value: member.id,
                      label: member.name || member.email,
                    }))}
                    onChange={setSelectedStaffId}
                    placeholder="Select an active staff member"
                    ariaLabel="Assign to"
                  />
                )}
                <span className="modal-hint">
                  New candidates continue to be distributed automatically to the least-loaded active staff member.
                </span>
              </div>

              {assignmentError && <div className="sh-form-error">{assignmentError}</div>}
            </div>

            <div className="modal-footer">
              <button
                type="button"
                className="modal-cancel-btn"
                onClick={() => setAssigning(null)}
                disabled={assignmentSaving}
              >
                Cancel
              </button>
              <button
                type="button"
                className="db-btn is-primary"
                onClick={() => void saveAssignment()}
                disabled={
                  !selectedStaffId ||
                  selectedStaffId === assigning.assigned_staff_id ||
                  staffLoading ||
                  assignmentSaving
                }
              >
                {assignmentSaving ? <Loader2 size={14} className="icon-spin" /> : <UserCheck size={14} />}
                {assignmentSaving ? "Assigning…" : "Assign candidate"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
