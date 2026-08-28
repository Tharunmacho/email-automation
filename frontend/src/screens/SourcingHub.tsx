"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  Plus,
  Users,
  Briefcase,
  Search,
  Building2,
  MoreVertical,
  User,
  Phone,
  Mail,
  Calendar,
  X,
  Trash2,
  ChevronDown,
  Hash,
  MapPin,
  Tag,
  Pencil,
  Copy,
  Check,
  Zap,
  Target,
  Inbox,
  AlertTriangle,
  type LucideIcon,
} from "lucide-react";

import { type StatTone } from "@/components/ui/StatTile";
import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { formatDateFull, formatInt, initialsOf } from "@/lib/format";
import { deriveStatus } from "@/screens/JobOrders";
import type { JobOrderRecord, SourcingClientRecord, SourcingType } from "@/types";
import {
  listSourcingClientsAPI,
  createSourcingClientAPI,
  deleteSourcingClientAPI,
  listJobOrdersAPI,
} from "@/lib/api";
import { CACHE_KEYS, readCache, writeCache } from "@/lib/localCache";

/**
 * What kind of party this is.
 *
 * Three: an *agent* introduces candidates, an *association* represents a
 * membership, and a *client* is a named account that hires under a contract.
 * They are sourced differently and chased differently, so the record says which.
 *
 * "Business" was a fourth, and it is gone. It meant "hires directly", which is
 * what a client is — the two were never chased differently, and a person adding
 * a company had to guess which of the two words the agency meant that day.
 *
 * The stored values are unchanged, so every row already in the database reads
 * back as exactly what it was. Rows written as `business` are normalised to
 * `client` on the way in — see `normaliseType` — so nothing disappears from the
 * screen because a category was retired underneath it.
 */
export type { SourcingType };

/**
 * What the API hands back, mapped onto what this screen offers.
 *
 * Only ever widens to `client`: the retired `business` meant a company that
 * hires directly, and an unrecognised value from a newer API is more usefully
 * shown as a client than dropped. A record is never hidden for having a type
 * this build does not know about.
 */
export function normaliseType(raw: string | undefined): SourcingType {
  return raw === "agent" || raw === "association" ? raw : "client";
}

// `SourcingClientRecord` in `@/types`, which is what `lib/api` sends and
// receives. Aliased here because this screen has always called it a
// SourcingRecord and several call sites read better that way.
export type SourcingRecord = SourcingClientRecord;

type TypeFilter = "all" | SourcingType;
type SortKey = "recent" | "name" | "demand";

/** Demand a client is carrying, rolled up from its job orders. */
interface Engagement {
  orders: number;
  live: number;
  seats: number;
  filled: number;
}

/**
 * What state a client is in, which is the one thing a card is colour-coded by.
 * `live` = hiring right now, `filled` = every seat covered, `idle` = no demand.
 */
type ClientTone = "live" | "filled" | "idle";

/** Client state mapped onto the product's shared tone vocabulary. */
const CLIENT_TONE: Record<ClientTone, StatTone> = {
  live: "blue",
  filled: "green",
  idle: "slate",
};

const INITIAL_RECORDS: SourcingRecord[] = [];

const TYPE_TABS: { key: TypeFilter; label: string; icon: LucideIcon }[] = [
  { key: "all", label: "Everyone", icon: Building2 },
  { key: "agent", label: "Agents", icon: User },
  { key: "association", label: "Associates", icon: Users },
  { key: "client", label: "Clients", icon: Target },
];

/** Singular labels, for the places that describe one record rather than a filter. */
const TYPE_LABEL: Record<SourcingType, string> = {
  agent: "agent",
  association: "associate",
  client: "client",
};

/** The prefix on a generated id, so a reference read over the phone says what it is. */
const TYPE_PREFIX: Record<SourcingType, string> = {
  agent: "AGT",
  association: "ASS",
  client: "CLI",
};

/**
 * How each kind is badged on a card.
 *
 * A table rather than the two-way conditional this used to be: with three kinds
 * a ternary silently labelled agents as associates, and would do the same to
 * whatever is added next.
 */
const TYPE_CHIP: Record<SourcingType, { label: string; icon: React.ReactNode }> = {
  agent: { label: "Agent", icon: <User size={11} /> },
  association: { label: "Associate", icon: <Users size={11} /> },
  client: { label: "Client", icon: <Briefcase size={11} /> },
};

const SORT_LABELS: Record<SortKey, string> = {
  recent: "Recently added",
  name: "Name (A–Z)",
  demand: "Most demand",
};

/** Every record is created ACTIVE and nothing can change it yet, so the badge
 *  would be a constant on every card. Show it only when a record carries a
 *  non-default status (set directly in the DB), where it actually means something. */
function isDefaultStatus(status: SourcingRecord["status"]): boolean {
  return status === "ACTIVE";
}

/** Records created before dates were normalised still carry M/D/YYYY. */
function parseRecordDate(value: string | undefined): Date | null {
  if (!value) return null;
  const iso = value.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));
  const slash = value.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (slash) return new Date(Number(slash[3]), Number(slash[1]) - 1, Number(slash[2]));
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function displayDate(value: string | undefined): string {
  const parsed = parseRecordDate(value);
  return parsed ? formatDateFull(parsed) : "—";
}

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** A blank phone/email is stored as the literal "N/A" by the create form. */
function hasValue(value: string | undefined): boolean {
  const v = (value || "").trim();
  return v !== "" && v.toUpperCase() !== "N/A";
}

function toneOf(engagement: Engagement | undefined): ClientTone {
  if (!engagement || engagement.orders === 0) return "idle";
  if (engagement.live > 0) return "live";
  return "filled";
}

interface SourcingHubProps {
  /** Reports what happened, by name, to the dashboard's activity log. */
  onActivity?: (message: string, type?: LogEntry["type"]) => void;
}

export default function SourcingHub({ onActivity }: SourcingHubProps) {
  const activity = useCallback(
    (message: string, type: LogEntry["type"] = "info") => onActivity?.(message, type),
    [onActivity],
  );

  const [typeFilter, setTypeFilter] = useState<TypeFilter>("all");
  const [searchQuery, setSearchQuery] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("recent");
  const [liveOnly, setLiveOnly] = useState(false);

  const [records, setRecords] = useState<SourcingRecord[]>(INITIAL_RECORDS);
  const [loading, setLoading] = useState(true);
  const [jobOrders, setJobOrders] = useState<JobOrderRecord[]>([]);

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [formError, setFormError] = useState("");

  // Form state, shared by the create and edit flows
  const [newType, setNewType] = useState<SourcingType>("client");
  const [newName, setNewName] = useState("");
  const [newIndustryOrCategory, setNewIndustryOrCategory] = useState("");
  const [newRegNo, setNewRegNo] = useState("");
  const [newContact, setNewContact] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newAddress, setNewAddress] = useState("");

  // Job orders carry a `client` name, which is what ties a sourcing record to
  // real demand. Without this the hub is just an address book.
  useEffect(() => {
    listJobOrdersAPI()
      .then((res) => setJobOrders(res.items ?? []))
      .catch(() => setJobOrders([]));
  }, []);

  // The DB is the source of truth. Whatever it returns replaces the table
  // outright — an empty list means the user deleted everything, not that the
  // lookup missed. The cache is only consulted when the request fails, and is
  // never pushed back to the API.
  useEffect(() => {
    let active = true;

    listSourcingClientsAPI()
      .then((res) => {
        if (!active) return;
        // Types are normalised on the way in, not trusted as they arrive: the
        // database still holds rows written as `business`, and a row whose type
        // this build does not recognise would fall out of every tab and render
        // an undefined badge. `normaliseType` shows it as a client instead.
        const items = ((res.items ?? []) as SourcingRecord[]).map((item) => ({
          ...item,
          type: normaliseType(item.type),
        }));
        setRecords(items);
        writeCache(CACHE_KEYS.sourcingClients, items);
      })
      .catch(() => {
        // API unreachable — fall back to the last response we saw, which may
        // predate the retired type just as the API's rows do.
        const cached = readCache<SourcingRecord>(CACHE_KEYS.sourcingClients);
        if (active && cached) {
          setRecords(cached.map((item) => ({ ...item, type: normaliseType(item.type) })));
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
    };
  }, []);

  // The row menu only ever toggled from its own button, so clicking anywhere
  // else left it hanging open. Any pointer-down outside a menu closes it.
  useEffect(() => {
    if (!activeMenuId) return;

    const closeOnOutside = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest?.(".sh-actions")) {
        setActiveMenuId(null);
        setConfirmDeleteId(null);
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setActiveMenuId(null);
        setConfirmDeleteId(null);
      }
    };

    document.addEventListener("mousedown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [activeMenuId]);

  const resetForm = useCallback(() => {
    setNewName("");
    setNewIndustryOrCategory("");
    setNewRegNo("");
    setNewContact("");
    setNewPhone("");
    setNewEmail("");
    setNewAddress("");
    setFormError("");
  }, []);

  const openCreateModal = useCallback(() => {
    setEditingId(null);
    resetForm();
    // Adding from a filtered view pre-selects that kind, which is almost always
    // what the person clicking "Add" while looking at Agents meant.
    setNewType(typeFilter === "all" ? "client" : typeFilter);
    setIsModalOpen(true);
  }, [resetForm, typeFilter]);

  const openEditModal = useCallback((item: SourcingRecord) => {
    setEditingId(item.id);
    setNewType(item.type);
    setNewName(item.name);
    setNewIndustryOrCategory(item.industryOrCategory || "");
    setNewRegNo(item.regNo || "");
    setNewContact(item.contact);
    setNewPhone(hasValue(item.phone) ? item.phone : "");
    setNewEmail(hasValue(item.email) ? item.email : "");
    setNewAddress(item.address || "");
    setFormError("");
    setIsModalOpen(true);
    setActiveMenuId(null);
  }, []);

  useEffect(() => {
    const handleOpenModal = () => openCreateModal();
    window.addEventListener("open-new-client-modal", handleOpenModal);
    return () => window.removeEventListener("open-new-client-modal", handleOpenModal);
  }, [openCreateModal]);

  /**
   * Demand per client, keyed on the client name the job order stores. Matching
   * is case- and space-insensitive because both sides are free text.
   */
  const engagementByClient = useMemo(() => {
    const key = (value: string) => value.trim().toLowerCase().replace(/\s+/g, " ");
    const map = new Map<string, Engagement>();

    for (const order of jobOrders) {
      const id = key(order.client || "");
      if (!id) continue;

      const entry = map.get(id) ?? { orders: 0, live: 0, seats: 0, filled: 0 };
      const status = deriveStatus(order);
      entry.orders += 1;
      if (status === "OPEN" || status === "IN PROGRESS") entry.live += 1;
      entry.seats += order.headcount || 1;
      entry.filled += (order.shortlistedCandidateIds || []).length || order.fulfilledCount || 0;
      map.set(id, entry);
    }

    return { map, key };
  }, [jobOrders]);

  const engagementOf = useCallback(
    (name: string): Engagement | undefined => engagementByClient.map.get(engagementByClient.key(name)),
    [engagementByClient],
  );

  const countFor = (key: TypeFilter) =>
    key === "all" ? records.length : records.filter((r) => r.type === key).length;

  const visibleRecords = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();

    const filtered = records.filter((rec) => {
      if (typeFilter !== "all" && rec.type !== typeFilter) return false;
      if (liveOnly && (engagementOf(rec.name)?.live ?? 0) === 0) return false;
      if (!q) return true;
      return (
        rec.name.toLowerCase().includes(q) ||
        rec.contact.toLowerCase().includes(q) ||
        rec.phone.toLowerCase().includes(q) ||
        rec.email.toLowerCase().includes(q) ||
        rec.id.toLowerCase().includes(q) ||
        (rec.regNo || "").toLowerCase().includes(q) ||
        (rec.address || "").toLowerCase().includes(q) ||
        (rec.industryOrCategory || "").toLowerCase().includes(q)
      );
    });

    const sorted = [...filtered];
    if (sortKey === "name") {
      sorted.sort((a, b) => a.name.localeCompare(b.name));
    } else if (sortKey === "demand") {
      sorted.sort((a, b) => {
        const ea = engagementOf(a.name);
        const eb = engagementOf(b.name);
        return (
          (eb?.live ?? 0) - (ea?.live ?? 0) ||
          (eb?.seats ?? 0) - (ea?.seats ?? 0) ||
          a.name.localeCompare(b.name)
        );
      });
    } else {
      sorted.sort((a, b) => {
        const da = parseRecordDate(a.date)?.getTime() ?? 0;
        const db = parseRecordDate(b.date)?.getTime() ?? 0;
        return db - da || a.name.localeCompare(b.name);
      });
    }
    return sorted;
  }, [records, typeFilter, liveOnly, searchQuery, sortKey, engagementOf]);

  const isFiltered = Boolean(searchQuery.trim()) || liveOnly || typeFilter !== "all";

  const clearFilters = () => {
    setSearchQuery("");
    setLiveOnly(false);
    setTypeFilter("all");
  };

  const persist = (updated: SourcingRecord[]) => {
    setRecords(updated);
    writeCache(CACHE_KEYS.sourcingClients, updated);
  };

  const handleSubmitClient = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) {
      setFormError("A client name is required.");
      return;
    }
    if (!newContact.trim()) {
      setFormError("A contact person is required — it is who the job order gets chased through.");
      return;
    }

    // Two clients with the same name cannot be told apart by the job orders,
    // which match on name alone. Catch it here rather than silently merging
    // both clients' demand into one card.
    const nameKey = engagementByClient.key(newName);
    const clash = records.some(
      (r) => r.id !== editingId && engagementByClient.key(r.name) === nameKey,
    );
    if (clash) {
      setFormError("Another client already uses this name. Job orders match clients by name, so names must be unique.");
      return;
    }

    if (editingId) {
      const existing = records.find((r) => r.id === editingId);
      if (!existing) return;

      const updatedRecord: SourcingRecord = {
        ...existing,
        name: newName.trim(),
        type: newType,
        contact: newContact.trim(),
        phone: newPhone.trim() || "N/A",
        email: newEmail.trim() || "N/A",
        industryOrCategory: newIndustryOrCategory.trim(),
        regNo: newRegNo.trim(),
        address: newAddress.trim(),
      };

      activity(
        existing.name === updatedRecord.name
          ? `Updated client: ${updatedRecord.name}.`
          : `Updated client: ${existing.name} — renamed to ${updatedRecord.name}.`,
        "success",
      );
      // POST /sourcing-clients upserts on `id`, so the same call saves an edit.
      createSourcingClientAPI(updatedRecord).catch(() => {});
      persist(records.map((r) => (r.id === editingId ? updatedRecord : r)));
    } else {
      const prefix = TYPE_PREFIX[newType] ?? "SRC";
      const uniqueNum = Math.floor(100 + Math.random() * 900);
      const id = `${prefix}-${uniqueNum}-${Date.now().toString().slice(-4)}`;

      const newRecord: SourcingRecord = {
        id,
        name: newName.trim(),
        type: newType,
        contact: newContact.trim(),
        phone: newPhone.trim() || "N/A",
        email: newEmail.trim() || "N/A",
        date: todayISO(),
        status: "ACTIVE",
        industryOrCategory: newIndustryOrCategory.trim(),
        regNo: newRegNo.trim(),
        address: newAddress.trim(),
      };

      activity(`Added ${TYPE_LABEL[newType]}: ${newRecord.name}.`, "success");
      createSourcingClientAPI(newRecord).catch(() => {});
      persist([newRecord, ...records]);
    }

    setIsModalOpen(false);
    setEditingId(null);
    resetForm();
  };

  const handleDeleteRecord = (id: string) => {
    // Resolved before the record leaves the list.
    const target = records.find((r) => r.id === id);
    activity(`Deleted client: ${target?.name ?? id}.`, "warn");

    deleteSourcingClientAPI(id).catch(() => {});
    persist(records.filter((r) => r.id !== id));
    setActiveMenuId(null);
    setConfirmDeleteId(null);
  };

  const handleCopyEmail = async (item: SourcingRecord) => {
    if (!hasValue(item.email)) return;
    try {
      await navigator.clipboard.writeText(item.email);
      setCopiedId(item.id);
      setTimeout(() => setCopiedId((prev) => (prev === item.id ? null : prev)), 1600);
    } catch {
      /* clipboard unavailable (insecure origin) — nothing useful to say */
    }
  };

  const closeModal = () => {
    setIsModalOpen(false);
    setEditingId(null);
  };

  /**
   * Whether the party being added is a company.
   *
   * This was `isBusiness` and turned on the retired type. A client is the
   * company now, so the company-shaped wording — HR contact, company
   * registration number, head office — follows it rather than disappearing with
   * the category.
   */
  const isCompany = newType === "client";
  /** Field labels that name the kind of party being added, rather than guessing. */
  const nameLabel = {
    agent: "Agent name *",
    association: "Associate name *",
    client: "Client name *",
  }[newType];
  const namePlaceholder = {
    agent: "e.g. Ravi Recruitment Services",
    association: "e.g. Activ Guild",
    client: "e.g. Gulf Steel Works",
  }[newType];
  const sectorLabel = {
    agent: "Region / speciality",
    association: "Category / domain",
    client: "Industry / sector",
  }[newType];
  const sectorPlaceholder = {
    agent: "e.g. Tamil Nadu — welders",
    association: "e.g. Professional Guild",
    client: "e.g. Oil & Gas",
  }[newType];

  // ---- pieces ------------------------------------------------------------ //

  const renderCard = (item: SourcingRecord) => {
    const e = engagementOf(item.name);
    const tone = toneOf(e);
    const seats = e?.seats ?? 0;
    const filled = e?.filled ?? 0;
    const pct = seats > 0 ? Math.min(100, Math.round((filled / seats) * 100)) : 0;
    const officeLabel = item.type === "client" ? "Head office" : "Registered office";

    // Every card renders the same four rows and the same demand block, whether
    // or not the values exist. Optional rows made the footers sit at different
    // heights, so the grid never lined up.
    const rows: { key: string; icon: React.ReactNode; label: string; body: React.ReactNode }[] = [
      {
        key: "contact",
        icon: <User size={14} />,
        label: "Contact",
        body: <span title={item.contact}>{item.contact}</span>,
      },
      {
        key: "phone",
        icon: <Phone size={14} />,
        label: "Phone",
        body: hasValue(item.phone) ? (
          <a className="sh-link" href={`tel:${item.phone.replace(/\s+/g, "")}`} title={item.phone}>
            {item.phone}
          </a>
        ) : (
          <span className="sh-row-empty">Not provided</span>
        ),
      },
      {
        key: "email",
        icon: <Mail size={14} />,
        label: "Email",
        body: hasValue(item.email) ? (
          <>
            <a className="sh-link" href={`mailto:${item.email}`} title={item.email}>
              {item.email}
            </a>
            <button
              className="sh-copy"
              onClick={() => handleCopyEmail(item)}
              title="Copy email address"
              aria-label={`Copy email address for ${item.name}`}
            >
              {copiedId === item.id ? <Check size={12} /> : <Copy size={12} />}
            </button>
          </>
        ) : (
          <span className="sh-row-empty">Not provided</span>
        ),
      },
      {
        key: "office",
        icon: <MapPin size={14} />,
        label: officeLabel,
        body: item.address ? (
          <span title={item.address}>{item.address}</span>
        ) : (
          <span className="sh-row-empty">Not provided</span>
        ),
      },
    ];

    return (
      <article className={`sh-card tone-${CLIENT_TONE[tone]}`} key={item.id}>
        <header className="sh-card-head">
          <span className="sh-monogram">{initialsOf(item.name)}</span>

          <div className="sh-headings">
            <h3 className="sh-name" title={item.name}>
              {item.name}
            </h3>
            <div className="sh-meta">
              <span className={`sh-type-chip ${item.type}`}>
                {TYPE_CHIP[item.type].icon}
                {TYPE_CHIP[item.type].label}
              </span>
              {item.industryOrCategory ? (
                <span className="sh-chip" title={item.industryOrCategory}>
                  <Tag size={11} />
                  {item.industryOrCategory}
                </span>
              ) : (
                <span className="sh-chip sh-chip-empty">
                  <Tag size={11} />
                  {item.type === "client" ? "No sector set" : "No category set"}
                </span>
              )}
              {!isDefaultStatus(item.status) && (
                <span className={`sh-status status-${item.status.toLowerCase()}`}>{item.status}</span>
              )}
            </div>
          </div>

          <div className="sh-actions">
            <button
              className="sh-icon-btn"
              onClick={() => {
                setActiveMenuId((prev) => (prev === item.id ? null : item.id));
                setConfirmDeleteId(null);
              }}
              aria-label={`Actions for ${item.name}`}
              aria-expanded={activeMenuId === item.id}
            >
              <MoreVertical size={18} />
            </button>

            {activeMenuId === item.id && (
              <div className="sourcing-dropdown-menu sh-menu">
                {confirmDeleteId === item.id ? (
                  <div className="sh-confirm">
                    <p className="sh-confirm-title">
                      <AlertTriangle size={13} />
                      Delete {item.name}?
                    </p>
                    <p className="sh-confirm-note">
                      Its job orders stay, but they will no longer match a client.
                    </p>
                    <div className="sh-confirm-row">
                      <button className="sh-confirm-cancel" onClick={() => setConfirmDeleteId(null)}>
                        Cancel
                      </button>
                      <button className="sh-confirm-go" onClick={() => handleDeleteRecord(item.id)}>
                        Delete
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                    <button className="dropdown-item" onClick={() => openEditModal(item)}>
                      <Pencil size={14} />
                      <span>Edit details</span>
                    </button>
                    <button className="dropdown-item danger" onClick={() => setConfirmDeleteId(item.id)}>
                      <Trash2 size={14} />
                      <span>Delete</span>
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        </header>

        <dl className="sh-rows">
          {rows.map((row) => (
            <div className="sh-row" key={row.key}>
              <span className="sh-row-icon">{row.icon}</span>
              <dt className="sh-row-label">{row.label}</dt>
              <dd className="sh-row-value">{row.body}</dd>
            </div>
          ))}
        </dl>

        {/* Demand strip — colour and copy both come from `tone`. */}
        <div className="sh-demand">
          <div className="sh-demand-head">
            <span className="sh-demand-title">
              <Briefcase size={13} />
              {e ? `${formatInt(e.orders)} job order${e.orders === 1 ? "" : "s"}` : "No job orders"}
            </span>
            {tone === "live" ? (
              <span className="sh-live-pill">
                <span className="sh-live-dot" />
                {formatInt(e?.live ?? 0)} live
              </span>
            ) : tone === "filled" ? (
              <span className="sh-done-pill">
                <Check size={11} />
                All closed
              </span>
            ) : (
              <span className="sh-demand-quiet">Not engaged</span>
            )}
          </div>

          <div className="sh-bar">
            <span className="sh-bar-fill" style={{ width: `${pct}%` }} />
          </div>

          <span className="sh-demand-foot">
            {e
              ? `${formatInt(filled)} of ${formatInt(seats)} seat${seats === 1 ? "" : "s"} filled · ${pct}%`
              : "No hiring demand raised against this client yet"}
          </span>
        </div>

        <footer className="sh-card-foot">
          <span className="sh-ref" title={item.regNo || item.id}>
            <Hash size={12} />
            {item.regNo || item.id}
          </span>
          <span className="sh-date">
            <Calendar size={13} />
            {displayDate(item.date)}
          </span>
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
            {searchQuery.trim() ? `No client matches “${searchQuery.trim()}”. ` : ""}
            Try widening the search or clearing the filters.
          </span>
          <button className="sh-empty-btn" onClick={clearFilters}>
            Clear filters
          </button>
        </>
      ) : (
        <>
          <p className="sh-empty-title">No sourcing clients yet</p>
          <span className="sh-empty-note">
            Add the agents, associates and clients you source through. Job orders
            raised against them will then roll up here as live demand.
          </span>
          <button className="sh-empty-btn primary" onClick={openCreateModal}>
            <Plus size={15} />
            Add your first client
          </button>
        </>
      )}
    </div>
  );

  const renderSkeleton = () => (
    <div className="sh-grid">
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

  // ---- render ------------------------------------------------------------ //

  return (
    <div className="sh-root">
      {/* Controls */}
      <div className="sh-toolbar">
        <div className="sh-segment" role="tablist" aria-label="Client type">
          {TYPE_TABS.map((tab) => {
            const Icon = tab.icon;
            const active = typeFilter === tab.key;
            return (
              <button
                key={tab.key}
                role="tab"
                aria-selected={active}
                className={`sh-segment-btn ${active ? "active" : ""}`}
                onClick={() => setTypeFilter(tab.key)}
              >
                <Icon size={15} />
                <span>{tab.label}</span>
                <span className="sh-segment-count">{formatInt(countFor(tab.key))}</span>
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
              placeholder="Search name, contact, email, sector…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              aria-label="Search clients"
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

          <button
            className={`sh-toggle ${liveOnly ? "active" : ""}`}
            onClick={() => setLiveOnly((prev) => !prev)}
            aria-pressed={liveOnly}
            title="Show only clients with an open or in-progress job order"
          >
            <Zap size={14} />
            <span>Hiring now</span>
          </button>

          <div className="sh-select">
            <select
              value={sortKey}
              onChange={(e) => setSortKey(e.target.value as SortKey)}
              aria-label="Sort clients"
            >
              {(Object.keys(SORT_LABELS) as SortKey[]).map((key) => (
                <option key={key} value={key}>
                  {SORT_LABELS[key]}
                </option>
              ))}
            </select>
            <ChevronDown size={14} />
          </div>

          <button className="sh-new-btn" onClick={openCreateModal}>
            <Plus size={16} />
            <span>New client</span>
          </button>
        </div>
      </div>

      <div className="sh-resultbar">
        <span className="sh-result-count">
          {loading
            ? "Loading clients…"
            : `${formatInt(visibleRecords.length)} of ${formatInt(records.length)} client${records.length === 1 ? "" : "s"}`}
        </span>
        {isFiltered && !loading && (
          <button className="sh-clear-link" onClick={clearFilters}>
            <X size={13} />
            Clear filters
          </button>
        )}
      </div>

      {loading ? renderSkeleton() : visibleRecords.length === 0 ? renderEmpty() : (
        <div className="sh-grid">{visibleRecords.map(renderCard)}</div>
      )}

      {/* Create / edit client */}
      {isModalOpen && (
        <div className="cm-overlay active" onClick={closeModal}>
          <div
            className="cm-dialog sh-modal"
            onClick={(e) => e.stopPropagation()}
            role="dialog"
            aria-modal="true"
            aria-label={editingId ? "Edit client" : "Create new client"}
          >
            <div className="sh-modal-head">
              <div>
                <h3 className="sh-modal-title">{editingId ? "Edit client" : "New sourcing client"}</h3>
                <p className="sh-modal-sub">
                  {editingId
                    ? "Job orders match clients by name — renaming this one detaches the orders raised under the old name."
                    : "Register an agent, associate or client you source candidates through."}
                </p>
              </div>
              <button className="sh-modal-close" onClick={closeModal} aria-label="Close">
                <X size={18} />
              </button>
            </div>

            <form onSubmit={handleSubmitClient}>
              <div className="modal-body sh-modal-body">
                <div className="sh-field">
                  <label className="modal-label" htmlFor="sh-type">
                    Type
                  </label>
                  {/*
                    A dropdown rather than a row of buttons. The order is how
                    often each is added, not alphabetical.
                  */}
                  <select
                    id="sh-type"
                    className="modal-select"
                    value={newType}
                    onChange={(e) => setNewType(e.target.value as SourcingType)}
                  >
                    <option value="agent">Agent — introduces candidates</option>
                    <option value="association">Associate — represents a membership</option>
                    <option value="client">Client — a company hiring under contract</option>
                  </select>
                </div>

                <div className="modal-row-2">
                  <div className="sh-field">
                    <label className="modal-label" htmlFor="sh-name">
                      {nameLabel}
                    </label>
                    <input
                      id="sh-name"
                      type="text"
                      className="modal-input"
                      placeholder={namePlaceholder}
                      required
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                    />
                  </div>

                  <div className="sh-field">
                    <label className="modal-label" htmlFor="sh-industry">
                      {sectorLabel}
                    </label>
                    <input
                      id="sh-industry"
                      type="text"
                      list="sourcing-industry-datalist"
                      className="modal-input"
                      placeholder={sectorPlaceholder}
                      value={newIndustryOrCategory}
                      onChange={(e) => setNewIndustryOrCategory(e.target.value)}
                    />
                    <datalist id="sourcing-industry-datalist">
                      {isCompany ? (
                        <>
                          <option value="IT Services" />
                          <option value="Software & Tech" />
                          <option value="Healthcare" />
                          <option value="Finance & Banking" />
                          <option value="Manufacturing" />
                          <option value="Consulting" />
                          <option value="Logistics & Transport" />
                        </>
                      ) : (
                        <>
                          <option value="Professional Guild" />
                          <option value="Technology Hub" />
                          <option value="Trade Association" />
                          <option value="Educational Network" />
                          <option value="Non-Profit" />
                        </>
                      )}
                    </datalist>
                  </div>
                </div>

                <div className="modal-row-2">
                  <div className="sh-field">
                    <label className="modal-label" htmlFor="sh-contact">
                      {isCompany ? "HR contact person *" : "Primary contact person *"}
                    </label>
                    <input
                      id="sh-contact"
                      type="text"
                      className="modal-input"
                      placeholder="e.g. Jane Doe"
                      required
                      value={newContact}
                      onChange={(e) => setNewContact(e.target.value)}
                    />
                  </div>

                  <div className="sh-field">
                    <label className="modal-label" htmlFor="sh-reg">
                      {isCompany ? "Company registration no." : "Registration no."}
                    </label>
                    <input
                      id="sh-reg"
                      type="text"
                      className="modal-input"
                      placeholder={isCompany ? "e.g. CRN-12345" : "e.g. REG-9921"}
                      value={newRegNo}
                      onChange={(e) => setNewRegNo(e.target.value)}
                    />
                  </div>
                </div>

                <div className="modal-row-2">
                  <div className="sh-field">
                    <label className="modal-label" htmlFor="sh-phone">
                      Phone number
                    </label>
                    <input
                      id="sh-phone"
                      type="tel"
                      className="modal-input"
                      placeholder="e.g. +91 90976 45780"
                      value={newPhone}
                      onChange={(e) => setNewPhone(e.target.value)}
                    />
                  </div>

                  <div className="sh-field">
                    <label className="modal-label" htmlFor="sh-email">
                      Email address
                    </label>
                    <input
                      id="sh-email"
                      type="email"
                      className="modal-input"
                      placeholder="e.g. contact@company.com"
                      value={newEmail}
                      onChange={(e) => setNewEmail(e.target.value)}
                    />
                  </div>
                </div>

                <div className="sh-field">
                  <label className="modal-label" htmlFor="sh-address">
                    {isCompany ? "Head office address" : "Registered address"}
                  </label>
                  <textarea
                    id="sh-address"
                    className="modal-textarea"
                    placeholder={isCompany ? "123 Corporate Blvd…" : "456 Registered Ave…"}
                    value={newAddress}
                    onChange={(e) => setNewAddress(e.target.value)}
                  />
                </div>

                {formError && (
                  <p className="sh-form-error" role="alert">
                    <AlertTriangle size={14} />
                    {formError}
                  </p>
                )}
              </div>

              <div className="modal-footer">
                <button type="button" className="modal-cancel-btn" onClick={closeModal}>
                  Cancel
                </button>
                <button type="submit" className="modal-submit-btn">
                  {editingId ? "Save changes" : "Create client"}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
