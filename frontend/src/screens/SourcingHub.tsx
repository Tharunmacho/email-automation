"use client";

import React, { useEffect, useMemo, useState } from "react";
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
  Briefcase as BriefcaseIcon,
  Target,
  Layers,
} from "lucide-react";

import MetricTile from "@/components/ui/MetricTile";
import { deriveStatus, type JobOrderRecord } from "@/screens/JobOrders";
import { formatDateFull, formatInt, initialsOf } from "@/lib/format";
import {
  listSourcingClientsAPI,
  createSourcingClientAPI,
  deleteSourcingClientAPI,
  listJobOrdersAPI,
} from "@/lib/api";

export interface SourcingRecord {
  id: string;
  name: string;
  type: "association" | "business";
  contact: string;
  phone: string;
  email: string;
  date: string;
  status: "ACTIVE" | "PENDING" | "INACTIVE";
  industryOrCategory?: string;
  regNo?: string;
  address?: string;
}

const INITIAL_RECORDS: SourcingRecord[] = [];

/** Status drives the card's top cap and its badge, the same way it does on the
 *  candidate cards — one visual language for "state of this record". */
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

export default function SourcingHub() {
  const [activeTab, setActiveTab] = useState<"association" | "business">("association");
  const [searchQuery, setSearchQuery] = useState("");
  const [records, setRecords] = useState<SourcingRecord[]>(INITIAL_RECORDS);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);
  const [jobOrders, setJobOrders] = useState<JobOrderRecord[]>([]);

  // Form state for new client modal
  const [newType, setNewType] = useState<"association" | "business">("business");
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

  React.useEffect(() => {
    // 1. Sync any local storage records to MongoDB Atlas API
    if (typeof window !== "undefined") {
      const saved = localStorage.getItem("sourcing_records");
      if (saved) {
        try {
          const localRecords: SourcingRecord[] = JSON.parse(saved);
          localRecords.forEach((rec) => {
            createSourcingClientAPI(rec).catch(() => {});
          });
        } catch {}
      }
    }

    // 2. Fetch all real client records from MongoDB database API
    listSourcingClientsAPI()
      .then((res) => {
        if (res.items && res.items.length > 0) {
          setRecords(res.items);
          if (typeof window !== "undefined") {
            localStorage.setItem("sourcing_records", JSON.stringify(res.items));
          }
        } else if (typeof window !== "undefined") {
          const saved = localStorage.getItem("sourcing_records");
          if (saved) {
            try {
              setRecords(JSON.parse(saved));
            } catch {}
          }
        }
      })
      .catch(() => {
        if (typeof window !== "undefined") {
          const saved = localStorage.getItem("sourcing_records");
          if (saved) {
            try {
              setRecords(JSON.parse(saved));
            } catch {}
          }
        }
      });
  }, []);

  // The row menu only ever toggled from its own button, so clicking anywhere
  // else left it hanging open. Any pointer-down outside a menu closes it.
  useEffect(() => {
    if (!activeMenuId) return;

    const closeOnOutside = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest?.(".sourcing-card-actions")) setActiveMenuId(null);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setActiveMenuId(null);
    };

    document.addEventListener("mousedown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [activeMenuId]);

  React.useEffect(() => {
    const handleOpenModal = () => {
      setNewType(activeTab);
      setIsModalOpen(true);
    };
    window.addEventListener("open-new-client-modal", handleOpenModal);
    return () => window.removeEventListener("open-new-client-modal", handleOpenModal);
  }, [activeTab]);

  /**
   * Demand per client, keyed on the client name the job order stores. Matching
   * is case- and space-insensitive because both sides are free text.
   */
  const engagementByClient = useMemo(() => {
    const key = (value: string) => value.trim().toLowerCase().replace(/\s+/g, " ");
    const map = new Map<string, { orders: number; live: number; seats: number; filled: number }>();

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

  const engagementOf = (name: string) =>
    engagementByClient.map.get(engagementByClient.key(name));

  const engagedClients = records.filter((r) => (engagementOf(r.name)?.live ?? 0) > 0).length;
  const openSeats = records.reduce((sum, r) => {
    const e = engagementOf(r.name);
    return sum + (e && e.live > 0 ? Math.max(0, e.seats - e.filled) : 0);
  }, 0);

  const filteredRecords = records.filter((rec) => {
    if (rec.type !== activeTab) return false;
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    return (
      rec.name.toLowerCase().includes(q) ||
      rec.contact.toLowerCase().includes(q) ||
      rec.phone.toLowerCase().includes(q) ||
      rec.email.toLowerCase().includes(q) ||
      rec.id.toLowerCase().includes(q) ||
      (rec.industryOrCategory && rec.industryOrCategory.toLowerCase().includes(q))
    );
  });

  const handleCreateClient = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim() || !newContact.trim()) return;

    const prefix = newType === "association" ? "ASS" : "BUS";
    const uniqueNum = Math.floor(100 + Math.random() * 900);
    const id = `${prefix}-${uniqueNum}-${Date.now().toString().slice(-4)}`;
    const today = new Date();
    const formattedDate = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;

    const newRecord: SourcingRecord = {
      id,
      name: newName,
      type: newType,
      contact: newContact,
      phone: newPhone || "N/A",
      email: newEmail || "N/A",
      date: formattedDate,
      status: "ACTIVE",
      industryOrCategory: newIndustryOrCategory,
      regNo: newRegNo,
      address: newAddress,
    };

    // Save to MongoDB API & LocalStorage
    createSourcingClientAPI(newRecord).catch(() => {});

    setRecords((prev) => {
      const updated = [newRecord, ...prev];
      if (typeof window !== "undefined") {
        try {
          localStorage.setItem("sourcing_records", JSON.stringify(updated));
        } catch {}
      }
      return updated;
    });
    setIsModalOpen(false);

    // Reset form fields
    setNewName("");
    setNewIndustryOrCategory("");
    setNewRegNo("");
    setNewContact("");
    setNewPhone("");
    setNewEmail("");
    setNewAddress("");
  };

  const handleDeleteRecord = (id: string) => {
    deleteSourcingClientAPI(id).catch(() => {});
    setRecords((prev) => {
      const updated = prev.filter((r) => r.id !== id);
      if (typeof window !== "undefined") {
        try {
          localStorage.setItem("sourcing_records", JSON.stringify(updated));
        } catch {}
      }
      return updated;
    });
    setActiveMenuId(null);
  };

  const isBusiness = newType === "business";

  const associationCount = records.filter((r) => r.type === "association").length;
  const businessCount = records.filter((r) => r.type === "business").length;


  return (
    <div className="sourcing-hub-wrapper" style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {/* Now that clients link to job orders, these report real demand rather
          than restating the per-type counts already on the tabs. */}
      <div className="metric-tiles">
        <MetricTile
          label="Sourcing clients"
          value={formatInt(records.length)}
          icon={Building2}
          caption={`${formatInt(associationCount)} association${associationCount === 1 ? "" : "s"} · ${formatInt(businessCount)} business${businessCount === 1 ? "" : "es"}`}
        />
        <MetricTile
          label="Clients with live orders"
          value={formatInt(engagedClients)}
          icon={Target}
          accent="#047857"
          accentSoft="rgba(16, 185, 129, 0.10)"
          caption={
            records.length > 0
              ? `${Math.round((engagedClients / records.length) * 100)}% of the portfolio engaged`
              : "No clients yet"
          }
        />
        <MetricTile
          label="Open positions"
          value={formatInt(openSeats)}
          icon={Layers}
          caption="Unfilled seats across live orders"
        />
      </div>

      {/* Clean Tabs & Search Row */}
      <div className="sourcing-controls-row">
        <div className="sourcing-tabs-container">
          <button
            className={`sourcing-tab-btn ${activeTab === "association" ? "active" : ""}`}
            onClick={() => setActiveTab("association")}
          >
            <Users size={16} />
            <span>Associations</span>
            <span
              style={{
                fontSize: "0.72rem",
                fontWeight: 700,
                padding: "2px 7px",
                borderRadius: "999px",
                background: activeTab === "association" ? "#eaf0fa" : "#f6f9fd",
                color: activeTab === "association" ? "var(--primary)" : "#64748b",
              }}
            >
              {associationCount}
            </span>
          </button>
          <button
            className={`sourcing-tab-btn ${activeTab === "business" ? "active" : ""}`}
            onClick={() => setActiveTab("business")}
          >
            <Briefcase size={16} />
            <span>Businesses</span>
            <span
              style={{
                fontSize: "0.72rem",
                fontWeight: 700,
                padding: "2px 7px",
                borderRadius: "999px",
                background: activeTab === "business" ? "#ccfbf1" : "#f6f9fd",
                color: activeTab === "business" ? "#0d9488" : "#64748b",
              }}
            >
              {businessCount}
            </span>
          </button>
        </div>

        <div className="sourcing-controls-right" style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
          <div className="sourcing-search-wrapper">
            <Search size={16} className="sourcing-search-icon" />
            <input
              type="text"
              className="sourcing-search-input"
              placeholder={
                activeTab === "association" ? "Search associations..." : "Search businesses..."
              }
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
            {searchQuery && (
              <button
                className="sourcing-search-clear"
                onClick={() => setSearchQuery("")}
                title="Clear search"
              >
                <X size={14} />
              </button>
            )}
          </div>

          <button
            className="btn-new-client"
            onClick={() => {
              setNewType(activeTab);
              setIsModalOpen(true);
            }}
          >
            <Plus size={16} />
            <span>New Client</span>
          </button>
        </div>
      </div>

      {/* Cards Grid */}
      <div className="sourcing-cards-grid">
        {filteredRecords.length === 0 ? (
          <div className="sourcing-empty-state">
            <Building2 size={40} className="empty-icon" />
            <p>No {activeTab === "association" ? "associations" : "businesses"} found.</p>
          </div>
        ) : (
          filteredRecords.map((item) => {
            const officeLabel = item.type === "business" ? "Head office" : "Registered office";
            const rows: { icon: React.ReactNode; label: string; value: string; wrap?: boolean }[] = [
              { icon: <User size={14} />, label: "Contact", value: item.contact },
              { icon: <Phone size={14} />, label: "Phone", value: item.phone },
              { icon: <Mail size={14} />, label: "Email", value: item.email },
            ];
            if (item.address) {
              rows.push({
                icon: <MapPin size={14} />,
                label: officeLabel,
                value: item.address,
                wrap: true,
              });
            }

            return (
              <article className="sourcing-card" key={item.id}>
                <span className="sourcing-card-cap" />

                <header className="sc-head">
                  <span className="sc-monogram">{initialsOf(item.name)}</span>

                  <div className="sc-headings">
                    <h3 className="sc-name" title={item.name}>
                      {item.name}
                    </h3>
                    <div className="sc-meta">
                      {item.industryOrCategory && (
                        <span className="sourcing-chip" title={item.industryOrCategory}>
                          <Tag size={11} />
                          {item.industryOrCategory}
                        </span>
                      )}
                      {item.industryOrCategory && !isDefaultStatus(item.status) && (
                        <span className="sc-sep" aria-hidden="true" />
                      )}
                      {!isDefaultStatus(item.status) && (
                        <span className={`sourcing-badge status-${item.status.toLowerCase()}`}>
                          {item.status}
                        </span>
                      )}
                    </div>
                  </div>

                  <div className="sourcing-card-actions">
                    <button
                      className="sourcing-menu-btn"
                      onClick={() =>
                        setActiveMenuId((prev) => (prev === item.id ? null : item.id))
                      }
                      aria-label={`Actions for ${item.name}`}
                    >
                      <MoreVertical size={18} />
                    </button>

                    {activeMenuId === item.id && (
                      <div className="sourcing-dropdown-menu">
                        <button
                          className="dropdown-item danger"
                          onClick={() => handleDeleteRecord(item.id)}
                        >
                          <Trash2 size={14} />
                          <span>Delete</span>
                        </button>
                      </div>
                    )}
                  </div>
                </header>

                <dl className="sc-rows">
                  {rows.map((row) => (
                    <div className="sc-row" key={row.label}>
                      <span className="sc-row-icon">{row.icon}</span>
                      <dt className="sc-row-label">{row.label}</dt>
                      <dd
                        className={`sc-row-value ${row.wrap ? "sc-row-value-wrap" : ""}`}
                        title={row.value}
                      >
                        {row.value}
                      </dd>
                    </div>
                  ))}
                </dl>

                {(() => {
                  const engagement = engagementOf(item.name);
                  return (
                    <div className={`sc-demand ${engagement?.live ? "is-live" : ""}`}>
                      <BriefcaseIcon size={14} />
                      {engagement ? (
                        <span>
                          <strong>{engagement.orders}</strong> job order
                          {engagement.orders === 1 ? "" : "s"} · <strong>{engagement.seats}</strong>{" "}
                          seat{engagement.seats === 1 ? "" : "s"} · <strong>{engagement.filled}</strong>{" "}
                          filled
                        </span>
                      ) : (
                        <span>No job orders raised yet</span>
                      )}
                    </div>
                  );
                })()}

                <footer className="sc-foot">
                  <span className="sc-ref" title={item.regNo || item.id}>
                    <Hash size={12} />
                    {item.regNo || item.id}
                  </span>
                  <span className="sc-date">
                    <Calendar size={13} />
                    {displayDate(item.date)}
                  </span>
                </footer>
              </article>
            );
          })
        )}
      </div>

      {/* Create New Client Modal - Dynamic for Business and Association */}
      {isModalOpen && (
        <div className="cm-overlay active" onClick={() => setIsModalOpen(false)}>
          <div
            className="cm-dialog client-modal-dialog"
            style={{ maxWidth: "600px", borderRadius: "16px", padding: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header" style={{ padding: "1.5rem 1.75rem", borderBottom: "1px solid #f6f9fd" }}>
              <div>
                <h3 className="modal-title" style={{ fontSize: "1.4rem", fontWeight: 700 }}>
                  Create New Client
                </h3>
                <p className="modal-subtitle" style={{ fontSize: "0.85rem", color: "#64748b", marginTop: "0.2rem" }}>
                  Fill in the client details below
                </p>
              </div>
              <button className="modal-close-btn" onClick={() => setIsModalOpen(false)}>
                <X size={20} />
              </button>
            </div>

            <form onSubmit={handleCreateClient}>
              <div className="modal-body" style={{ padding: "1.75rem", display: "flex", flexDirection: "column", gap: "1.25rem" }}>
                {/* Row 1: Client Type & Company/Association Name */}
                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">Client Type</label>
                    <div style={{ position: "relative" }}>
                      <select
                        className="modal-select"
                        value={newType}
                        onChange={(e) =>
                          setNewType(e.target.value as "association" | "business")
                        }
                        style={{ appearance: "none", paddingRight: "2.25rem" }}
                      >
                        <option value="business">Business</option>
                        <option value="association">Association</option>
                      </select>
                      <ChevronDown size={16} style={{ position: "absolute", right: "0.85rem", top: "50%", transform: "translateY(-50%)", color: "#94a3b8", pointerEvents: "none" }} />
                    </div>
                  </div>

                  <div>
                    <label className="modal-label">
                      {isBusiness ? "Company Name *" : "Association Name *"}
                    </label>
                    <input
                      type="text"
                      className="modal-input"
                      placeholder={isBusiness ? "e.g. Apex Inc." : "e.g. activ"}
                      required
                      value={newName}
                      onChange={(e) => setNewName(e.target.value)}
                    />
                  </div>
                </div>

                {/* Row 2: Industry/Category & Registration No */}
                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">
                      {isBusiness ? "Industry / Sector" : "Category / Domain"}
                    </label>
                    <div style={{ position: "relative" }}>
                      <input
                        type="text"
                        list="sourcing-industry-datalist"
                        className="modal-input"
                        placeholder={isBusiness ? "Select or type Industry (e.g. IT Services)" : "Select or type Category (e.g. Professional Guild)"}
                        value={newIndustryOrCategory}
                        onChange={(e) => setNewIndustryOrCategory(e.target.value)}
                      />
                      <datalist id="sourcing-industry-datalist">
                        {isBusiness ? (
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

                  <div>
                    <label className="modal-label">
                      {isBusiness ? "Company Registration No." : "Association Registration No."}
                    </label>
                    <input
                      type="text"
                      className="modal-input"
                      placeholder={isBusiness ? "e.g. CRN-12345" : "e.g. ASSOC-9921"}
                      value={newRegNo}
                      onChange={(e) => setNewRegNo(e.target.value)}
                    />
                  </div>
                </div>

                {/* Row 3: HR / Contact Person & Phone */}
                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">
                      {isBusiness ? "HR Contact Person *" : "Primary Contact Person *"}
                    </label>
                    <input
                      type="text"
                      className="modal-input"
                      placeholder={isBusiness ? "e.g. Jane Doe" : "e.g. thamizh"}
                      required
                      value={newContact}
                      onChange={(e) => setNewContact(e.target.value)}
                    />
                  </div>

                  <div>
                    <label className="modal-label">Phone Number</label>
                    <input
                      type="text"
                      className="modal-input"
                      placeholder={isBusiness ? "e.g. +1 555-0199" : "e.g. 909764578"}
                      value={newPhone}
                      onChange={(e) => setNewPhone(e.target.value)}
                    />
                  </div>
                </div>

                {/* Row 4: Email Address */}
                <div>
                  <label className="modal-label">Email Address</label>
                  <input
                    type="email"
                    className="modal-input"
                    placeholder={isBusiness ? "e.g. contact@company.com" : "e.g. tharun@gmail.com"}
                    value={newEmail}
                    onChange={(e) => setNewEmail(e.target.value)}
                  />
                </div>

                {/* Row 5: Address */}
                <div>
                  <label className="modal-label">
                    {isBusiness ? "Head Office Address" : "Registered Office Address"}
                  </label>
                  <textarea
                    className="modal-textarea"
                    placeholder={isBusiness ? "123 Corporate Blvd..." : "456 Association Ave..."}
                    value={newAddress}
                    onChange={(e) => setNewAddress(e.target.value)}
                  />
                </div>
              </div>

              <div className="modal-footer" style={{ padding: "1.25rem 1.75rem", borderTop: "1px solid #f6f9fd" }}>
                <button
                  type="button"
                  className="modal-cancel-btn"
                  onClick={() => setIsModalOpen(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="modal-submit-btn">
                  Create Client
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
