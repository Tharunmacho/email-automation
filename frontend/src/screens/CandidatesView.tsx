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
} from "lucide-react";
import { formatInt, formatDateFull, initialsOf } from "@/lib/format";
import type { CandidateRecord } from "@/lib/api";

export type TalentFilter = "all" | "verified" | "pending";

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

/**
 * A person's name is short and has few words. When the parser hands back a
 * clause lifted out of the résumé body — "prototyping and specialize in
 * connecting apps to powerful AI services…" — it fails both tests, and the row
 * is better off falling back to the email address than printing prose in the
 * Candidate column. These bounds are deliberately loose: they exist to reject
 * sentences, not to police unusual names.
 */
const MAX_NAME_CHARS = 42;
const MAX_NAME_WORDS = 6;
const PLACEHOLDER_NAMES = new Set(["candidate profile", "unnamed", "n/a", "none"]);

function isUsableName(value: string): boolean {
  const trimmed = value.trim();
  if (!trimmed) return false;
  if (PLACEHOLDER_NAMES.has(trimmed.toLowerCase())) return false;
  if (trimmed.length > MAX_NAME_CHARS) return false;
  if (trimmed.split(/\s+/).length > MAX_NAME_WORDS) return false;
  // Prose gives itself away with sentence punctuation; names do not carry it.
  // A bare full stop is NOT enough on its own — "S. SOMASUNDARI" and
  // "R. Suresh Kumar" are initials, and an earlier version of this test threw
  // both names away and fell back to gibberish derived from the address. Only
  // a stop that follows a real word counts as the end of a sentence.
  if (/(?:[,;:!?]|\w{2,}\.)\s/.test(trimmed)) return false;
  return true;
}

/** Turns `mahalakshmiks344@gmail.com` into `Mahalakshmiks`. */
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
    return "Healthcare / Medical";
  if (lowerSkills.includes("finance") || lowerSkills.includes("accounting") || lowerSkills.includes("bank"))
    return "Finance / Banking";
  if (lowerSkills.includes("marketing") || lowerSkills.includes("seo") || lowerSkills.includes("content"))
    return "Marketing";
  if (lowerSkills.includes("design") || lowerSkills.includes("figma") || lowerSkills.includes("ux"))
    return "Design / UX";
  const designation = getDesignation(candidate).toLowerCase();
  if (designation.includes("engineer") || designation.includes("developer") || designation.includes("software"))
    return "IT / Software";
  if (designation.includes("account") || designation.includes("finance") || designation.includes("audit"))
    return "Finance / Banking";
  if (designation.includes("design") || designation.includes("ui") || designation.includes("ux"))
    return "Design / UX";
  if (designation.includes("market") || designation.includes("report") || designation.includes("content"))
    return "Marketing";
  // NOT the company name. Falling back to `current_company` put "Shelby
  // Company Ltd." in the Industry column — an employer is not a sector, and a
  // column that silently changes what it means is worse than an empty one.
  return "General";
}

/** Years as a number so the column sorts numerically; `null` means unknown. */
function getExperienceYears(candidate: CandidateRecord): number | null {
  const years = candidate.profile?.total_experience_years;
  return years === null || years === undefined ? null : years;
}

function formatExperience(years: number | null): string {
  if (years === null) return "—";
  if (years <= 0) return "Fresher";
  // Under a year, months are the honest unit — "0.3 yrs" is a spreadsheet
  // artefact, not something a recruiter would ever say out loud.
  if (years < 1) {
    const months = Math.max(1, Math.round(years * 12));
    return `${months} mo${months === 1 ? "" : "s"}`;
  }
  // Floor, not round: 16.6 years of experience is sixteen completed years.
  // Rounding up would credit the candidate with time they have not served.
  const whole = Math.floor(years);
  return `${whole} yr${whole === 1 ? "" : "s"}`;
}

/**
 * Regroups a bare ten-digit national number so the column stops looking ragged
 * next to the numbers that already carry a country code. Anything with a `+`,
 * brackets or dashes is left exactly as the candidate wrote it — guessing a
 * country code from digit count would invent data.
 */
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

/**
 * The record's own identifier. No longer a column, but kept in the search
 * haystack so a row can still be found by the ID quoted in an email or ticket.
 * This is the resume hash, NOT a passport number.
 */
function getReference(candidate: CandidateRecord): string {
  return (candidate.resume_hash?.slice(0, 8) || candidate.id.slice(-8)).toUpperCase();
}

type StatusKey = "verified" | "review" | "active";

function getStatus(candidate: CandidateRecord): { key: StatusKey; label: string } {
  if (candidate.status === "verified") return { key: "verified", label: "Verified" };
  if ((candidate.profile?.confidence ?? 1) < REVIEW_CONFIDENCE) return { key: "review", label: "Review" };
  return { key: "active", label: "Active" };
}

/**
 * What an empty table means depends on which slice you asked for. "No
 * candidates in the database" is wrong on the Pending Review screen when there
 * are two hundred records and none of them need review — that is good news, and
 * it should read like it.
 */
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
};

/** The three slices, in the order a recruiter works through them. */
const FILTERS: { id: TalentFilter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "verified", label: "Verified" },
  { id: "pending", label: "Pending review" },
];

/** Which DataBlue pill each state wears. */
const STATUS_PILL: Record<StatusKey, string> = {
  verified: "is-verified",
  review: "is-pending",
  active: "is-info",
};

/** Sort weight — review first, because it is the column you act on. */
const STATUS_ORDER: Record<StatusKey, number> = { review: 0, active: 1, verified: 2 };

type SortKey = "name" | "designation" | "industry" | "experience" | "status" | "added";
type SortDir = "asc" | "desc";

type Align = "left" | "center" | "right";

interface Column {
  key: string;
  label: string;
  sort?: SortKey;
  /** Applied to the header cell AND every body cell in the column. */
  align: Align;
  className?: string;
}

/**
 * The single source of truth for the table's columns. Width comes from the
 * matching `.ctable-col-*` rule; alignment is declared once here and applied
 * to both the header and the cells, so the two can never drift apart.
 */
const COLUMNS: Column[] = [
  { key: "index", label: "S.No.", align: "center", className: "ctable-num" },
  { key: "name", label: "Candidate", sort: "name", align: "left" },
  { key: "designation", label: "Designation", sort: "designation", align: "left" },
  { key: "industry", label: "Industry", sort: "industry", align: "left" },
  { key: "experience", label: "Experience", sort: "experience", align: "center", className: "ctable-numeric" },
  { key: "contact", label: "Contact", align: "left" },
  { key: "status", label: "Status", sort: "status", align: "center", className: "ctable-th-status" },
  { key: "actions", label: "Actions", align: "center", className: "ctable-th-actions" },
];

/** Look-up so a body cell can ask for its own column's alignment class. */
const ALIGN: Record<string, string> = Object.fromEntries(
  COLUMNS.map((col) => [col.key, `ctable-al-${col.align}`]),
);

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

  /**
   * Verified and Pending Review are this table with a slice applied, not
   * screens of their own — same columns, same actions, same search, so the
   * directory does not have to be relearned three times.
   */
  const candidates = useMemo(() => {
    if (filter === "verified") return allCandidates.filter((c) => getStatus(c).key === "verified");
    if (filter === "pending") return allCandidates.filter((c) => getStatus(c).key === "review");
    return allCandidates;
  }, [allCandidates, filter]);

  /** Counts sit on the tabs themselves, so the split is visible before you click. */
  const filterCounts = useMemo(
    () => ({
      all: allCandidates.length,
      verified: allCandidates.filter((c) => getStatus(c).key === "verified").length,
      pending: allCandidates.filter((c) => getStatus(c).key === "review").length,
    }),
    [allCandidates],
  );
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
    // Sorting a copy — mutating the filtered array would reorder the memo that
    // produced it and make the next render depend on the previous one.
    return [...filtered].sort((a, b) => {
      switch (sort.key) {
        case "name":
          return factor * getDisplayName(a).localeCompare(getDisplayName(b));
        case "designation":
          return factor * getDesignation(a).localeCompare(getDesignation(b));
        case "industry":
          return factor * getIndustry(a).localeCompare(getIndustry(b));
        case "experience": {
          // Unknowns sort to the bottom in either direction rather than
          // masquerading as zero years of experience.
          const ax = getExperienceYears(a);
          const bx = getExperienceYears(b);
          if (ax === null && bx === null) return 0;
          if (ax === null) return 1;
          if (bx === null) return -1;
          return factor * (ax - bx);
        }
        case "status":
          return factor * (STATUS_ORDER[getStatus(a).key] - STATUS_ORDER[getStatus(b).key]);
        case "added":
        default: {
          const at = a.created_at ? new Date(a.created_at).getTime() : 0;
          const bt = b.created_at ? new Date(b.created_at).getTime() : 0;
          return factor * (at - bt);
        }
      }
    });
  }, [filtered, sort]);

  /** First click sorts ascending; clicking the active column flips direction. */
  const toggleSort = (key: SortKey) =>
    setSort((prev) => (prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));

  return (
    <div className="tab-content active" style={{ animation: "fadeIn 0.3s ease" }}>
      {/* No stat tiles here: the tab counts already report the same three
          figures, and the table is what this screen is for. */}
      <div className="cview-filters">
        <div className="db-tabs" role="tablist" aria-label="Candidate status filter">
          {FILTERS.map(({ id, label }) => (
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

        <div className="db-tabs" role="group" aria-label="Directory layout">
          <button
            type="button"
            className={`db-tab ${view === "table" ? "is-on" : ""}`}
            onClick={() => setView("table")}
            aria-pressed={view === "table"}
            title="Table view"
          >
            <Rows3 size={14} /> Table
          </button>
          <button
            type="button"
            className={`db-tab ${view === "cards" ? "is-on" : ""}`}
            onClick={() => setView("cards")}
            aria-pressed={view === "cards"}
            title="Card view"
          >
            <LayoutGrid size={14} /> Cards
          </button>
        </div>
      </div>

      <div className="sh-toolbar cview-toolbar">
        <div className="sh-toolbar-right">
          <div className="sh-search">
            <Search size={16} className="sh-search-icon" />
            <input
              type="text"
              className="sh-search-input"
              placeholder="Search name, role, industry, skills or contact…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search candidates"
            />
            {query && (
              <button
                className="sourcing-search-clear"
                onClick={() => setQuery("")}
                title="Clear search"
                aria-label="Clear search"
              >
                <X size={14} />
              </button>
            )}
          </div>

          <span className="sh-result-count">
            {formatInt(filtered.length)} of {formatInt(candidates.length)} shown
          </span>
        </div>
      </div>

      {filtered.length === 0 ? (
        <div className="db-empty">
          <UsersRound size={28} strokeWidth={1.5} />
          <span className="db-empty-title">{EMPTY_COPY[filter].title}</span>
          <span className="db-empty-sub">
            {query ? "Nothing here matches that search." : EMPTY_COPY[filter].sub}
          </span>
          {query && (
            <button type="button" className="db-btn" style={{ marginTop: "0.7rem" }} onClick={() => setQuery("")}>
              Clear search
            </button>
          )}
        </div>
      ) : view === "cards" ? (
        <div className="ccard-grid">
          {sorted.map((candidate) => {
            const displayName = getDisplayName(candidate);
            const status = getStatus(candidate);
            const years = getExperienceYears(candidate);
            const logCount = logCounts[candidate.id] ?? 0;
            return (
              <div
                key={candidate.id}
                className="ccard"
                role="button"
                tabIndex={0}
                aria-label={`Open ${displayName}`}
                onClick={() => onOpenCandidate(candidate)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    onOpenCandidate(candidate);
                  }
                }}
              >
                <div className="ccard-head">
                  <span className="ccard-avatar" aria-hidden="true">
                    {initialsOf(displayName)}
                  </span>
                  <span className="ccard-identity">
                    <span className="ccard-name" title={displayName}>
                      {displayName}
                    </span>
                    <span className="ccard-role" title={getDesignation(candidate) || undefined}>
                      {getDesignation(candidate) || getEmail(candidate) || "—"}
                    </span>
                  </span>
                  <span className={`db-pill ${STATUS_PILL[status.key]}`}>{status.label}</span>
                </div>

                <div className="ccard-meta">
                  <span>
                    <span className="ccard-meta-key">Industry</span>
                    <span className="ccard-meta-val">{getIndustry(candidate)}</span>
                  </span>
                  <span>
                    <span className="ccard-meta-key">Experience</span>
                    <span className="ccard-meta-val">{formatExperience(years)}</span>
                  </span>
                  <span>
                    <span className="ccard-meta-key">Contact</span>
                    <span className="ccard-meta-val">{getContact(candidate) || "—"}</span>
                  </span>
                  <span>
                    <span className="ccard-meta-key">Added</span>
                    <span className="ccard-meta-val">
                      {candidate.created_at ? formatDateFull(new Date(candidate.created_at)) : "—"}
                    </span>
                  </span>
                </div>

                {/* The row's action cluster, verbatim — same buttons, same
                    behaviour, so switching view changes the layout and nothing
                    else. Clicks must not also open the card behind them. */}
                <div className="ccard-foot" onClick={(event) => event.stopPropagation()}>
                  <span className="ctable-sub">{getEmail(candidate)}</span>
                  <div className="ctable-actions">
                    <button
                      type="button"
                      className="ctable-btn ctable-btn-edit"
                      title="Edit details"
                      aria-label={`Edit details of ${displayName}`}
                      onClick={() => onEditCandidate(candidate)}
                    >
                      <Edit3 size={14} />
                    </button>
                    <button
                      type="button"
                      className={`ctable-btn ctable-btn-activity ${logCount > 0 ? "is-on" : ""}`}
                      title={
                        logCount > 0
                          ? `Activity log — ${logCount} event${logCount === 1 ? "" : "s"}`
                          : "Activity log"
                      }
                      aria-label={`Activity log for ${displayName}`}
                      onClick={() => onOpenLogs(candidate)}
                    >
                      <MoreHorizontal size={14} />
                    </button>
                    <button
                      type="button"
                      className="ctable-btn ctable-btn-delete"
                      title="Delete candidate"
                      aria-label={`Delete ${displayName}`}
                      onClick={() => {
                        if (
                          confirm(`Permanently delete "${displayName}" from MongoDB Atlas?`)
                        ) {
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
        <div className="ctable-wrap">
          <div className="ctable-scroll">
            <table className="ctable">
              <colgroup>
                <col className="ctable-col-num" />
                <col className="ctable-col-name" />
                <col className="ctable-col-designation" />
                <col className="ctable-col-industry" />
                <col className="ctable-col-experience" />
                <col className="ctable-col-contact" />
                <col className="ctable-col-status" />
                <col className="ctable-col-actions" />
              </colgroup>
              <thead>
                <tr>
                  {COLUMNS.map((col) => (
                    <th key={col.key} className={`${ALIGN[col.key]} ${col.className ?? ""}`}>
                      {col.sort ? (
                        <button
                          type="button"
                          className={`ctable-sort ${sort.key === col.sort ? "is-active" : ""}`}
                          onClick={() => toggleSort(col.sort!)}
                          aria-label={`Sort by ${col.label}`}
                        >
                          {col.label}
                          {sort.key === col.sort && sort.dir === "asc" ? (
                            <ArrowUp size={11} />
                          ) : (
                            <ArrowDown size={11} />
                          )}
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
                  const createdAt = candidate.created_at
                    ? formatDateFull(new Date(candidate.created_at))
                    : "—";
                  const logCount = logCounts[candidate.id] ?? 0;
                  const isConfirmingDelete = deleteConfirm === candidate.id;

                  return (
                    <tr
                      key={candidate.id}
                      className={`ctable-row status-${status.key}`}
                      tabIndex={0}
                      role="button"
                      aria-label={`Open ${displayName}`}
                      onClick={() => onOpenCandidate(candidate)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onOpenCandidate(candidate);
                        }
                      }}
                    >
                      <td className={`${ALIGN.index} ctable-num`}>{index + 1}</td>

                      <td className={ALIGN.name}>
                        <div className="ctable-identity">
                          <span className="ctable-monogram" aria-hidden="true">
                            {initialsOf(displayName)}
                          </span>
                          <span className="ctable-identity-text">
                            <span className="ctable-name">{displayName}</span>
                            <span className="ctable-sub">{email || createdAt}</span>
                          </span>
                        </div>
                      </td>

                      {/* `title` gives the full value back when a cell truncates. */}
                      <td className={ALIGN.designation} title={designation || undefined}>
                        {designation || <span className="ctable-empty-cell">—</span>}
                      </td>

                      <td className={ALIGN.industry} title={industry}>
                        {industry}
                      </td>

                      <td className={`${ALIGN.experience} ctable-numeric`}>{formatExperience(years)}</td>

                      <td className={`${ALIGN.contact} ctable-strong`} title={contact || undefined}>
                        {contact || <span className="ctable-empty-cell">—</span>}
                      </td>

                      <td className={`${ALIGN.status} ctable-cell-status`}>
                        <span className={`db-pill ${STATUS_PILL[status.key]}`}>{status.label}</span>
                      </td>

                      {/* Actions live inside the row but must not trigger it. */}
                      <td
                        className={`${ALIGN.actions} ctable-cell-actions`}
                        onClick={(event) => event.stopPropagation()}
                      >
                        {isConfirmingDelete ? (
                          <div className="ctable-confirm">
                            <span className="ctable-confirm-label">Delete?</span>
                            <button
                              type="button"
                              className="ctable-confirm-yes"
                              title="Confirm delete"
                              aria-label={`Confirm delete of ${displayName}`}
                              onClick={() => {
                                onDeleteCandidate(candidate.id);
                                setDeleteConfirm(null);
                              }}
                            >
                              <Check size={14} />
                            </button>
                            <button
                              type="button"
                              className="ctable-confirm-no"
                              title="Cancel"
                              aria-label="Cancel delete"
                              onClick={() => setDeleteConfirm(null)}
                            >
                              <X size={14} />
                            </button>
                          </div>
                        ) : (
                          <div className="ctable-actions">
                            <button
                              type="button"
                              className="ctable-btn ctable-btn-view"
                              title="View executive profile"
                              aria-label={`View executive profile of ${displayName}`}
                              onClick={() => onOpenCandidate(candidate)}
                            >
                              <Eye size={14} />
                            </button>
                            <button
                              type="button"
                              className="ctable-btn ctable-btn-edit"
                              title="Edit details"
                              aria-label={`Edit details of ${displayName}`}
                              onClick={() => onEditCandidate(candidate)}
                            >
                              <Edit3 size={14} />
                            </button>
                            {/* Opens this candidate's own activity screen. The
                                tint marks a row that has history, so the log
                                can be found without opening every row first. */}
                            <button
                              type="button"
                              className={`ctable-btn ctable-btn-activity ${logCount > 0 ? "is-on" : ""}`}
                              title={
                                logCount > 0
                                  ? `Activity log — ${logCount} event${logCount === 1 ? "" : "s"}`
                                  : "Activity log"
                              }
                              aria-label={`Activity log for ${displayName}`}
                              onClick={() => onOpenLogs(candidate)}
                            >
                              <MoreHorizontal size={14} />
                            </button>
                            <button
                              type="button"
                              className="ctable-btn ctable-btn-delete"
                              title="Delete candidate"
                              aria-label={`Delete ${displayName}`}
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

          <div className="ctable-foot">
            <span>
              Showing <strong>{formatInt(sorted.length)}</strong> of{" "}
              <strong>{formatInt(candidates.length)}</strong> candidates
            </span>
            <span className="ctable-live">
              <span className="status-dot" />
              Live
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
