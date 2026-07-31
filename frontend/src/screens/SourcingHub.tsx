"use client";

import React, { useState } from "react";
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
} from "lucide-react";

import { listSourcingClientsAPI, createSourcingClientAPI, deleteSourcingClientAPI } from "@/lib/api";

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

interface SourcingHubProps {
  embedded?: boolean;
}

export default function SourcingHub({ embedded = false }: SourcingHubProps) {
  const [activeTab, setActiveTab] = useState<"association" | "business">("association");
  const [searchQuery, setSearchQuery] = useState("");
  const [records, setRecords] = useState<SourcingRecord[]>(INITIAL_RECORDS);
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);

  // Form state for new client modal
  const [newType, setNewType] = useState<"association" | "business">("business");
  const [newName, setNewName] = useState("");
  const [newIndustryOrCategory, setNewIndustryOrCategory] = useState("");
  const [newRegNo, setNewRegNo] = useState("");
  const [newContact, setNewContact] = useState("");
  const [newPhone, setNewPhone] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newAddress, setNewAddress] = useState("");

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

  React.useEffect(() => {
    const handleOpenModal = () => {
      setNewType(activeTab);
      setIsModalOpen(true);
    };
    window.addEventListener("open-new-client-modal", handleOpenModal);
    return () => window.removeEventListener("open-new-client-modal", handleOpenModal);
  }, [activeTab]);

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
    const formattedDate = `${today.getMonth() + 1}/${today.getDate()}/${today.getFullYear()}`;

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

  const totalCount = records.length;
  const associationCount = records.filter((r) => r.type === "association").length;
  const businessCount = records.filter((r) => r.type === "business").length;
  const activeCount = records.filter((r) => r.status === "ACTIVE").length;

  return (
    <div className="sourcing-hub-wrapper" style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {/* Hero Statistics Bar */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
          gap: "1rem",
        }}
      >
        <div
          style={{
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderLeft: "4px solid #4f46e5",
            borderRadius: "14px",
            padding: "1.15rem 1.25rem",
            display: "flex",
            alignItems: "center",
            gap: "1rem",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div
            style={{
              width: "42px",
              height: "42px",
              borderRadius: "10px",
              background: "rgba(79, 70, 229, 0.08)",
              color: "#4f46e5",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Building2 size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {totalCount}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Total Sourcing Clients
            </div>
          </div>
        </div>

        <div
          style={{
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderLeft: "4px solid #6366f1",
            borderRadius: "14px",
            padding: "1.15rem 1.25rem",
            display: "flex",
            alignItems: "center",
            gap: "1rem",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div
            style={{
              width: "42px",
              height: "42px",
              borderRadius: "10px",
              background: "rgba(99, 102, 241, 0.08)",
              color: "#6366f1",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Users size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {associationCount}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Association Networks
            </div>
          </div>
        </div>

        <div
          style={{
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderLeft: "4px solid #0d9488",
            borderRadius: "14px",
            padding: "1.15rem 1.25rem",
            display: "flex",
            alignItems: "center",
            gap: "1rem",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div
            style={{
              width: "42px",
              height: "42px",
              borderRadius: "10px",
              background: "rgba(13, 148, 136, 0.08)",
              color: "#0d9488",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Briefcase size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {businessCount}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Enterprise Businesses
            </div>
          </div>
        </div>

        <div
          style={{
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderLeft: "4px solid #10b981",
            borderRadius: "14px",
            padding: "1.15rem 1.25rem",
            display: "flex",
            alignItems: "center",
            gap: "1rem",
            boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
          }}
        >
          <div
            style={{
              width: "42px",
              height: "42px",
              borderRadius: "10px",
              background: "rgba(16, 185, 129, 0.08)",
              color: "#10b981",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Users size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {activeCount}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Active Client Status
            </div>
          </div>
        </div>
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
                background: activeTab === "association" ? "#dbeafe" : "#f1f5f9",
                color: activeTab === "association" ? "#2563eb" : "#64748b",
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
                background: activeTab === "business" ? "#ccfbf1" : "#f1f5f9",
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
          filteredRecords.map((item) => (
            <div className="sourcing-card" key={item.id}>
              {/* Card Header: Icon + Menu */}
              <div className="sourcing-card-top">
                <div className="sourcing-card-icon-box">
                  <Building2 size={20} />
                </div>
                <div style={{ position: "relative" }}>
                  <button
                    className="sourcing-menu-btn"
                    onClick={() =>
                      setActiveMenuId((prev) => (prev === item.id ? null : item.id))
                    }
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
              </div>

              {/* Title & Badge */}
              <div className="sourcing-card-title-block">
                <h3 className="sourcing-card-title">{item.name}</h3>
                <span className={`sourcing-badge status-${item.status.toLowerCase()}`}>
                  {item.status}
                </span>
              </div>

              {/* Fields Container */}
              <div className="sourcing-fields-grid">
                {/* Contact & Phone row */}
                <div className="sourcing-field-row-split">
                  <div className="sourcing-field-box">
                    <div className="field-label-wrapper">
                      <User size={14} className="field-icon icon-blue" />
                      <span className="field-label">CONTACT</span>
                    </div>
                    <div className="field-value">{item.contact}</div>
                  </div>

                  <div className="sourcing-field-box">
                    <div className="field-label-wrapper">
                      <Phone size={14} className="field-icon icon-teal" />
                      <span className="field-label">PHONE</span>
                    </div>
                    <div className="field-value">{item.phone}</div>
                  </div>
                </div>

                {/* Email row */}
                <div className="sourcing-field-box email-box">
                  <div className="field-label-wrapper">
                    <div className="mail-icon-bg">
                      <Mail size={13} className="field-icon icon-blue" />
                    </div>
                    <span className="field-label">EMAIL ADDRESS</span>
                  </div>
                  <div className="field-value">{item.email}</div>
                </div>
              </div>

              {/* Card Footer */}
              <div className="sourcing-card-footer">
                <span className="sourcing-card-id">ID: {item.id}</span>
                <div className="sourcing-card-date">
                  <Calendar size={14} />
                  <span>{item.date}</span>
                </div>
              </div>
            </div>
          ))
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
            <div className="modal-header" style={{ padding: "1.5rem 1.75rem", borderBottom: "1px solid #f1f5f9" }}>
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

              <div className="modal-footer" style={{ padding: "1.25rem 1.75rem", borderTop: "1px solid #f1f5f9" }}>
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
