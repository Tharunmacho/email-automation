"use client";

/**
 * Mapped Candidates: every candidate mapped to a job order, and where each
 * one is in the steps from résumé to travel.
 *
 * Laid out like All Candidates on purpose — four readings that are also the
 * filter, one panel holding the rows, the same search and the same filter
 * dropdowns — so it reads as the same product. The rows are pairs: a job order
 * and the candidate mapped to it. Clicking one opens that candidate's steps.
 *
 * The API decides who appears here (staff see the candidates allocated to them
 * and nobody else's) and which step may be answered; this screen only draws
 * what it is sent.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronLeft,
  ChevronRight,
  CircleX,
  Layers,
  PlaneTakeoff,
  RefreshCw,
  Route,
  Rows3,
  Search,
  UserCheck,
  Workflow,
  X,
  type LucideIcon,
} from "lucide-react";

import Select from "@/components/ui/Select";
import MappingPipelineDialog from "@/components/mapping/MappingPipelineDialog";
import MappingProgress from "@/components/mapping/MappingProgress";
import {
  MAPPING_STAGES,
  STAGE_META,
  formatMoment,
  lastActivity,
  progressRank,
} from "@/components/mapping/stages";
import { formatInt, initialsOf } from "@/lib/format";
import { listMappedCandidates, type MappedCandidateRow } from "@/lib/api";
import {
  formatExperience,
  getContact,
  getDesignation,
  getDisplayName,
  getEmail,
  getExperienceYears,
  getIndustry,
  getReference,
} from "@/screens/CandidatesView";

type MapFilter = "all" | "active" | "travelled" | "closed";
type SortKey = "order" | "candidate" | "progress" | "updated";
type SortDir = "asc" | "desc";

const FILTERS: { id: MapFilter; label: string; icon: LucideIcon }[] = [
  { id: "all", label: "All mapped", icon: Route },
  { id: "active", label: "In progress", icon: Workflow },
  { id: "travelled", label: "Travelled", icon: PlaneTakeoff },
  { id: "closed", label: "Closed", icon: CircleX },
];

const EMPTY_COPY: Record<MapFilter, { title: string; sub: string }> = {
  all: {
    title: "No mapped candidates yet",
    sub: "Candidates appear here once they are submitted against a job order.",
  },
  active: { title: "Nothing in progress", sub: "Every mapping has either travelled or closed." },
  travelled: { title: "Nobody has travelled yet", sub: "Candidates land here once the travel step is recorded." },
  closed: { title: "No closed mappings", sub: "A rejection at selection or offer closes a mapping." },
};

const COLUMNS: { key: string; label: string; sort?: SortKey }[] = [
  { key: "order", label: "Job order", sort: "order" },
  { key: "candidate", label: "Candidate", sort: "candidate" },
  { key: "owner", label: "Assigned to" },
  { key: "progress", label: "Progress & status", sort: "progress" },
  { key: "updated", label: "Last update", sort: "updated" },
];

function filterOf(row: MappedCandidateRow): Exclude<MapFilter, "all"> {
  if (row.mapping.status === "completed") return "travelled";
  if (row.mapping.status === "closed") return "closed";
  return "active";
}

function orderLabel(row: MappedCandidateRow): string {
  const order = row.job_order;
  if (order.missing) return `Deleted order · ${order.id}`;
  return [order.title || "Untitled order", order.client].filter(Boolean).join(" · ");
}

interface Props {
  /** The signed-in account: staff are scoped by the API, and cannot skip steps. */
  role: string;
  onToast: (message: string, type?: "success" | "error" | "info") => void;
  onOpenProfile?: (candidateId: string) => void;
  /** Bumped by the shell on realtime events, to re-read without polling. */
  refreshKey?: number;
}

export default function MappingCandidatesScreen({ role, onToast, onOpenProfile, refreshKey = 0 }: Props) {
  const isStaff = role === "staff";
  const canSkip = role === "admin" || role === "manager" || role === "finance_manager";

  const [rows, setRows] = useState<MappedCandidateRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [reloadNonce, setReloadNonce] = useState(0);

  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<MapFilter>("all");
  const [orderFilter, setOrderFilter] = useState("all");
  const [stageFilter, setStageFilter] = useState("all");
  const [industryFilter, setIndustryFilter] = useState("all");
  const [experienceFilter, setExperienceFilter] = useState("all");
  const [ownerFilter, setOwnerFilter] = useState("all");
  const [view, setView] = useState<"table" | "grouped">("table");
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: "updated", dir: "desc" });
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(12);
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    listMappedCandidates()
      .then((response) => {
        if (!active) return;
        setRows(response.items ?? []);
        setLoadError("");
      })
      .catch((error) => {
        if (active) setLoadError(error instanceof Error ? error.message : "Could not load mapped candidates.");
      })
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [refreshKey, reloadNonce]);

  const reload = () => {
    setLoading(true);
    setReloadNonce((value) => value + 1);
  };

  const replaceRow = useCallback((updated: MappedCandidateRow) => {
    setRows((list) => list.map((row) => (row.id === updated.id ? updated : row)));
  }, []);

  const counts = useMemo(() => {
    const out = { all: rows.length, active: 0, travelled: 0, closed: 0 };
    for (const row of rows) out[filterOf(row)] += 1;
    return out;
  }, [rows]);

  const footnotes = useMemo<Record<MapFilter, string>>(() => {
    const orders = new Set(rows.map((row) => row.job_order.id)).size;
    const late = rows.filter(
      (row) => row.mapping.status === "in_progress" && ["visa", "travel"].includes(row.mapping.current_stage ?? ""),
    ).length;
    const unfit = rows.filter(
      (row) => row.mapping.current_stage === "visa"
        && row.mapping.stages.find((step) => step.key === "visa")?.entry?.medical === "unfit",
    ).length;
    return {
      all: `Across ${formatInt(orders)} job order${orders === 1 ? "" : "s"}`,
      active: `${formatInt(late)} at visa or travel${unfit ? ` · ${formatInt(unfit)} unfit` : ""}`,
      travelled: "All six steps complete",
      closed: "Returned to the available pool",
    };
  }, [rows]);

  const scoped = useMemo(
    () => (filter === "all" ? rows : rows.filter((row) => filterOf(row) === filter)),
    [filter, rows],
  );

  const orderOptions = useMemo(() => {
    const seen = new Map<string, string>();
    for (const row of rows) if (!seen.has(row.job_order.id)) seen.set(row.job_order.id, orderLabel(row));
    return [...seen.entries()]
      .sort((a, b) => a[1].localeCompare(b[1]))
      .map(([value, label]) => ({ value, label, hint: value }));
  }, [rows]);

  const industryOptions = useMemo(
    () => Array.from(new Set(rows.map(getIndustry))).sort((a, b) => a.localeCompare(b)),
    [rows],
  );

  const ownerOptions = useMemo(
    () => Array.from(new Set(rows.map((row) => row.assigned_staff_name).filter(Boolean) as string[]))
      .sort((a, b) => a.localeCompare(b)),
    [rows],
  );

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return scoped.filter((row) => {
      if (orderFilter !== "all" && row.job_order.id !== orderFilter) return false;
      if (stageFilter === "unfit") {
        const visa = row.mapping.stages.find((step) => step.key === "visa");
        if (row.mapping.current_stage !== "visa" || visa?.entry?.medical !== "unfit") return false;
      } else if (stageFilter !== "all" && row.mapping.current_stage !== stageFilter) {
        return false;
      }
      if (!isStaff && ownerFilter !== "all" && row.assigned_staff_name !== ownerFilter) return false;
      if (industryFilter !== "all" && getIndustry(row) !== industryFilter) return false;

      // The same experience bands as All Candidates.
      const years = getExperienceYears(row) ?? 0;
      if (experienceFilter === "fresher" && years >= 1) return false;
      if (experienceFilter === "junior" && (years < 1 || years > 3)) return false;
      if (experienceFilter === "mid" && (years < 4 || years > 7)) return false;
      if (experienceFilter === "senior" && years < 8) return false;

      if (!needle) return true;
      const profile = row.profile ?? {};
      const travel = row.mapping.stages.find((step) => step.key === "travel")?.entry;
      const haystack = [
        getDisplayName(row),
        getDesignation(row),
        getIndustry(row),
        profile.location ?? "",
        (profile.skills ?? []).join(" "),
        getContact(row),
        getEmail(row),
        getReference(row),
        row.job_order.id,
        row.job_order.title ?? "",
        row.job_order.client ?? "",
        row.job_order.destinationCountry ?? "",
        row.assigned_staff_name ?? "",
        travel?.flight_number ?? "",
      ]
        .join(" ")
        .toLowerCase();
      return haystack.includes(needle);
    });
  }, [experienceFilter, industryFilter, isStaff, orderFilter, ownerFilter, query, scoped, stageFilter]);

  const sorted = useMemo(() => {
    const factor = sort.dir === "asc" ? 1 : -1;
    const compare = (a: MappedCandidateRow, b: MappedCandidateRow) => {
      switch (sort.key) {
        case "order":
          return factor * orderLabel(a).localeCompare(orderLabel(b));
        case "candidate":
          return factor * getDisplayName(a).localeCompare(getDisplayName(b));
        case "progress":
          return factor * (progressRank(a) - progressRank(b));
        case "updated":
        default: {
          const at = new Date(lastActivity(a) ?? 0).getTime() || 0;
          const bt = new Date(lastActivity(b) ?? 0).getTime() || 0;
          return factor * (at - bt);
        }
      }
    };
    return [...filtered].sort((a, b) =>
      // Grouped view keeps each order's candidates together, then applies the
      // chosen sort inside the group.
      view === "grouped"
        ? orderLabel(a).localeCompare(orderLabel(b)) || a.job_order.id.localeCompare(b.job_order.id) || compare(a, b)
        : compare(a, b),
    );
  }, [filtered, sort, view]);

  const groupSizes = useMemo(() => {
    const sizes = new Map<string, number>();
    for (const row of sorted) sizes.set(row.job_order.id, (sizes.get(row.job_order.id) ?? 0) + 1);
    return sizes;
  }, [sorted]);

  const pageCount = Math.max(1, Math.ceil(sorted.length / pageSize));
  const safePage = Math.min(currentPage, pageCount);
  const pageStart = (safePage - 1) * pageSize;
  const visible = sorted.slice(pageStart, pageStart + pageSize);
  const activeOptionFilters = [
    orderFilter !== "all",
    stageFilter !== "all",
    industryFilter !== "all",
    experienceFilter !== "all",
    !isStaff && ownerFilter !== "all",
  ].filter(Boolean).length;

  const toggleSort = (key: SortKey) =>
    setSort((prev) =>
      prev.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" },
    );

  const resetPage = () => setCurrentPage(1);
  const openRow = rows.find((row) => row.id === openId) ?? null;
  const activeLabel = FILTERS.find((item) => item.id === filter)?.label ?? "All mapped";

  return (
    <div className="ds-page map-page">
      <header className="ds-head">
        <div>
          <h1 className="ds-head-title">Mapped Candidates</h1>
          <p className="ds-head-sub">
            {isStaff
              ? "Your allocated candidates mapped to job orders, and the step each one is on"
              : "Every candidate mapped to a job order, from résumé submission to travel"}
          </p>
        </div>
        <div className="ds-head-actions">
          <button type="button" className="ds-ghost-btn" onClick={reload} disabled={loading}>
            <RefreshCw size={15} className={loading ? "icon-spin" : undefined} /> Refresh
          </button>
        </div>
      </header>

      <div className="ds-stats" role="tablist" aria-label="Filter mapped candidates">
        {FILTERS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={filter === id}
            className={`ds-stat ${filter === id ? "is-on" : ""}`}
            onClick={() => { setFilter(id); resetPage(); }}
          >
            <span className="ds-stat-top">
              <span className="ds-stat-label">{label}</span>
              <span className="ds-stat-icon" aria-hidden="true"><Icon size={16} strokeWidth={2} /></span>
            </span>
            <span className="ds-stat-value">{loading && rows.length === 0 ? "—" : formatInt(counts[id])}</span>
            <span className="ds-stat-foot"><em className="ds-stat-quiet">{footnotes[id]}</em></span>
          </button>
        ))}
      </div>

      <section className="ds-panel">
        <div className="ds-panel-head is-split">
          <div>
            <h2 className="ds-panel-title">{activeLabel}</h2>
            <p className="ds-panel-sub">
              {formatInt(sorted.length)} matching of {formatInt(scoped.length)} mapped
            </p>
          </div>

          <div className="ds-panel-tools">
            <label className="ds-search">
              <Search size={15} />
              <input
                type="search"
                value={query}
                placeholder="Search candidate, job order, client or flight"
                onChange={(event) => { setQuery(event.target.value); resetPage(); }}
                aria-label="Search mapped candidates"
              />
            </label>

            <div className="ds-seg is-quiet" role="group" aria-label="Layout">
              <button
                type="button"
                className={`ds-seg-btn is-icon ${view === "table" ? "is-on" : ""}`}
                onClick={() => setView("table")}
                title="One row per candidate"
                aria-label="One row per candidate"
                aria-pressed={view === "table"}
              >
                <Rows3 size={15} />
              </button>
              <button
                type="button"
                className={`ds-seg-btn is-icon ${view === "grouped" ? "is-on" : ""}`}
                onClick={() => { setView("grouped"); resetPage(); }}
                title="Group by job order"
                aria-label="Group by job order"
                aria-pressed={view === "grouped"}
              >
                <Layers size={15} />
              </button>
            </div>
          </div>
        </div>

        <div className="ds-filterbar" aria-label="Mapped candidate options">
          <span className="ds-filterbar-label">Filters{activeOptionFilters ? ` (${activeOptionFilters})` : ""}</span>
          <Select
            size="sm"
            value={orderFilter}
            options={[{ value: "all", label: "All job orders" }, ...orderOptions]}
            onChange={(value) => { setOrderFilter(value); resetPage(); }}
            ariaLabel="Job order"
          />
          <Select
            size="sm"
            value={stageFilter}
            options={[
              { value: "all", label: "Any step" },
              ...MAPPING_STAGES.map((stage) => ({ value: stage, label: `At: ${STAGE_META[stage].title}` })),
              { value: "unfit", label: "Medically unfit", hint: "Held at the visa step" },
            ]}
            onChange={(value) => { setStageFilter(value); resetPage(); }}
            ariaLabel="Current step"
          />
          <Select
            size="sm"
            value={industryFilter}
            options={[
              { value: "all", label: "All industries" },
              ...industryOptions.map((industry) => ({ value: industry, label: industry })),
            ]}
            onChange={(value) => { setIndustryFilter(value); resetPage(); }}
            ariaLabel="Candidate industry"
          />
          <Select
            size="sm"
            value={experienceFilter}
            options={[
              { value: "all", label: "Any experience" },
              { value: "fresher", label: "Fresher / under 1 year" },
              { value: "junior", label: "1–3 years" },
              { value: "mid", label: "4–7 years" },
              { value: "senior", label: "8+ years" },
            ]}
            onChange={(value) => { setExperienceFilter(value); resetPage(); }}
            ariaLabel="Candidate experience"
          />
          {!isStaff && ownerOptions.length > 0 && (
            <Select
              size="sm"
              value={ownerFilter}
              options={[
                { value: "all", label: "All owners" },
                ...ownerOptions.map((owner) => ({ value: owner, label: owner })),
              ]}
              onChange={(value) => { setOwnerFilter(value); resetPage(); }}
              ariaLabel="Candidate owner"
            />
          )}
          {activeOptionFilters > 0 && (
            <button
              type="button"
              className="ds-filter-clear"
              onClick={() => {
                setOrderFilter("all");
                setStageFilter("all");
                setIndustryFilter("all");
                setExperienceFilter("all");
                setOwnerFilter("all");
                resetPage();
              }}
            >
              <X size={13} /> Clear
            </button>
          )}
        </div>

        {loading && rows.length === 0 ? (
          <div className="map-skeleton" role="status" aria-label="Loading mapped candidates">
            {[0, 1, 2, 3].map((item) => <div key={item} className="dash-skel" />)}
          </div>
        ) : loadError && rows.length === 0 ? (
          <div className="ds-empty-state">
            <CircleX size={30} />
            <h3>Mapped candidates could not be loaded</h3>
            <p>{loadError}</p>
            <button type="button" className="ds-ghost-btn" onClick={reload}>Try again</button>
          </div>
        ) : sorted.length === 0 ? (
          <div className="ds-empty-state">
            <Route size={30} />
            <h3>{query ? "Nothing matches that search" : EMPTY_COPY[filter].title}</h3>
            <p>
              {query
                ? "No mapped candidate or job order matches what you typed."
                : isStaff && filter === "all"
                  ? "Your allocated candidates appear here once they are mapped to a job order."
                  : EMPTY_COPY[filter].sub}
            </p>
            {query && (
              <button type="button" className="ds-ghost-btn" onClick={() => setQuery("")}>Clear search</button>
            )}
          </div>
        ) : (
          <div className="ds-table-wrap is-ruled map-table-wrap">
            <table className="ds-table is-ruled map-table">
              <thead>
                <tr>
                  {COLUMNS.map((col) => (
                    <th key={col.key}>
                      {col.sort ? (
                        <button
                          type="button"
                          className={`ds-sort ${sort.key === col.sort ? "is-on" : ""}`}
                          onClick={() => toggleSort(col.sort!)}
                        >
                          {col.label}
                          {sort.key === col.sort && (sort.dir === "asc" ? <ArrowUp size={11} /> : <ArrowDown size={11} />)}
                        </button>
                      ) : (
                        col.label
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visible.map((row, index) => {
                  const name = getDisplayName(row);
                  const order = row.job_order;
                  const startsGroup = view === "grouped" && (index === 0 || visible[index - 1].job_order.id !== order.id);
                  const updated = lastActivity(row);
                  return [
                    startsGroup && (
                      <tr key={`group-${order.id}`} className="map-group-row">
                        <th colSpan={COLUMNS.length} scope="rowgroup">
                          <span className="map-group-title">{orderLabel(row)}</span>
                          <span className="map-group-meta">
                            {[order.destinationCountry, order.id].filter(Boolean).join(" · ")}
                            {" · "}
                            {formatInt(groupSizes.get(order.id) ?? 0)} mapped
                            {order.headcount ? ` of ${formatInt(order.headcount)} needed` : ""}
                          </span>
                        </th>
                      </tr>
                    ),
                    <tr
                      key={row.id}
                      className="is-clickable"
                      onClick={() => setOpenId(row.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setOpenId(row.id);
                        }
                      }}
                      tabIndex={0}
                      aria-label={`Open the pipeline for ${name}`}
                    >
                      <td data-label="Job order">
                        <span className="map-order-cell">
                          <strong title={orderLabel(row)}>{order.missing ? "Deleted order" : order.title || "Untitled order"}</strong>
                          <small>{[order.client, order.destinationCountry].filter(Boolean).join(" · ") || "—"}</small>
                          <small className="crm-record-id">{order.id}</small>
                        </span>
                      </td>
                      <td data-label="Candidate">
                        <span className="ds-who">
                          <span className="ds-avatar" aria-hidden="true">{initialsOf(name)}</span>
                          <span className="ds-who-text">
                            <strong title={name}>{name}</strong>
                            <small className="crm-record-id">Candidate ID · {getReference(row)}</small>
                            <small>{getDesignation(row) || "—"} · {formatExperience(getExperienceYears(row))}</small>
                          </span>
                        </span>
                      </td>
                      <td data-label="Assigned to">
                        <span className={`ds-owner ${row.assigned_staff_id ? "" : "is-empty"}`}>
                          <UserCheck size={13} />
                          {row.assigned_staff_name || "Unassigned"}
                        </span>
                      </td>
                      <td data-label="Progress & status"><MappingProgress row={row} /></td>
                      <td data-label="Last update">
                        <span className="map-updated">{updated ? formatMoment(updated) : "—"}</span>
                      </td>
                    </tr>,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}

        <div className="ds-panel-foot">
          <span>
            Showing <strong>{formatInt(sorted.length === 0 ? 0 : pageStart + 1)}–{formatInt(Math.min(pageStart + pageSize, sorted.length))}</strong> of{" "}
            <strong>{formatInt(sorted.length)}</strong> mapped candidates
          </span>
          <div className="ds-pagination" aria-label="Mapped candidate pages">
            <Select
              size="sm"
              value={String(pageSize)}
              options={[10, 12, 25, 50].map((size) => ({ value: String(size), label: `${size} / page` }))}
              onChange={(value) => { setPageSize(Number(value)); resetPage(); }}
              ariaLabel="Mapped candidates per page"
            />
            <button type="button" onClick={() => setCurrentPage(Math.max(1, safePage - 1))} disabled={safePage === 1} aria-label="Previous page">
              <ChevronLeft size={15} />
            </button>
            <span>Page {safePage} of {pageCount}</span>
            <button type="button" onClick={() => setCurrentPage(Math.min(pageCount, safePage + 1))} disabled={safePage === pageCount} aria-label="Next page">
              <ChevronRight size={15} />
            </button>
          </div>
        </div>
      </section>

      {openRow && (
        <MappingPipelineDialog
          row={openRow}
          canSkip={canSkip}
          onClose={() => setOpenId(null)}
          onChanged={replaceRow}
          onToast={onToast}
          onOpenProfile={onOpenProfile ? (candidateId) => { setOpenId(null); onOpenProfile(candidateId); } : undefined}
        />
      )}
    </div>
  );
}
