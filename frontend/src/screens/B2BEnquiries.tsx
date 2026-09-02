"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Briefcase,
  Building2,
  Calendar,
  Check,
  CheckCircle,
  Clock,
  Copy,
  Hash,
  Inbox,
  MapPin,
  MessageSquare,
  Phone,
  Plus,
  Search,
  Trash2,
  User,
  Users,
  X,
  XCircle,
  type LucideIcon,
} from "lucide-react";

import StatTile, { type StatTone } from "@/components/ui/StatTile";
import Select from "@/components/ui/Select";
import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { formatDateFull, formatInt, initialsOf, timeAgo } from "@/lib/format";
import {
  listB2BEnquiriesAPI,
  createB2BEnquiryAPI,
  updateB2BEnquiryAPI,
  convertB2BEnquiryAPI,
  deleteB2BEnquiryAPI,
  type B2BEnquiryRecord,
} from "@/lib/api";
import { CACHE_KEYS, readCache, writeCache } from "@/lib/localCache";

/**
 * B2B Enquiries — what an agent asked for, before it is a job the agency owns.
 *
 * The WhatsApp bot talks to two kinds of people. A candidate answers questions
 * about themselves and lands in Candidates. An *agent* — or an association, or
 * a company hiring under contract — describes a vacancy, and that lands here.
 *
 * The distinction this screen exists to hold is between an enquiry and a job
 * order. An enquiry is a record of a conversation: it is whatever was said,
 * including "about forty, I'll confirm next week". A job order is a commitment
 * the agency has made, with a headcount and a date it is measured against.
 * Converting is the moment a recruiter decides the first has become the second,
 * and it is the only thing on this screen that writes outside it.
 *
 * Nothing here creates a Sourcing Hub record. A number that messaged the bot is
 * not an account the agency has agreed to work with, and quietly making it one
 * would fill that hub with strangers. The two are joined the other way round:
 * an enquiry from a number already on file arrives carrying that party's name.
 */

export type EnquiryStatus = "new" | "reviewing" | "converted" | "closed";

/** `all` is the view, not a state — kept separate so a filter can never be
 *  mistaken for something an enquiry can be. */
type StatusFilter = "all" | EnquiryStatus;
type SortKey = "recent" | "headcount" | "company";

const STATUS_ORDER: EnquiryStatus[] = ["new", "reviewing", "converted", "closed"];

/**
 * What each state means, said in the words a recruiter would use.
 *
 * Colour is the state and nothing else on this screen: blue is waiting on us,
 * green is finished well, slate is finished and came to nothing.
 */
const STATUS_META: Record<EnquiryStatus, { label: string; tone: StatTone; icon: LucideIcon }> = {
  new: { label: "New", tone: "blue", icon: Inbox },
  reviewing: { label: "Reviewing", tone: "yellow", icon: Clock },
  converted: { label: "Converted", tone: "green", icon: CheckCircle },
  closed: { label: "Closed", tone: "slate", icon: XCircle },
};

const STATUS_TABS: { key: StatusFilter; label: string }[] = [
  { key: "all", label: "All" },
  ...STATUS_ORDER.map((key) => ({ key: key as StatusFilter, label: STATUS_META[key].label })),
];

/** The same three words the Sourcing Hub uses, so one record does not call a
 *  party an "agent" while the other calls them a "partner". */
const PARTY_LABEL: Record<string, string> = {
  agent: "Agent",
  association: "Associate",
  client: "Client",
};

const PARTY_ICON: Record<string, React.ReactNode> = {
  agent: <User size={11} />,
  association: <Users size={11} />,
  client: <Briefcase size={11} />,
};

const SORT_LABELS: Record<SortKey, string> = {
  recent: "Most recent",
  headcount: "Largest requirement",
  company: "Company (A–Z)",
};

/**
 * The state a recruiter may move an enquiry into by hand.
 *
 * `converted` is missing on purpose and the API refuses it too: it means a job
 * order exists, and the only thing that can make that true is converting, which
 * writes the order and the status together. A dropdown that offered the word
 * would let someone mark an enquiry finished without the job it claims.
 */
const ASSIGNABLE: EnquiryStatus[] = ["new", "reviewing", "closed"];

function normaliseStatus(raw: string | undefined): EnquiryStatus {
  return STATUS_ORDER.includes(raw as EnquiryStatus) ? (raw as EnquiryStatus) : "new";
}

/** The name to put on the card. A company when there is one, the person who
 *  messaged when there is not — never a phone number as a heading. */
function displayName(enquiry: B2BEnquiryRecord): string {
  return (enquiry.company_name || "").trim() || (enquiry.contact_name || "").trim() || "Unnamed enquiry";
}

/** The line that says what they want, in one sentence.
 *
 *  Assembled from the structured fields when the bot got them, and falling back
 *  to the sender's own words when it did not. An enquiry that went off the
 *  script still has to read as something. */
function requirementLine(enquiry: B2BEnquiryRecord): string {
  const parts: string[] = [];
  if (enquiry.headcount) parts.push(`${formatInt(enquiry.headcount)}×`);
  if (enquiry.job_title) parts.push(enquiry.job_title);
  if (enquiry.destination_country) parts.push(`for ${enquiry.destination_country}`);
  if (parts.length) return parts.join(" ");
  return (enquiry.requirement || "").trim() || "No requirement recorded";
}

function hasValue(value: string | undefined | null): boolean {
  const v = (value || "").trim();
  return v !== "" && v.toUpperCase() !== "N/A";
}

function displayDate(value: string | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : formatDateFull(parsed);
}

/** A due date thirty days out, matching what the Job Orders screen defaults to
 *  when one is raised there — a converted enquiry should not arrive with a
 *  deadline the rest of the product would not have given it. */
function defaultDueDate(): string {
  const d = new Date();
  d.setDate(d.getDate() + 30);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** What a converted enquiry pre-fills the job order with. */
interface ConvertForm {
  title: string;
  client: string;
  headcount: string;
  salary: string;
  skills: string;
  description: string;
  dueDate: string;
}

function convertDefaults(enquiry: B2BEnquiryRecord): ConvertForm {
  return {
    // The job title if the bot captured one; otherwise the recruiter names it,
    // because a requisition called "No requirement recorded" helps nobody.
    title: (enquiry.job_title || "").trim(),
    // Job orders match clients by name — see the Sourcing Hub — so the name
    // already on file wins over whatever the sender typed into WhatsApp.
    client: (enquiry.sourcing_client_name || enquiry.company_name || enquiry.contact_name || "").trim(),
    // Blank rather than 1 when nobody said a number. A pre-filled "1" is a
    // guess that looks like an answer, and it would be committed to.
    headcount: enquiry.headcount ? String(enquiry.headcount) : "",
    salary: (enquiry.salary_budget || "").trim(),
    skills: (enquiry.skills || []).join(", "),
    description: (enquiry.requirement || "").trim(),
    dueDate: defaultDueDate(),
  };
}

interface B2BEnquiriesProps {
  /** Reports what happened, by name, to the dashboard's activity log. */
  onActivity?: (message: string, type?: LogEntry["type"]) => void;
}

export default function B2BEnquiries({ onActivity }: B2BEnquiriesProps) {
  const activity = useCallback(
    (message: string, type: LogEntry["type"] = "info") => onActivity?.(message, type),
    [onActivity],
  );

  const [records, setRecords] = useState<B2BEnquiryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("recent");

  const [openId, setOpenId] = useState<string | null>(null);
  /** The detail dialog has two faces: what was asked for, and the order it is
   *  becoming. One dialog rather than two, because converting is a reading of
   *  the enquiry and the recruiter needs it in front of them while they do it. */
  const [detailMode, setDetailMode] = useState<"read" | "convert">("read");
  const [convertForm, setConvertForm] = useState<ConvertForm | null>(null);

  const [logOpen, setLogOpen] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState("");

  /**
   * When this screen was opened, read once.
   *
   * "Arrived this week" needs a clock, and reading one during render makes the
   * render impure — two renders over the same records can disagree, which is
   * exactly what React is allowed to do and what the compiler assumes it never
   * does. Pinned at mount instead, which is also the honest reading: the figure
   * is as of when the recruiter opened the screen, and it does not tick over
   * under them while they are looking at it.
   */
  const [openedAt] = useState(() => Date.now());

  // ---- loading ----------------------------------------------------------- //
  //
  // The database is the source of truth and an empty response replaces the list
  // outright — it means nobody has enquired, not that the lookup missed. The
  // cache is read only when the request fails, and is never written back to the
  // API: it is a mirror, not a second database.
  //
  // Written as a promise chain rather than an awaited call so nothing is set
  // synchronously inside the effect. `loading` already starts true, so the
  // first fetch has nothing to announce on the way in and only reports on the
  // way out.
  useEffect(() => {
    let active = true;

    listB2BEnquiriesAPI()
      .then((res) => {
        if (!active) return;
        const items = (res.items ?? []).map((item) => ({
          ...item,
          status: normaliseStatus(item.status),
        }));
        setRecords(items);
        writeCache(CACHE_KEYS.b2bEnquiries, items);
        setLoadError("");
      })
      .catch((err: Error) => {
        if (!active) return;
        // Unreachable API — fall back to the last response this browser saw,
        // and say so, because a stale list presented as live is worse than no
        // list at all.
        const cached = readCache<B2BEnquiryRecord>(CACHE_KEYS.b2bEnquiries);
        if (cached) setRecords(cached);
        setLoadError(
          cached
            ? "Showing the last enquiries this browser saw — the server is unreachable."
            : err?.message || "Could not load enquiries.",
        );
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, []);

  // Escape closes whichever dialog is on top, innermost first.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (confirmDeleteId) setConfirmDeleteId(null);
      else if (logOpen) setLogOpen(false);
      else if (detailMode === "convert") setDetailMode("read");
      else if (openId) setOpenId(null);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [confirmDeleteId, logOpen, detailMode, openId]);

  const open = useMemo(
    () => records.find((r) => r.id === openId) ?? null,
    [records, openId],
  );

  // ---- derived ------------------------------------------------------------ //

  const counts = useMemo(() => {
    const map: Record<StatusFilter, number> = {
      all: records.length,
      new: 0,
      reviewing: 0,
      converted: 0,
      closed: 0,
    };
    for (const rec of records) map[normaliseStatus(rec.status)] += 1;
    return map;
  }, [records]);

  /** Seats still being asked for. Converted and closed enquiries are excluded:
   *  a converted one is counted by the job order it became, and counting it
   *  twice would overstate the pipeline by exactly the work already underway. */
  const openSeats = useMemo(
    () =>
      records
        .filter((r) => ["new", "reviewing"].includes(normaliseStatus(r.status)))
        .reduce((sum, r) => sum + (r.headcount || 0), 0),
    [records],
  );

  const arrivedThisWeek = useMemo(() => {
    const cutoff = openedAt - 7 * 86400000;
    return records.filter((r) => {
      const t = r.received_at ? new Date(r.received_at).getTime() : NaN;
      return !Number.isNaN(t) && t >= cutoff;
    }).length;
  }, [records, openedAt]);

  const visible = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();

    const filtered = records.filter((rec) => {
      if (statusFilter !== "all" && normaliseStatus(rec.status) !== statusFilter) return false;
      if (!q) return true;
      return [
        rec.id,
        rec.company_name,
        rec.contact_name,
        rec.phone,
        rec.email,
        rec.job_title,
        rec.destination_country,
        rec.country,
        rec.requirement,
        rec.sourcing_client_name,
        (rec.skills || []).join(" "),
      ]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(q));
    });

    const sorted = [...filtered];
    if (sortKey === "headcount") {
      sorted.sort((a, b) => (b.headcount || 0) - (a.headcount || 0));
    } else if (sortKey === "company") {
      sorted.sort((a, b) => displayName(a).localeCompare(displayName(b)));
    } else {
      sorted.sort(
        (a, b) =>
          new Date(b.received_at || 0).getTime() - new Date(a.received_at || 0).getTime(),
      );
    }
    return sorted;
  }, [records, statusFilter, searchQuery, sortKey]);

  const isFiltered = Boolean(searchQuery.trim()) || statusFilter !== "all";

  const clearFilters = () => {
    setSearchQuery("");
    setStatusFilter("all");
  };

  // ---- actions ------------------------------------------------------------ //

  /** Replace one enquiry in place, so a status change does not reorder the list
   *  under the recruiter who just clicked it. */
  const applyUpdate = useCallback((updated: B2BEnquiryRecord) => {
    setRecords((prev) => {
      const next = prev.map((r) => (r.id === updated.id ? { ...r, ...updated } : r));
      writeCache(CACHE_KEYS.b2bEnquiries, next);
      return next;
    });
  }, []);

  const handleStatus = async (enquiry: B2BEnquiryRecord, status: EnquiryStatus) => {
    if (normaliseStatus(enquiry.status) === status) return;
    setBusy(true);
    setFormError("");
    try {
      const res = await updateB2BEnquiryAPI(enquiry.id, { status });
      applyUpdate(res.enquiry);
      activity(
        `B2B enquiry ${enquiry.id} from ${displayName(enquiry)} marked ${STATUS_META[status].label.toLowerCase()}.`,
        status === "closed" ? "warn" : "info",
      );
    } catch (err) {
      setFormError((err as Error)?.message || "Could not update this enquiry.");
    } finally {
      setBusy(false);
    }
  };

  const handleConvert = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!open || !convertForm) return;

    if (!convertForm.title.trim()) {
      setFormError("The job order needs a title — it is what the client will see.");
      return;
    }
    if (!convertForm.client.trim()) {
      setFormError("The job order needs a client. Job orders are matched to clients by name.");
      return;
    }
    // A requisition for zero seats is FILLED the moment it is raised — see
    // `deriveStatus` in Job Orders — so it would vanish from the list it was
    // raised to appear on. Caught here rather than explained there.
    const headcount = Number.parseInt(convertForm.headcount, 10);
    if (!Number.isFinite(headcount) || headcount < 1) {
      setFormError("How many people? A job order for none is closed the moment it is raised.");
      return;
    }

    setBusy(true);
    setFormError("");
    try {
      const res = await convertB2BEnquiryAPI(open.id, {
        title: convertForm.title.trim(),
        client: convertForm.client.trim(),
        headcount,
        salary: convertForm.salary.trim(),
        skills: convertForm.skills
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        description: convertForm.description.trim(),
        due_date: convertForm.dueDate,
      });
      if (res.enquiry) applyUpdate(res.enquiry);
      activity(
        `B2B enquiry ${open.id} converted into job order ${res.job_order.id} — ${convertForm.title.trim()} for ${convertForm.client.trim()}.`,
        "success",
      );
      setDetailMode("read");
    } catch (err) {
      setFormError((err as Error)?.message || "Could not raise the job order.");
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async (enquiry: B2BEnquiryRecord) => {
    setBusy(true);
    try {
      await deleteB2BEnquiryAPI(enquiry.id);
      setRecords((prev) => {
        const next = prev.filter((r) => r.id !== enquiry.id);
        writeCache(CACHE_KEYS.b2bEnquiries, next);
        return next;
      });
      activity(`Deleted B2B enquiry ${enquiry.id} from ${displayName(enquiry)}.`, "warn");
      setConfirmDeleteId(null);
      if (openId === enquiry.id) setOpenId(null);
    } catch (err) {
      setFormError((err as Error)?.message || "Could not delete this enquiry.");
    } finally {
      setBusy(false);
    }
  };

  const handleCopy = async (enquiry: B2BEnquiryRecord) => {
    const value = hasValue(enquiry.phone) ? enquiry.phone! : enquiry.email || "";
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      setCopiedId(enquiry.id);
      setTimeout(() => setCopiedId((prev) => (prev === enquiry.id ? null : prev)), 1600);
    } catch {
      /* clipboard unavailable (insecure origin) — nothing useful to say */
    }
  };

  const openDetail = (enquiry: B2BEnquiryRecord) => {
    setOpenId(enquiry.id);
    setDetailMode("read");
    setConvertForm(convertDefaults(enquiry));
    setFormError("");
  };

  const closeDetail = () => {
    setOpenId(null);
    setDetailMode("read");
    setFormError("");
  };

  // ---- pieces ------------------------------------------------------------- //

  const renderCard = (enquiry: B2BEnquiryRecord) => {
    const status = normaliseStatus(enquiry.status);
    const meta = STATUS_META[status];
    const StatusIcon = meta.icon;
    const party = (enquiry.party_type || "client").toLowerCase();

    return (
      <article className={`be-card tone-${meta.tone}`} key={enquiry.id}>
        <button
          type="button"
          className="be-card-open"
          onClick={() => openDetail(enquiry)}
          aria-label={`Open enquiry ${enquiry.id} from ${displayName(enquiry)}`}
        >
          <header className="be-card-head">
            <span className="be-monogram">{initialsOf(displayName(enquiry))}</span>
            <div className="be-headings">
              <h3 className="be-name" title={displayName(enquiry)}>
                {displayName(enquiry)}
              </h3>
              <div className="be-meta">
                <span className={`be-party ${party}`}>
                  {PARTY_ICON[party] ?? PARTY_ICON.client}
                  {PARTY_LABEL[party] ?? "Client"}
                </span>
                {/* Where it came in from. A recruiter reads an enquiry the bot
                    took differently from one a colleague typed up after a call
                    — the first is verbatim, the second is already a summary. */}
                <span className={`be-source is-${enquiry.source === "manual" ? "manual" : "bot"}`}>
                  <MessageSquare size={11} />
                  {enquiry.source === "manual" ? "Logged" : "WhatsApp"}
                </span>
                {/* Only when the sender is already a party on file. Absent
                    otherwise, rather than a chip reading "unknown" on the
                    majority of cards. */}
                {enquiry.sourcing_client_name ? (
                  <span className="be-known" title="Already in the Sourcing Hub">
                    <Building2 size={11} />
                    On file
                  </span>
                ) : null}
              </div>
            </div>
            <span className={`be-status is-${status}`}>
              <StatusIcon size={12} />
              {meta.label}
            </span>
          </header>

          {/* The requirement, as one line. The whole of it is in the dialog —
              this is the line that decides whether it gets opened. */}
          <p className="be-requirement" title={enquiry.requirement || undefined}>
            {requirementLine(enquiry)}
          </p>

          <dl className="be-rows">
            <div className="be-row">
              <span className="be-row-icon">
                <User size={13} />
              </span>
              <dt className="be-row-label">Contact</dt>
              <dd className="be-row-value">{enquiry.contact_name || "—"}</dd>
            </div>
            <div className="be-row">
              <span className="be-row-icon">
                <Phone size={13} />
              </span>
              <dt className="be-row-label">Phone</dt>
              <dd className="be-row-value">
                {hasValue(enquiry.phone) ? enquiry.phone : <span className="be-quiet">Not provided</span>}
              </dd>
            </div>
            <div className="be-row">
              <span className="be-row-icon">
                <MapPin size={13} />
              </span>
              <dt className="be-row-label">Destination</dt>
              <dd className="be-row-value">
                {hasValue(enquiry.destination_country) ? (
                  enquiry.destination_country
                ) : (
                  <span className="be-quiet">Not stated</span>
                )}
              </dd>
            </div>
            <div className="be-row">
              <span className="be-row-icon">
                <Calendar size={13} />
              </span>
              <dt className="be-row-label">Needed by</dt>
              <dd className="be-row-value">
                {hasValue(enquiry.needed_by) ? enquiry.needed_by : <span className="be-quiet">Not stated</span>}
              </dd>
            </div>
          </dl>
        </button>

        <footer className="be-card-foot">
          <span className="be-ref" title={enquiry.id}>
            <Hash size={12} />
            {enquiry.id}
          </span>
          <span className="be-when" title={displayDate(enquiry.received_at)}>
            {enquiry.received_at ? timeAgo(enquiry.received_at) : "—"}
          </span>
          <button
            type="button"
            className="be-foot-btn"
            onClick={() => handleCopy(enquiry)}
            title={hasValue(enquiry.phone) ? "Copy phone number" : "Copy email address"}
            aria-label={`Copy contact details for ${displayName(enquiry)}`}
          >
            {copiedId === enquiry.id ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </footer>
      </article>
    );
  };

  const renderEmpty = () => (
    <div className="sh-empty">
      <span className="sh-empty-icon">{isFiltered ? <Search size={26} /> : <Inbox size={26} />}</span>
      {isFiltered ? (
        <>
          <p className="sh-empty-title">Nothing matches these filters</p>
          <span className="sh-empty-note">
            {searchQuery.trim() ? `No enquiry matches “${searchQuery.trim()}”. ` : ""}
            Try widening the search or clearing the filters.
          </span>
          <button className="sh-empty-btn" onClick={clearFilters}>
            Clear filters
          </button>
        </>
      ) : (
        <>
          <p className="sh-empty-title">No B2B enquiries yet</p>
          <span className="sh-empty-note">
            Requirements raised by agents on WhatsApp arrive here on their own. Anything
            that comes in by phone or email can be logged by hand.
          </span>
          <button className="sh-empty-btn primary" onClick={() => setLogOpen(true)}>
            <Plus size={15} />
            Log an enquiry
          </button>
        </>
      )}
    </div>
  );

  const renderSkeleton = () => (
    <div className="be-grid">
      {[0, 1, 2].map((i) => (
        <div className="sh-skeleton" key={i}>
          <div className="sh-skeleton-head">
            <span className="sh-sk-block sh-sk-mono" />
            <div className="sh-sk-lines">
              <span className="sh-sk-block sh-sk-line lg" />
              <span className="sh-sk-block sh-sk-line sm" />
            </div>
          </div>
          <span className="sh-sk-block sh-sk-line" />
          <span className="sh-sk-block sh-sk-line" />
          <span className="sh-sk-block sh-sk-line md" />
          <span className="sh-sk-block sh-sk-bar" />
        </div>
      ))}
    </div>
  );

  // ---- render ------------------------------------------------------------- //

  return (
    <div className="be-root">
      <div className="stat-tiles">
        <StatTile
          tone="blue"
          icon={Inbox}
          label="Awaiting review"
          value={formatInt(counts.new)}
          note={counts.reviewing > 0 ? `${formatInt(counts.reviewing)} already being worked` : "Nothing in progress"}
        />
        <StatTile
          tone={openSeats > 0 ? "navy" : "slate"}
          icon={Users}
          label="Seats requested"
          value={formatInt(openSeats)}
          note="Across enquiries not yet converted or closed"
        />
        <StatTile
          tone="green"
          icon={CheckCircle}
          label="Converted"
          value={formatInt(counts.converted)}
          note="Raised as job orders"
        />
        <StatTile
          tone="slate"
          icon={MessageSquare}
          label="Arrived this week"
          value={formatInt(arrivedThisWeek)}
          note={`${formatInt(counts.all)} enquir${counts.all === 1 ? "y" : "ies"} in total`}
        />
      </div>

      <div className="sh-toolbar">
        <div className="sh-segment" role="tablist" aria-label="Enquiry status">
          {STATUS_TABS.map((tab) => {
            const active = statusFilter === tab.key;
            return (
              <button
                key={tab.key}
                role="tab"
                aria-selected={active}
                className={`sh-segment-btn ${active ? "active" : ""}`}
                onClick={() => setStatusFilter(tab.key)}
              >
                <span>{tab.label}</span>
                <span className="sh-segment-count">{formatInt(counts[tab.key])}</span>
              </button>
            );
          })}
        </div>

        <div className="sh-toolbar-right">
          <div className="sh-search">
            <Search size={16} className="sh-search-icon" />
            <input
              type="text"
              className="sh-search-input"
              placeholder="Search company, contact, job, country…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              aria-label="Search enquiries"
            />
            {searchQuery && (
              <button
                className="sourcing-search-clear"
                onClick={() => setSearchQuery("")}
                title="Clear search"
                aria-label="Clear search"
              >
                <X size={14} />
              </button>
            )}
          </div>

          <Select
            className="sh-sort-select"
            size="sm"
            value={sortKey}
            options={(Object.keys(SORT_LABELS) as SortKey[]).map((key) => ({
              value: key,
              label: SORT_LABELS[key],
            }))}
            onChange={(value) => setSortKey(value as SortKey)}
            ariaLabel="Sort enquiries"
          />

          <button className="sh-new-btn" onClick={() => setLogOpen(true)}>
            <Plus size={16} />
            <span>Log enquiry</span>
          </button>
        </div>
      </div>

      {loadError && (
        <p className="be-banner" role="status">
          <AlertTriangle size={14} />
          {loadError}
        </p>
      )}

      <div className="sh-resultbar">
        <span className="sh-result-count">
          {loading
            ? "Loading enquiries…"
            : `${formatInt(visible.length)} of ${formatInt(records.length)} enquir${records.length === 1 ? "y" : "ies"}`}
        </span>
        {isFiltered && !loading && (
          <button className="sh-clear-link" onClick={clearFilters}>
            <X size={13} />
            Clear filters
          </button>
        )}
      </div>

      {loading ? renderSkeleton() : visible.length === 0 ? renderEmpty() : (
        <div className="be-grid">{visible.map(renderCard)}</div>
      )}

      {open && (
        <EnquiryDialog
          enquiry={open}
          mode={detailMode}
          form={convertForm}
          busy={busy}
          error={formError}
          onClose={closeDetail}
          onSetMode={(mode) => {
            setFormError("");
            if (mode === "convert") setConvertForm(convertDefaults(open));
            setDetailMode(mode);
          }}
          onFormChange={(patch) => setConvertForm((prev) => (prev ? { ...prev, ...patch } : prev))}
          onStatus={(status) => void handleStatus(open, status)}
          onConvert={handleConvert}
          onDelete={() => setConfirmDeleteId(open.id)}
        />
      )}

      {logOpen && (
        <LogEnquiryDialog
          busy={busy}
          onClose={() => setLogOpen(false)}
          onSubmit={async (payload) => {
            setBusy(true);
            setFormError("");
            try {
              const res = await createB2BEnquiryAPI(payload);
              setRecords((prev) => {
                const next = [res.enquiry, ...prev];
                writeCache(CACHE_KEYS.b2bEnquiries, next);
                return next;
              });
              activity(
                `Logged B2B enquiry ${res.enquiry.id} from ${displayName(res.enquiry)}.`,
                "success",
              );
              setLogOpen(false);
              return true;
            } catch (err) {
              return (err as Error)?.message || "Could not log this enquiry.";
            } finally {
              setBusy(false);
            }
          }}
        />
      )}

      {confirmDeleteId && (
        <div className="cm-overlay active" onClick={() => setConfirmDeleteId(null)}>
          <div
            className="cm-dialog be-confirm-dialog"
            onClick={(e) => e.stopPropagation()}
            role="alertdialog"
            aria-modal="true"
            aria-label="Delete enquiry"
          >
            <div className="modal-body">
              <h3 className="be-confirm-title">
                <AlertTriangle size={16} />
                Delete this enquiry?
              </h3>
              <p className="be-confirm-note">
                This removes the record of what was asked for. If the enquiry was real and
                came to nothing, close it instead — a closed enquiry keeps its history.
              </p>
            </div>
            <div className="modal-footer">
              <button className="modal-cancel-btn" onClick={() => setConfirmDeleteId(null)}>
                Cancel
              </button>
              <button
                className="db-btn is-danger"
                disabled={busy}
                onClick={() => {
                  const target = records.find((r) => r.id === confirmDeleteId);
                  if (target) void handleDelete(target);
                }}
              >
                <Trash2 size={14} />
                Delete permanently
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  The detail dialog
//
//  Two faces, one dialog: what was asked for, and the job order it is becoming.
//  Converting is a reading of the enquiry, so the enquiry stays on screen while
//  the recruiter does it rather than being replaced by a form about it.
// --------------------------------------------------------------------------- //

interface EnquiryDialogProps {
  enquiry: B2BEnquiryRecord;
  mode: "read" | "convert";
  form: ConvertForm | null;
  busy: boolean;
  error: string;
  onClose: () => void;
  onSetMode: (mode: "read" | "convert") => void;
  onFormChange: (patch: Partial<ConvertForm>) => void;
  onStatus: (status: EnquiryStatus) => void;
  onConvert: (event: React.FormEvent) => void;
  onDelete: () => void;
}

function EnquiryDialog({
  enquiry,
  mode,
  form,
  busy,
  error,
  onClose,
  onSetMode,
  onFormChange,
  onStatus,
  onConvert,
  onDelete,
}: EnquiryDialogProps) {
  const status = normaliseStatus(enquiry.status);
  const converted = Boolean(enquiry.converted_job_order_id);

  /** Every field worth showing, in the order a recruiter reads them: who, then
   *  what, then when. Rendered from a list so a field that is empty falls out
   *  rather than leaving a labelled blank. */
  const facts: { label: string; value: React.ReactNode }[] = [
    { label: "Contact", value: enquiry.contact_name },
    { label: "Company", value: enquiry.company_name },
    {
      label: "Phone",
      value: hasValue(enquiry.phone) ? (
        <a className="sh-link" href={`tel:${(enquiry.phone || "").replace(/\s+/g, "")}`}>
          {enquiry.phone}
        </a>
      ) : null,
    },
    {
      label: "Email",
      value: hasValue(enquiry.email) ? (
        <a className="sh-link" href={`mailto:${enquiry.email}`}>
          {enquiry.email}
        </a>
      ) : null,
    },
    { label: "Based in", value: [enquiry.city, enquiry.country].filter(Boolean).join(", ") },
    { label: "Job", value: enquiry.job_title },
    { label: "Headcount", value: enquiry.headcount ? formatInt(enquiry.headcount) : null },
    { label: "Destination", value: enquiry.destination_country },
    { label: "Experience", value: enquiry.experience_required },
    { label: "Budget", value: enquiry.salary_budget },
    { label: "Needed by", value: enquiry.needed_by },
    { label: "Skills", value: (enquiry.skills || []).join(", ") },
    { label: "Received", value: displayDate(enquiry.received_at) },
    { label: "Handled by", value: enquiry.handled_by },
  ].filter((fact) => {
    if (fact.value === null || fact.value === undefined) return false;
    return typeof fact.value === "string" ? fact.value.trim() !== "" : true;
  });

  return (
    <div className="cm-overlay active" onClick={onClose}>
      <div
        className="cm-dialog be-dialog"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={`Enquiry ${enquiry.id}`}
      >
        <div className="be-dialog-head">
          <div className="be-dialog-headings">
            <span className="be-dialog-eyebrow">
              <Hash size={12} />
              {enquiry.id}
              <span className={`be-status is-${status}`}>{STATUS_META[status].label}</span>
            </span>
            <h3 className="be-dialog-title">{displayName(enquiry)}</h3>
            <p className="be-dialog-sub">
              {mode === "convert"
                ? "Raise the job order this enquiry asked for. What you enter here is what the agency commits to."
                : requirementLine(enquiry)}
            </p>
          </div>
          <button className="sh-modal-close" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>

        {mode === "read" ? (
          <>
            <div className="modal-body be-dialog-body">
              {/* Their own words, first and largest. Every structured field
                  below it was pulled out of this by the bot, and where the two
                  disagree this is the one that was actually said. */}
              {hasValue(enquiry.requirement) && (
                <section className="be-quote">
                  <span className="be-quote-label">What they asked for</span>
                  <p className="be-quote-body">{enquiry.requirement}</p>
                </section>
              )}

              <dl className="be-facts">
                {facts.map((fact) => (
                  <div className="be-fact" key={fact.label}>
                    <dt className="be-fact-label">{fact.label}</dt>
                    <dd className="be-fact-value">{fact.value}</dd>
                  </div>
                ))}
              </dl>

              {hasValue(enquiry.notes) && (
                <section className="be-quote is-note">
                  <span className="be-quote-label">Notes</span>
                  <p className="be-quote-body">{enquiry.notes}</p>
                </section>
              )}

              {converted && (
                <p className="be-linked" role="status">
                  <Briefcase size={14} />
                  Raised as job order <strong>{enquiry.converted_job_order_id}</strong>. Open Job
                  Orders to work it.
                </p>
              )}

              {error && (
                <p className="sh-form-error" role="alert">
                  <AlertTriangle size={14} />
                  {error}
                </p>
              )}
            </div>

            <div className="modal-footer be-dialog-foot">
              <button className="be-danger-link" onClick={onDelete} disabled={busy}>
                <Trash2 size={14} />
                Delete
              </button>

              <div className="be-foot-actions">
                {/*
                  The states a person may set. Two absences, for two reasons.

                  `converted` is not in the list because it is not a state you
                  choose — it is what converting makes true, alongside the order
                  id, and the API refuses the word on its own.

                  And a converted enquiry offers none of them. Moving it back to
                  "new" would leave it reading unhandled while still pointing at
                  a live job order, and the button beside it would stay disabled
                  with no explanation on the row — the enquiry is finished, and
                  the work has moved to the order named below.
                */}
                {!converted &&
                  ASSIGNABLE.filter((s) => s !== status).map((s) => (
                    <button
                      key={s}
                      className="modal-cancel-btn"
                      disabled={busy}
                      onClick={() => onStatus(s)}
                    >
                      {s === "new" ? "Reopen" : s === "reviewing" ? "Start review" : "Close"}
                    </button>
                  ))}

                <button
                  className="db-btn is-primary"
                  disabled={busy || converted}
                  onClick={() => onSetMode("convert")}
                  title={
                    converted
                      ? `Already converted into ${enquiry.converted_job_order_id}`
                      : "Raise a job order from this enquiry"
                  }
                >
                  <ArrowRight size={15} />
                  {converted ? "Already converted" : "Convert to job order"}
                </button>
              </div>
            </div>
          </>
        ) : (
          <form onSubmit={onConvert}>
            <div className="modal-body be-dialog-body">
              <div className="modal-row-2">
                <div className="sh-field">
                  <label className="modal-label" htmlFor="be-title">
                    Job title *
                  </label>
                  <input
                    id="be-title"
                    className="modal-input"
                    value={form?.title ?? ""}
                    onChange={(e) => onFormChange({ title: e.target.value })}
                    placeholder="e.g. Structural Welder"
                    required
                  />
                </div>
                <div className="sh-field">
                  <label className="modal-label" htmlFor="be-client">
                    Client *
                  </label>
                  <input
                    id="be-client"
                    className="modal-input"
                    value={form?.client ?? ""}
                    onChange={(e) => onFormChange({ client: e.target.value })}
                    placeholder="e.g. Gulf Steel Works"
                    required
                  />
                  {/* Job orders match clients by name, so the two screens have
                      to agree on the spelling or the order rolls up under
                      nobody. Said here rather than discovered there. */}
                  <span className="be-hint">
                    Matched to the Sourcing Hub by name — use the name already on file.
                  </span>
                </div>
              </div>

              <div className="modal-row-2">
                <div className="sh-field">
                  <label className="modal-label" htmlFor="be-headcount">
                    Headcount *
                  </label>
                  <input
                    id="be-headcount"
                    type="number"
                    min={1}
                    className="modal-input"
                    value={form?.headcount ?? ""}
                    onChange={(e) => onFormChange({ headcount: e.target.value })}
                    placeholder="e.g. 40"
                    required
                  />
                </div>
                <div className="sh-field">
                  <label className="modal-label" htmlFor="be-due">
                    Due date
                  </label>
                  <input
                    id="be-due"
                    type="date"
                    className="modal-input"
                    value={form?.dueDate ?? ""}
                    onChange={(e) => onFormChange({ dueDate: e.target.value })}
                  />
                </div>
              </div>

              <div className="modal-row-2">
                <div className="sh-field">
                  <label className="modal-label" htmlFor="be-salary">
                    Salary
                  </label>
                  <input
                    id="be-salary"
                    className="modal-input"
                    value={form?.salary ?? ""}
                    onChange={(e) => onFormChange({ salary: e.target.value })}
                    placeholder="e.g. QAR 2,200/month"
                  />
                </div>
                <div className="sh-field">
                  <label className="modal-label" htmlFor="be-skills">
                    Skills
                  </label>
                  <input
                    id="be-skills"
                    className="modal-input"
                    value={form?.skills ?? ""}
                    onChange={(e) => onFormChange({ skills: e.target.value })}
                    placeholder="Comma separated"
                  />
                </div>
              </div>

              <div className="sh-field">
                <label className="modal-label" htmlFor="be-description">
                  Description
                </label>
                <textarea
                  id="be-description"
                  className="modal-textarea"
                  value={form?.description ?? ""}
                  onChange={(e) => onFormChange({ description: e.target.value })}
                  placeholder="What the client needs, in the agency's words."
                />
                <span className="be-hint">
                  Pre-filled with what the agent said. Edit it into something a recruiter
                  working the order can act on.
                </span>
              </div>

              {error && (
                <p className="sh-form-error" role="alert">
                  <AlertTriangle size={14} />
                  {error}
                </p>
              )}
            </div>

            <div className="modal-footer">
              <button
                type="button"
                className="modal-cancel-btn"
                onClick={() => onSetMode("read")}
                disabled={busy}
              >
                Back to enquiry
              </button>
              <button type="submit" className="db-btn is-primary" disabled={busy}>
                <Briefcase size={15} />
                {busy ? "Raising…" : "Raise job order"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  Logging one by hand
//
//  For the enquiry that came in by phone or by email. Fewer fields than the bot
//  collects, deliberately: a person typing this up after a call has the one
//  sentence that matters and should not be held at a form asking for twelve.
// --------------------------------------------------------------------------- //

interface LogEnquiryDialogProps {
  busy: boolean;
  onClose: () => void;
  /** Resolves `true` on success, or the message to show on failure. */
  onSubmit: (payload: Partial<B2BEnquiryRecord>) => Promise<true | string>;
}

function LogEnquiryDialog({ busy, onClose, onSubmit }: LogEnquiryDialogProps) {
  const [partyType, setPartyType] = useState("agent");
  const [companyName, setCompanyName] = useState("");
  const [contactName, setContactName] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [jobTitle, setJobTitle] = useState("");
  const [headcount, setHeadcount] = useState("");
  const [destination, setDestination] = useState("");
  const [neededBy, setNeededBy] = useState("");
  const [requirement, setRequirement] = useState("");
  const [error, setError] = useState("");

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!contactName.trim()) {
      setError("Who called? A row with no name on it cannot be chased.");
      return;
    }
    if (!requirement.trim() && !jobTitle.trim()) {
      setError("What did they ask for? Either a job title or a sentence describing it.");
      return;
    }

    const parsed = Number.parseInt(headcount, 10);
    const result = await onSubmit({
      party_type: partyType,
      company_name: companyName.trim(),
      contact_name: contactName.trim(),
      phone: phone.trim(),
      email: email.trim(),
      job_title: jobTitle.trim(),
      // Omitted rather than sent as 0 when nobody gave a number — the API
      // stores absence as absence and the screen says "Not stated".
      ...(Number.isFinite(parsed) && parsed > 0 ? { headcount: parsed } : {}),
      destination_country: destination.trim(),
      needed_by: neededBy.trim(),
      requirement: requirement.trim(),
    });
    if (result !== true) setError(result);
  };

  return (
    <div className="cm-overlay active" onClick={onClose}>
      <div
        className="cm-dialog sh-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="Log a B2B enquiry"
      >
        <div className="sh-modal-head">
          <div>
            <h3 className="sh-modal-title">Log a B2B enquiry</h3>
            <p className="sh-modal-sub">
              For a requirement that came in by phone or email. Anything the bot takes on
              WhatsApp arrives here on its own.
            </p>
          </div>
          <button className="sh-modal-close" onClick={onClose} aria-label="Close">
            <X size={18} />
          </button>
        </div>

        <form onSubmit={submit}>
          <div className="modal-body sh-modal-body">
            <div className="modal-row-2">
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-type">
                  Type
                </label>
                <Select
                  id="be-log-type"
                  value={partyType}
                  options={[
                    { value: "agent", label: "Agent", hint: "Introduces candidates" },
                    { value: "association", label: "Associate", hint: "Represents a membership" },
                    { value: "client", label: "Client", hint: "A company hiring under contract" },
                  ]}
                  onChange={setPartyType}
                  ariaLabel="Enquiry party type"
                />
              </div>
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-company">
                  Company
                </label>
                <input
                  id="be-log-company"
                  className="modal-input"
                  value={companyName}
                  onChange={(e) => setCompanyName(e.target.value)}
                  placeholder="e.g. Gulf Steel Works"
                />
              </div>
            </div>

            <div className="modal-row-2">
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-contact">
                  Contact person *
                </label>
                <input
                  id="be-log-contact"
                  className="modal-input"
                  value={contactName}
                  onChange={(e) => setContactName(e.target.value)}
                  placeholder="e.g. Jane Doe"
                  required
                />
              </div>
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-phone">
                  Phone
                </label>
                <input
                  id="be-log-phone"
                  type="tel"
                  className="modal-input"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  placeholder="e.g. +974 3312 4455"
                />
                <span className="be-hint">
                  Matched against the Sourcing Hub, so a party already on file is named
                  rather than shown as a number.
                </span>
              </div>
            </div>

            <div className="sh-field">
              <label className="modal-label" htmlFor="be-log-email">
                Email
              </label>
              <input
                id="be-log-email"
                type="email"
                className="modal-input"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="e.g. hr@company.com"
              />
            </div>

            <div className="modal-row-2">
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-job">
                  Job title
                </label>
                <input
                  id="be-log-job"
                  className="modal-input"
                  value={jobTitle}
                  onChange={(e) => setJobTitle(e.target.value)}
                  placeholder="e.g. Structural Welder"
                />
              </div>
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-headcount">
                  Headcount
                </label>
                <input
                  id="be-log-headcount"
                  type="number"
                  min={1}
                  className="modal-input"
                  value={headcount}
                  onChange={(e) => setHeadcount(e.target.value)}
                  placeholder="Leave blank if they did not say"
                />
              </div>
            </div>

            <div className="modal-row-2">
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-destination">
                  Destination country
                </label>
                <input
                  id="be-log-destination"
                  className="modal-input"
                  value={destination}
                  onChange={(e) => setDestination(e.target.value)}
                  placeholder="e.g. Qatar"
                />
              </div>
              <div className="sh-field">
                <label className="modal-label" htmlFor="be-log-needed">
                  Needed by
                </label>
                <input
                  id="be-log-needed"
                  className="modal-input"
                  value={neededBy}
                  onChange={(e) => setNeededBy(e.target.value)}
                  placeholder="e.g. before Ramadan"
                />
              </div>
            </div>

            <div className="sh-field">
              <label className="modal-label" htmlFor="be-log-requirement">
                What they asked for
              </label>
              <textarea
                id="be-log-requirement"
                className="modal-textarea"
                value={requirement}
                onChange={(e) => setRequirement(e.target.value)}
                placeholder="Their words, as close as you can get them."
              />
            </div>

            {error && (
              <p className="sh-form-error" role="alert">
                <AlertTriangle size={14} />
                {error}
              </p>
            )}
          </div>

          <div className="modal-footer">
            <button type="button" className="modal-cancel-btn" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="db-btn is-primary" disabled={busy}>
              <Plus size={15} />
              {busy ? "Saving…" : "Log enquiry"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
