"use client";

import { useMemo, useState } from "react";
import {
  Search,
  UsersRound,
  Eye,
  Edit3,
  Trash2,
  MoreHorizontal,
  ArrowDown,
  ArrowUp,
  Check,
  X,
  LayoutGrid,
  Rows3,
  FileText,
  Plus,
  ExternalLink,
  Users,
  FileSearch,
  Briefcase,
  CheckCircle2,
  type LucideIcon,
} from "lucide-react";
import { formatInt, formatDateFull, initialsOf } from "@/lib/format";
import { resumeDownloadUrl, type CandidateRecord } from "@/lib/api";

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
  /** How many activity entries each candidate has, keyed by id. */
  logCounts: Record<string, number>;
  onOpenCandidate: (candidate: CandidateRecord) => void;
  onEditCandidate: (candidate: CandidateRecord) => void;
  onOpenLogs: (candidate: CandidateRecord) => void;
  onDeleteCandidate: (candidateId: string) => void;
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
  return (candidate.resume_hash?.slice(0, 8) || candidate.id.slice(-8)).toUpperCase();
}

type StatusKey = "verified" | "review" | "active";

function getStatus(candidate: CandidateRecord): { key: StatusKey; label: string } {
  if (candidate.status === "verified") return { key: "verified", label: "Verified" };
  if ((candidate.profile?.confidence ?? 1) < REVIEW_CONFIDENCE) return { key: "review", label: "Needs Review" };
  return { key: "active", label: "Active" };
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
  { id: "all", label: "All Candidates" },
  { id: "pending", label: "Needs Review" },
  { id: "active", label: "Unchecked" },
  { id: "verified", label: "Verified" },
];

type SortKey = "name" | "designation" | "industry" | "experience" | "status" | "added";
type SortDir = "asc" | "desc";

const COLUMNS = [
  { key: "index", label: "S.No." },
  { key: "name", label: "Candidate", sort: "name" as SortKey },
  { key: "designation", label: "Designation", sort: "designation" as SortKey },
  { key: "industry", label: "Industry", sort: "industry" as SortKey },
  { key: "experience", label: "Experience", sort: "experience" as SortKey },
  { key: "contact", label: "Contact" },
  { key: "status", label: "Status", sort: "status" as SortKey },
  { key: "actions", label: "Actions" },
];

export default function CandidatesView({
  candidates: allCandidates,
  logCounts,
  onOpenCandidate,
  onEditCandidate,
  onOpenLogs,
  onDeleteCandidate,
}: CandidatesViewProps) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<TalentFilter>("all");
  const [view, setView] = useState<"table" | "cards">("table");
  const [deleteConfirm, setDeleteConfirm] = useState<string | null>(null);

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

  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: "added", dir: "desc" });

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
        case "designation":
          return factor * getDesignation(a).localeCompare(getDesignation(b));
        case "industry":
          return factor * getIndustry(a).localeCompare(getIndustry(b));
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
      const rows =
        id === "all"
          ? candidates
          : candidates.filter((row) => {
              const key = getStatus(row).key;
              if (id === "verified") return key === "verified";
              if (id === "pending") return key === "review";
              return key !== "verified" && key !== "review";
            });
      const current = countIn(rows, thisMonth, now.getTime());
      const previous = countIn(rows, lastMonth, thisMonth);
      out[id] = previous > 0 ? ((current - previous) / previous) * 100 : null;
    }
    return out;
  }, [candidates]);

  const toggleSort = (key: SortKey) =>
    setSort((prev) => (prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));

  return (
    <div className="ds-page">
      <header className="ds-head">
        <div>
          <h1 className="ds-head-title">Candidates</h1>
          <p className="ds-head-sub">
            Every parsed profile in the database — designation, experience and status.
          </p>
        </div>

        <div className="ds-head-actions">
          <button type="button" className="ds-ghost-btn">
            <Plus size={15} /> Add candidate
          </button>
          <button type="button" className="ds-primary-btn">
            <ExternalLink size={15} /> Export CSV
          </button>
        </div>
      </header>

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

      <section className="ds-panel">
        <div className="ds-panel-head is-split">
          <div>
            <h2 className="ds-panel-title">
              {FILTERS.find((f) => f.id === filter)?.label ?? "All candidates"}
            </h2>
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
                className={`ds-seg-btn ${view === "table" ? "is-on" : ""}`}
                onClick={() => setView("table")}
                title="Table view"
              >
                <Rows3 size={15} />
              </button>
              <button
                type="button"
                className={`ds-seg-btn ${view === "cards" ? "is-on" : ""}`}
                onClick={() => setView("cards")}
                title="Card view"
              >
                <LayoutGrid size={15} />
              </button>
            </div>
          </div>
        </div>

      {/* Main Content Area */}
      {filtered.length === 0 ? (
        <div className="ds-empty-state">
          <UsersRound size={30} />
          <h3>{EMPTY_COPY[filter].title}</h3>
          <p>{query ? "Nothing here matches that search." : EMPTY_COPY[filter].sub}</p>
          {query && (
            <button type="button" className="ds-ghost-btn" onClick={() => setQuery("")}>
              Clear search
            </button>
          )}
        </div>
      ) : view === "cards" ? (
        /* Card View Grid */
        <div className="shopeers-cand-grid">
          {sorted.map((candidate) => {
            const displayName = getDisplayName(candidate);
            const status = getStatus(candidate);
            const years = getExperienceYears(candidate);
            const logCount = logCounts[candidate.id] ?? 0;
            return (
              <div
                key={candidate.id}
                className="shopeers-cand-card"
                onClick={() => onOpenCandidate(candidate)}
              >
                <div className="shopeers-cand-card-head">
                  <div className="shopeers-cand-cell">
                    <span className="shopeers-cand-avatar">{initialsOf(displayName)}</span>
                    <div className="shopeers-cand-info">
                      <span className="shopeers-cand-name" title={displayName}>
                        {displayName}
                      </span>
                      <span className="shopeers-cand-sub">{getDesignation(candidate) || getEmail(candidate)}</span>
                    </div>
                  </div>
                  <span className={`shopeers-status-pill is-${status.key}`}>{status.label}</span>
                </div>

                <div className="shopeers-cand-card-meta">
                  <div className="shopeers-meta-item">
                    <span className="shopeers-meta-lbl">Industry</span>
                    <span className="shopeers-meta-val">{getIndustry(candidate)}</span>
                  </div>
                  <div className="shopeers-meta-item">
                    <span className="shopeers-meta-lbl">Experience</span>
                    <span className="shopeers-meta-val">{formatExperience(years)}</span>
                  </div>
                  <div className="shopeers-meta-item">
                    <span className="shopeers-meta-lbl">Contact</span>
                    <span className="shopeers-meta-val">{getContact(candidate) || "—"}</span>
                  </div>
                  <div className="shopeers-meta-item">
                    <span className="shopeers-meta-lbl">Added</span>
                    <span className="shopeers-meta-val">
                      {candidate.created_at ? formatDateFull(new Date(candidate.created_at)) : "—"}
                    </span>
                  </div>
                </div>

                <div className="shopeers-cand-card-foot" onClick={(e) => e.stopPropagation()}>
                  <span style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>{getEmail(candidate)}</span>
                  <div className="shopeers-action-btns">
                    {candidate.resume?.storage_key && (
                      <a
                        className="shopeers-act-btn"
                        href={resumeDownloadUrl(candidate.id)}
                        target="_blank"
                        rel="noopener noreferrer"
                        title="View Resume PDF"
                      >
                        <FileText size={14} />
                      </a>
                    )}
                    <button
                      type="button"
                      className="shopeers-act-btn"
                      title="Edit Candidate"
                      onClick={() => onEditCandidate(candidate)}
                    >
                      <Edit3 size={14} />
                    </button>
                    <button
                      type="button"
                      className="shopeers-act-btn"
                      title="Activity Logs"
                      onClick={() => onOpenLogs(candidate)}
                    >
                      <MoreHorizontal size={14} />
                    </button>
                    <button
                      type="button"
                      className="shopeers-act-btn is-delete"
                      title="Delete Candidate"
                      onClick={() => {
                        if (confirm(`Permanently delete "${displayName}"?`)) {
                          onDeleteCandidate(candidate.id);
                        }
                      }}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      ) : (
        /* Table View */
        <div className="shopeers-table-card">
          <div className="shopeers-table-responsive">
            <table className="shopeers-table">
              <thead>
                <tr>
                  {COLUMNS.map((col) => (
                    <th key={col.key}>
                      {col.sort ? (
                        <button
                          type="button"
                          style={{
                            background: "none",
                            border: "none",
                            padding: 0,
                            font: "inherit",
                            color: "inherit",
                            cursor: "pointer",
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "0.25rem",
                            fontWeight: 700,
                          }}
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
                {sorted.map((candidate, index) => {
                  const displayName = getDisplayName(candidate);
                  const designation = getDesignation(candidate);
                  const industry = getIndustry(candidate);
                  const years = getExperienceYears(candidate);
                  const contact = getContact(candidate);
                  const email = getEmail(candidate);
                  const status = getStatus(candidate);
                  const isConfirmingDelete = deleteConfirm === candidate.id;

                  return (
                    <tr
                      key={candidate.id}
                      // The row carries its own state as a wash, so the column
                      // of pills is a confirmation rather than the only place
                      // the status lives.
                      className={`shopeers-tr-row tone-${
                        status.key === "verified" ? "ok" : status.key === "review" ? "warn" : "info"
                      }`}
                      onClick={() => onOpenCandidate(candidate)}
                    >
                      <td className="shopeers-td-sno">{index + 1}</td>

                      <td>
                        <div className="shopeers-cand-cell">
                          <span className="shopeers-cand-avatar">{initialsOf(displayName)}</span>
                          <div className="shopeers-cand-info">
                            <span className="shopeers-cand-name" title={displayName}>
                              {displayName}
                            </span>
                            <span className="shopeers-cand-sub">{email}</span>
                          </div>
                        </div>
                      </td>

                      <td title={designation || undefined}>
                        <span style={{ fontWeight: 600, color: "var(--text-main)" }}>
                          {designation || "—"}
                        </span>
                      </td>

                      <td title={industry}>{industry}</td>

                      <td style={{ textAlign: "center", fontWeight: 600, color: "var(--text-main)" }}>
                        {formatExperience(years)}
                      </td>

                      <td style={{ fontWeight: 500, color: "var(--dash-ink-2)" }} title={contact || undefined}>
                        {contact || "—"}
                      </td>

                      <td style={{ textAlign: "center" }}>
                        <span className={`shopeers-status-pill is-${status.key}`}>{status.label}</span>
                      </td>

                      <td onClick={(e) => e.stopPropagation()}>
                        {isConfirmingDelete ? (
                          <div style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
                            <span style={{ fontSize: "0.725rem", color: "var(--error)", fontWeight: 700 }}>
                              Delete?
                            </span>
                            <button
                              type="button"
                              className="shopeers-act-btn"
                              style={{ background: "var(--error)", color: "#FFFFFF", borderColor: "var(--error)" }}
                              onClick={() => {
                                onDeleteCandidate(candidate.id);
                                setDeleteConfirm(null);
                              }}
                            >
                              <Check size={13} />
                            </button>
                            <button
                              type="button"
                              className="shopeers-act-btn"
                              onClick={() => setDeleteConfirm(null)}
                            >
                              <X size={13} />
                            </button>
                          </div>
                        ) : (
                          <div className="shopeers-action-btns">
                            {candidate.resume?.storage_key && (
                              <a
                                className="shopeers-act-btn"
                                href={resumeDownloadUrl(candidate.id)}
                                target="_blank"
                                rel="noopener noreferrer"
                                title="View Original Resume PDF"
                                onClick={(e) => e.stopPropagation()}
                              >
                                <FileText size={14} />
                              </a>
                            )}
                            <button
                              type="button"
                              className="shopeers-act-btn"
                              title="View Executive Profile"
                              onClick={() => onOpenCandidate(candidate)}
                            >
                              <Eye size={14} />
                            </button>
                            <button
                              type="button"
                              className="shopeers-act-btn"
                              title="Edit Candidate Details"
                              onClick={() => onEditCandidate(candidate)}
                            >
                              <Edit3 size={14} />
                            </button>
                            <button
                              type="button"
                              className="shopeers-act-btn"
                              title="Activity History"
                              onClick={() => onOpenLogs(candidate)}
                            >
                              <MoreHorizontal size={14} />
                            </button>
                            <button
                              type="button"
                              className="shopeers-act-btn is-delete"
                              title="Delete Candidate"
                              onClick={() => setDeleteConfirm(candidate.id)}
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="shopeers-table-foot">
            <span>
              Showing <strong>{formatInt(sorted.length)}</strong> of{" "}
              <strong>{formatInt(candidates.length)}</strong> candidates
            </span>
            <span className="ds-status is-ok">
              <i aria-hidden="true" />
              Live DB sync
            </span>
          </div>
        </div>
      )}
      </section>
    </div>
  );
}
