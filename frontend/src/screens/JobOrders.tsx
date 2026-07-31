"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  Plus,
  Search,
  Building2,
  Users,
  IndianRupee,
  Calendar,
  X,
  Trash2,
  MoreVertical,
  Briefcase,
  ChevronDown,
  ArrowLeft,
  Pencil,
  CheckCircle,
  Sparkles,
  Check,
  UserCheck,
} from "lucide-react";

import {
  listCandidates,
  listJobOrdersAPI,
  createJobOrderAPI,
  updateJobOrderAPI,
  deleteJobOrderAPI,
  type CandidateRecord,
} from "@/lib/api";

export interface JobOrderRecord {
  id: string;
  title: string;
  client: string;
  headcount: number;
  salary: string;
  skills: string[];
  description?: string;
  dueDate: string;
  status: "OPEN" | "IN PROGRESS" | "FILLED" | "CLOSED";
  minExperience?: string;
  industry?: string;
  designation?: string;
  fulfilledCount?: number;
  shortlistedCandidateIds?: string[];
}

const DEFAULT_JOB_ORDERS: JobOrderRecord[] = [];

const DEFAULT_CLIENT_OPTIONS: string[] = [];

// Helper: parse min experience years from string
function parseMinExperienceYears(expStr?: string): number {
  if (!expStr) return 0;
  const lower = expStr.toLowerCase();
  if (lower.includes("fresher") || lower.includes("any") || lower.includes("0")) return 0;
  const match = expStr.match(/\d+/);
  return match ? parseInt(match[0], 10) : 0;
}

// Matching Engine Score Calculation
export interface MatchResult {
  candidate: CandidateRecord;
  matchScore: number; // 0 to 100%
  matchedSkills: string[];
  expMatched: boolean;
  roleMatched: boolean;
  isSelected: boolean;
}

export function calculateCandidateMatch(
  order: JobOrderRecord,
  candidate: CandidateRecord,
  shortlistedIds: string[] = []
): MatchResult {
  const profile = candidate.profile || {};
  const candSkills = [
    ...(profile.skills || []),
    ...(profile.technical_skills || []),
    ...(profile.languages || []),
  ].map((s) => s.toLowerCase());

  const candSummary = (profile.resume_summary || "").toLowerCase();
  const candRole = (profile.current_designation || "").toLowerCase();
  const workExpText = (profile.work_experience || [])
    .map((w) => `${w.designation || ""} ${w.company || ""} ${w.description || ""}`)
    .join(" ")
    .toLowerCase();

  const reqSkills = order.skills || [];
  const matchedSkills: string[] = [];

  reqSkills.forEach((skill) => {
    const sLower = skill.toLowerCase();
    const isMatched =
      candSkills.some((cs) => cs.includes(sLower) || sLower.includes(cs)) ||
      candSummary.includes(sLower) ||
      workExpText.includes(sLower);

    if (isMatched) {
      matchedSkills.push(skill);
    }
  });

  // 1. Skill Match Weight (50%)
  const skillRatio = reqSkills.length > 0 ? matchedSkills.length / reqSkills.length : 0.5;
  const skillScore = skillRatio * 50;

  // 2. Experience Match Weight (25%)
  const reqExpYears = parseMinExperienceYears(order.minExperience);
  const candExpYears = profile.total_experience_years || 0;
  let expScore = 25;
  let expMatched = true;

  if (reqExpYears > 0) {
    if (candExpYears >= reqExpYears) {
      expScore = 25;
    } else {
      expScore = Math.max(5, (candExpYears / reqExpYears) * 25);
      expMatched = false;
    }
  }

  // 3. Designation / Industry Weight (25%)
  const targetRole = (order.designation || order.title || "").toLowerCase();
  const targetIndustry = (order.industry || "").toLowerCase();

  let roleScore = 10;
  let roleMatched = false;

  if (targetRole) {
    const roleTokens = targetRole.split(/\s+/).filter((t) => t.length > 2);
    const matchesToken = roleTokens.some(
      (tok) => candRole.includes(tok) || workExpText.includes(tok) || candSummary.includes(tok)
    );
    if (matchesToken) {
      roleScore = 25;
      roleMatched = true;
    }
  }

  if (targetIndustry && candSummary.includes(targetIndustry)) {
    roleScore = Math.min(25, roleScore + 10);
  }

  // Final Percentage Calculation
  let finalScore = Math.round(skillScore + expScore + roleScore);
  if (matchedSkills.length === 0 && !roleMatched) {
    finalScore = Math.min(finalScore, 35);
  }
  finalScore = Math.max(20, Math.min(99, finalScore));

  // Perfect match bonus if skills and role match
  if (matchedSkills.length === reqSkills.length && reqSkills.length > 0 && roleMatched) {
    finalScore = 100;
  }

  return {
    candidate,
    matchScore: finalScore,
    matchedSkills,
    expMatched,
    roleMatched,
    isSelected: shortlistedIds.includes(candidate.id),
  };
}

interface JobOrdersProps {
  candidates?: CandidateRecord[];
}

export default function JobOrders({ candidates: initialCandidates = [] }: JobOrdersProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [orders, setOrders] = useState<JobOrderRecord[]>(DEFAULT_JOB_ORDERS);

  const filteredOrders = useMemo(() => {
    if (!searchQuery.trim()) return orders;
    const q = searchQuery.toLowerCase();
    return orders.filter(
      (ord) =>
        ord.title.toLowerCase().includes(q) ||
        ord.client.toLowerCase().includes(q) ||
        (ord.skills || []).some((s) => s.toLowerCase().includes(q))
    );
  }, [orders, searchQuery]);
  const [selectedOrder, setSelectedOrder] = useState<JobOrderRecord | null>(null);
  const [dbCandidates, setDbCandidates] = useState<CandidateRecord[]>(initialCandidates);

  // Dynamic Client Options from Sourcing Hub Database API
  const [clientOptions, setClientOptions] = useState<string[]>([]);

  // Modals state
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState(false);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);

  // Form state for "Create New Order"
  const [selectedClient, setSelectedClient] = useState("");
  const [designationRole, setDesignationRole] = useState("");
  const [minExperience, setMinExperience] = useState("");
  const [industry, setIndustry] = useState("");
  const [requiredSkills, setRequiredSkills] = useState("");
  const [salaryRange, setSalaryRange] = useState("");
  const [internalNotes, setInternalNotes] = useState("");

  // Form state for "Edit Job Order"
  const [editTitle, setEditTitle] = useState("");
  const [editHeadcount, setEditHeadcount] = useState("1");
  const [editDueDate, setEditDueDate] = useState("");
  const [editIndustry, setEditIndustry] = useState("");
  const [editDesignation, setEditDesignation] = useState("");
  const [editMinExperience, setEditMinExperience] = useState("");
  const [editSalary, setEditSalary] = useState("");
  const [editSkills, setEditSkills] = useState("");
  const [editRemarks, setEditRemarks] = useState("");

  // Fetch job orders from DB API
  useEffect(() => {
    // 1. Sync local orders to MongoDB Atlas API
    if (typeof window !== "undefined") {
      const savedOrders = localStorage.getItem("job_orders_records");
      if (savedOrders) {
        try {
          const localOrders: JobOrderRecord[] = JSON.parse(savedOrders);
          localOrders.forEach((ord) => {
            createJobOrderAPI(ord).catch(() => {});
          });
        } catch {}
      }
    }

    // 2. Fetch latest job orders from MongoDB Atlas API
    listJobOrdersAPI()
      .then((res) => {
        if (res.items && res.items.length > 0) {
          setOrders(res.items);
          if (typeof window !== "undefined") {
            localStorage.setItem("job_orders_records", JSON.stringify(res.items));
          }
        } else if (typeof window !== "undefined") {
          const savedOrders = localStorage.getItem("job_orders_records");
          if (savedOrders) {
            try {
              setOrders(JSON.parse(savedOrders));
            } catch {}
          }
        }
      })
      .catch(() => {
        if (typeof window !== "undefined") {
          const savedOrders = localStorage.getItem("job_orders_records");
          if (savedOrders) {
            try {
              setOrders(JSON.parse(savedOrders));
            } catch {}
          }
        }
      });
  }, []);

  // Load backend database candidates if prop is empty
  useEffect(() => {
    if (initialCandidates.length > 0) {
      setDbCandidates(initialCandidates);
    } else {
      listCandidates()
        .then((res) => setDbCandidates(res.items))
        .catch(() => {});
    }
  }, [initialCandidates]);

  // Load clients directly from Sourcing Hub Database API
  useEffect(() => {
    const fetchSourcingClients = () => {
      import("@/lib/api").then(({ listSourcingClientsAPI }) => {
        listSourcingClientsAPI()
          .then((res) => {
            if (res.items && res.items.length > 0) {
              const names = res.items.map((r: { name: string }) => r.name).filter(Boolean);
              setClientOptions(Array.from(new Set(names)));
            } else if (typeof window !== "undefined") {
              const saved = localStorage.getItem("sourcing_records");
              if (saved) {
                try {
                  const records = JSON.parse(saved);
                  const names = records.map((r: { name: string }) => r.name).filter(Boolean);
                  setClientOptions(Array.from(new Set(names)));
                } catch {}
              }
            }
          })
          .catch(() => {
            if (typeof window !== "undefined") {
              const saved = localStorage.getItem("sourcing_records");
              if (saved) {
                try {
                  const records = JSON.parse(saved);
                  const names = records.map((r: { name: string }) => r.name).filter(Boolean);
                  setClientOptions(Array.from(new Set(names)));
                } catch {}
              }
            }
          });
      });
    };

    fetchSourcingClients();
  }, [isCreateModalOpen, isEditModalOpen]);

  // Sync saved Job Orders from localStorage
  useEffect(() => {
    if (typeof window !== "undefined") {
      const savedOrders = localStorage.getItem("job_orders_records");
      if (savedOrders) {
        try {
          setOrders(JSON.parse(savedOrders));
        } catch {}
      }
    }
  }, []);

  const saveOrdersToStorage = (updated: JobOrderRecord[]) => {
    setOrders(updated);
    if (typeof window !== "undefined") {
      try {
        localStorage.setItem("job_orders_records", JSON.stringify(updated));
      } catch {}
    }
  };

  useEffect(() => {
    const handleOpenModal = () => setIsCreateModalOpen(true);
    window.addEventListener("open-new-order-modal", handleOpenModal);
    return () => window.removeEventListener("open-new-order-modal", handleOpenModal);
  }, []);

  // Open Edit Modal pre-filled with selectedOrder values
  const openEditModal = (item: JobOrderRecord) => {
    setEditTitle(item.title);
    setEditHeadcount(String(item.headcount));
    setEditDueDate(item.dueDate);
    setEditIndustry(item.industry || "");
    setEditDesignation(item.designation || item.title);
    setEditMinExperience(item.minExperience || "");
    setEditSalary(item.salary);
    setEditSkills(item.skills.join(", "));
    setEditRemarks(item.description || "");
    setIsEditModalOpen(true);
  };

  const handleUpdateOrder = (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedOrder) return;

    const parsedSkills = editSkills
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const updated: JobOrderRecord = {
      ...selectedOrder,
      title: editTitle,
      headcount: parseInt(editHeadcount, 10) || 1,
      dueDate: editDueDate || "7/31/2026",
      industry: editIndustry || "General",
      designation: editDesignation || editTitle,
      minExperience: editMinExperience || "Any",
      salary: editSalary || "0",
      skills: parsedSkills.length > 0 ? parsedSkills : ["General"],
      description: editRemarks,
    };

    updateJobOrderAPI(updated.id, updated).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === updated.id ? updated : ord));
    saveOrdersToStorage(newOrders);
    setSelectedOrder(updated);
    setIsEditModalOpen(false);
  };

  const handleToggleCloseOrder = (item: JobOrderRecord) => {
    const newStatus = item.status === "CLOSED" ? "OPEN" : "CLOSED";
    const updated = { ...item, status: newStatus as "OPEN" | "CLOSED" };
    updateJobOrderAPI(updated.id, updated).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === updated.id ? updated : ord));
    saveOrdersToStorage(newOrders);
    setSelectedOrder(updated);
  };

  const handleToggleShortlistCandidate = (orderId: string, candidateId: string) => {
    const currentOrder = orders.find((o) => o.id === orderId);
    if (!currentOrder) return;

    const currentShortlisted = currentOrder.shortlistedCandidateIds || [];
    const isCurrentlySelected = currentShortlisted.includes(candidateId);

    const newShortlisted = isCurrentlySelected
      ? currentShortlisted.filter((id) => id !== candidateId)
      : [...currentShortlisted, candidateId];

    const updatedOrder: JobOrderRecord = {
      ...currentOrder,
      shortlistedCandidateIds: newShortlisted,
      fulfilledCount: newShortlisted.length,
    };

    updateJobOrderAPI(orderId, updatedOrder).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === orderId ? updatedOrder : ord));
    saveOrdersToStorage(newOrders);

    if (selectedOrder?.id === orderId) {
      setSelectedOrder(updatedOrder);
    }
  };

  const handleCreateOrder = (e: React.FormEvent) => {
    e.preventDefault();
    if (!designationRole.trim()) return;

    const uniqueNum = Math.floor(100 + Math.random() * 900);
    const id = `ORD-${uniqueNum}-${Date.now().toString().slice(-4)}`;
    const parsedSkills = requiredSkills
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const newRecord: JobOrderRecord = {
      id,
      title: designationRole,
      client: selectedClient || clientOptions[0] || "activ",
      headcount: 1,
      fulfilledCount: 0,
      salary: salaryRange || "900",
      skills: parsedSkills.length > 0 ? parsedSkills : ["General"],
      description: internalNotes || "None",
      dueDate: "7/31/2026",
      status: "OPEN",
      minExperience: minExperience || "Any",
      industry: industry || "Test",
      designation: designationRole,
      shortlistedCandidateIds: [],
    };

    createJobOrderAPI(newRecord).catch(() => {});
    saveOrdersToStorage([newRecord, ...orders]);
    setIsCreateModalOpen(false);

    // Reset form fields
    setSelectedClient("");
    setDesignationRole("");
    setMinExperience("");
    setIndustry("");
    setRequiredSkills("");
    setSalaryRange("");
    setInternalNotes("");
  };

  const handleDeleteOrder = (id: string) => {
    deleteJobOrderAPI(id).catch(() => {});
    const updated = orders.filter((o) => o.id !== id);
    saveOrdersToStorage(updated);
    if (selectedOrder?.id === id) {
      setSelectedOrder(null);
    }
    setActiveMenuId(null);
  };

  // Hero statistics bar calculations
  const totalOrdersCount = orders.length;
  const activeOrdersCount = orders.filter((o) => o.status === "OPEN" || o.status === "IN PROGRESS").length;
  const totalHeadcount = orders.reduce((sum, o) => sum + (o.headcount || 1), 0);
  const totalShortlisted = orders.reduce((sum, o) => sum + (o.shortlistedCandidateIds?.length || o.fulfilledCount || 0), 0);

  // Compute matched candidates from Database OCR for selectedOrder
  const matchedCandidateResults = useMemo(() => {
    if (!selectedOrder) return [];
    const shortlistedIds = selectedOrder.shortlistedCandidateIds || [];

    if (dbCandidates.length > 0) {
      return dbCandidates
        .map((cand) => calculateCandidateMatch(selectedOrder, cand, shortlistedIds))
        .sort((a, b) => b.matchScore - a.matchScore);
    }

    return [];
  }, [selectedOrder, dbCandidates]);

  // -------------------------------------------------------------
  // RENDER DETAILED VIEW IF AN ORDER IS SELECTED
  // -------------------------------------------------------------
  if (selectedOrder) {
    const shortlistedIds = selectedOrder.shortlistedCandidateIds || [];
    const fulfilled = shortlistedIds.length || selectedOrder.fulfilledCount || 0;
    const totalHead = selectedOrder.headcount || 1;
    const progressPct = Math.min(100, Math.round((fulfilled / totalHead) * 100));

    return (
      <div className="job-order-detail-wrapper" style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
        {/* Back Button */}
        <div>
          <button
            onClick={() => setSelectedOrder(null)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: "38px",
              height: "38px",
              borderRadius: "10px",
              background: "#ffffff",
              border: "1px solid #e2e8f0",
              color: "#475569",
              cursor: "pointer",
              boxShadow: "0 1px 2px rgba(0,0,0,0.03)",
              transition: "all 0.2s ease",
            }}
            title="Back to Job Orders list"
          >
            <ArrowLeft size={18} />
          </button>
        </div>

        {/* Top Details Card */}
        <div
          style={{
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderRadius: "20px",
            padding: "1.75rem",
            display: "flex",
            flexDirection: "column",
            gap: "1.5rem",
            boxShadow: "0 1px 3px rgba(0,0,0,0.04)",
          }}
        >
          {/* Card Top Row: Badges / Actions (Left) + Fulfillment Progress (Right) */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", flexWrap: "wrap", gap: "1rem" }}>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              {/* Badges & Actions */}
              <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
                <span style={{ background: "#dbeafe", color: "#2563eb", fontSize: "0.75rem", fontWeight: 700, padding: "3px 10px", borderRadius: "6px" }}>
                  {selectedOrder.id}
                </span>

                <span
                  style={{
                    background: selectedOrder.status === "OPEN" ? "#fef3c7" : "#f1f5f9",
                    color: selectedOrder.status === "OPEN" ? "#d97706" : "#64748b",
                    fontSize: "0.75rem",
                    fontWeight: 700,
                    padding: "3px 10px",
                    borderRadius: "6px",
                    textTransform: "uppercase",
                  }}
                >
                  {selectedOrder.status}
                </span>

                <button
                  onClick={() => handleToggleCloseOrder(selectedOrder)}
                  style={{
                    background: "#0f172a",
                    color: "#ffffff",
                    border: "none",
                    fontSize: "0.75rem",
                    fontWeight: 700,
                    padding: "4px 12px",
                    borderRadius: "6px",
                    cursor: "pointer",
                    letterSpacing: "0.5px",
                  }}
                >
                  {selectedOrder.status === "CLOSED" ? "REOPEN ORDER" : "CLOSE ORDER"}
                </button>

                <button
                  onClick={() => openEditModal(selectedOrder)}
                  style={{
                    background: "#ffffff",
                    color: "#475569",
                    border: "1px solid #cbd5e1",
                    fontSize: "0.75rem",
                    fontWeight: 700,
                    padding: "4px 12px",
                    borderRadius: "6px",
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: "4px",
                  }}
                >
                  <Pencil size={13} />
                  <span>EDIT</span>
                </button>

                <button
                  onClick={() => handleDeleteOrder(selectedOrder.id)}
                  style={{
                    background: "#fee2e2",
                    color: "#dc2626",
                    border: "none",
                    fontSize: "0.75rem",
                    fontWeight: 700,
                    padding: "4px 12px",
                    borderRadius: "6px",
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    gap: "4px",
                  }}
                >
                  <Trash2 size={13} />
                  <span>DELETE</span>
                </button>
              </div>

              {/* Job Title & Client */}
              <div>
                <h1 style={{ fontFamily: "var(--font-outfit), sans-serif", fontSize: "1.75rem", fontWeight: 700, color: "#0f172a", margin: "0.2rem 0" }}>
                  {selectedOrder.title}
                </h1>
                <div style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", fontSize: "0.9rem", fontWeight: 600, color: "#2563eb" }}>
                  <Building2 size={15} />
                  <span>{selectedOrder.client}</span>
                </div>
              </div>
            </div>

            {/* Fulfillment Progress Bar */}
            <div style={{ minWidth: "220px", display: "flex", flexDirection: "column", gap: "0.4rem", alignItems: "flex-end" }}>
              <div style={{ display: "flex", gap: "2rem", fontSize: "0.8rem", fontWeight: 700 }}>
                <span style={{ color: "#64748b" }}>Fulfillment Progress</span>
                <span style={{ color: "#2563eb" }}>{fulfilled} / {totalHead}</span>
              </div>
              <div style={{ width: "100%", height: "8px", background: "#f1f5f9", borderRadius: "999px", overflow: "hidden" }}>
                <div style={{ width: `${progressPct}%`, height: "100%", background: "#3b82f6", borderRadius: "999px", transition: "width 0.3s ease" }}></div>
              </div>
            </div>
          </div>

          {/* Specifications Inner Box */}
          <div
            style={{
              background: "#ffffff",
              border: "1px solid #f1f5f9",
              borderRadius: "14px",
              padding: "1.25rem 1.5rem",
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
              gap: "1.25rem 1.5rem",
              boxShadow: "0 1px 2px rgba(0,0,0,0.02)",
            }}
          >
            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                REQUIRED SKILLS
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#0f172a", marginTop: "4px" }}>
                {selectedOrder.skills.join(", ")}
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                INDUSTRY
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#0f172a", marginTop: "4px" }}>
                {selectedOrder.industry || "Test"}
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                DESIGNATION
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#0f172a", marginTop: "4px" }}>
                {selectedOrder.designation || selectedOrder.title}
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                MIN. EXP.
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#0f172a", marginTop: "4px" }}>
                {selectedOrder.minExperience || "Any"}
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                EXPECTED SALARY
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#0f172a", marginTop: "4px" }}>
                ₹ {selectedOrder.salary}
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                DUE DATE
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#e11d48", marginTop: "4px", display: "flex", alignItems: "center", gap: "4px" }}>
                <Calendar size={13} />
                <span>{selectedOrder.dueDate}</span>
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                REMARKS
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#0f172a", marginTop: "4px" }}>
                {selectedOrder.description || "None"}
              </div>
            </div>
          </div>
        </div>

        {/* Candidate Matching Engine Section with Real-Time Database Matches */}
        <div style={{ display: "flex", flexDirection: "column", gap: "1rem", marginTop: "0.5rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "1.15rem", fontWeight: 700, color: "#0f172a" }}>
              <Sparkles size={18} style={{ color: "#2563eb" }} />
              <span>Candidate Matching Engine</span>
            </div>
            <span style={{ background: "#ffffff", border: "1px solid #e2e8f0", color: "#64748b", fontSize: "0.8rem", fontWeight: 600, padding: "4px 12px", borderRadius: "999px" }}>
              {matchedCandidateResults.length} candidate matches calculated
            </span>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: "0.85rem" }}>
            {matchedCandidateResults.map((res) => {
              const profile = res.candidate.profile || {};
              const name = profile.full_name || res.candidate.source_email?.from_name || "Extracted Candidate";
              const expYears = profile.total_experience_years ? `${profile.total_experience_years} Years exp` : "Fresher";

              return (
                <div
                  key={res.candidate.id}
                  style={{
                    background: "#ffffff",
                    border: res.isSelected ? "2px solid #22c55e" : "1px solid #e2e8f0",
                    borderRadius: "16px",
                    padding: "1.25rem 1.5rem",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: "1rem",
                    boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
                    transition: "all 0.2s ease",
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
                    <div
                      style={{
                        width: "46px",
                        height: "46px",
                        borderRadius: "50%",
                        background: res.matchScore >= 80 ? "#dbeafe" : "#f1f5f9",
                        color: res.matchScore >= 80 ? "#2563eb" : "#475569",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        fontSize: "1.1rem",
                        fontWeight: 700,
                      }}
                    >
                      {name.charAt(0).toUpperCase()}
                    </div>

                    <div>
                      <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
                        <span style={{ fontSize: "1rem", fontWeight: 700, color: "#0f172a" }}>{name}</span>
                        <span style={{ background: "#dcfce7", color: "#16a34a", fontSize: "0.75rem", fontWeight: 700, padding: "2px 8px", borderRadius: "999px" }}>
                          {res.matchScore}% MATCH
                        </span>
                      </div>
                      <div style={{ fontSize: "0.82rem", color: "#64748b", marginTop: "2px" }}>
                        {profile.current_designation || "Candidate"} • {expYears}
                      </div>
                    </div>
                  </div>

                  <button
                    onClick={() => handleToggleShortlistCandidate(selectedOrder.id, res.candidate.id)}
                    style={{
                      background: res.isSelected ? "#22c55e" : "#2563eb",
                      color: "#ffffff",
                      border: "none",
                      padding: "0.5rem 1.15rem",
                      borderRadius: "10px",
                      fontSize: "0.85rem",
                      fontWeight: 600,
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      gap: "0.4rem",
                      boxShadow: res.isSelected ? "0 2px 6px rgba(34, 197, 94, 0.3)" : "0 2px 6px rgba(37, 99, 235, 0.25)",
                    }}
                  >
                    {res.isSelected ? (
                      <>
                        <CheckCircle size={16} />
                        <span>Shortlisted</span>
                      </>
                    ) : (
                      <>
                        <Plus size={16} />
                        <span>Shortlist Candidate</span>
                      </>
                    )}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    );
  }

  // -------------------------------------------------------------
  // RENDER CARDS LIST VIEW (Default Screen)
  // -------------------------------------------------------------
  return (
    <div className="job-orders-wrapper" style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
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
            borderLeft: "4px solid #2563eb",
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
              background: "rgba(37, 99, 235, 0.08)",
              color: "#2563eb",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Briefcase size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {totalOrdersCount}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Total Job Orders
            </div>
          </div>
        </div>

        <div
          style={{
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderLeft: "4px solid #d97706",
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
              background: "rgba(217, 119, 6, 0.08)",
              color: "#d97706",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Sparkles size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {activeOrdersCount}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Active Orders Open
            </div>
          </div>
        </div>

        <div
          style={{
            background: "#ffffff",
            border: "1px solid #e2e8f0",
            borderLeft: "4px solid #8b5cf6",
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
              background: "rgba(139, 92, 246, 0.08)",
              color: "#8b5cf6",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
            }}
          >
            <Users size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {totalHeadcount}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Positions Headcount
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
            <UserCheck size={20} />
          </div>
          <div>
            <div style={{ fontSize: "1.4rem", fontWeight: 700, color: "#0f172a", fontFamily: "var(--font-outfit), sans-serif", lineHeight: 1 }}>
              {totalShortlisted}
            </div>
            <div style={{ fontSize: "0.78rem", fontWeight: 600, color: "#64748b", marginTop: "0.25rem" }}>
              Shortlisted Candidates
            </div>
          </div>
        </div>
      </div>
      {/* Search & Action Controls Row */}
      <div className="job-orders-controls-row" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "1rem", marginBottom: "1.5rem", flexWrap: "wrap" }}>
        <div className="job-orders-search-wrapper" style={{ position: "relative", flexGrow: 1, minWidth: "260px" }}>
          <Search size={18} className="job-orders-search-icon" style={{ position: "absolute", left: "1rem", top: "50%", transform: "translateY(-50%)", color: "#94a3b8" }} />
          <input
            type="text"
            className="job-orders-search-input"
            placeholder="Search by job title or required skills..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              width: "100%",
              background: "#ffffff",
              border: "1px solid #e2e8f0",
              borderRadius: "12px",
              padding: "0.75rem 1rem 0.75rem 2.65rem",
              fontSize: "0.9rem",
              color: "#0f172a",
              outline: "none",
              boxShadow: "0 1px 2px rgba(0,0,0,0.03)",
            }}
          />
          {searchQuery && (
            <button
              className="job-orders-search-clear"
              onClick={() => setSearchQuery("")}
              style={{ position: "absolute", right: "1rem", top: "50%", transform: "translateY(-50%)", background: "none", border: "none", color: "#94a3b8", cursor: "pointer" }}
            >
              <X size={16} />
            </button>
          )}
        </div>

        <button
          className="btn-new-client"
          onClick={() => setIsCreateModalOpen(true)}
        >
          <Plus size={16} />
          <span>Create New Order</span>
        </button>
      </div>

      {/* Cards Grid */}
      <div className="job-orders-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: "1.25rem" }}>
        {filteredOrders.length === 0 ? (
          <div className="job-orders-empty" style={{ gridColumn: "1 / -1", textAlign: "center", padding: "3rem 1rem", background: "white", border: "1px dashed #cbd5e1", borderRadius: "16px", color: "#94a3b8" }}>
            <Briefcase size={40} style={{ marginBottom: "0.5rem" }} />
            <p>No job orders found matching your search.</p>
          </div>
        ) : (
          filteredOrders.map((item) => {
            const fulfilled = (item.shortlistedCandidateIds || []).length || item.fulfilledCount || 0;
            return (
              <div
                className="job-order-card"
                key={item.id}
                onClick={() => setSelectedOrder(item)}
                style={{
                  background: "#ffffff",
                  border: "1px solid #e2e8f0",
                  borderRadius: "16px",
                  padding: "1.35rem",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.85rem",
                  boxShadow: "0 1px 3px rgba(0,0,0,0.04)",
                  position: "relative",
                  cursor: "pointer",
                  transition: "all 0.2s ease",
                }}
              >
                {/* Header: Title + Badge + Options */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: "0.5rem" }}>
                  <div>
                    <h3 style={{ fontFamily: "var(--font-outfit), sans-serif", fontSize: "1.25rem", fontWeight: 700, color: "#0f172a", marginBottom: "0.35rem" }}>
                      {item.title}
                    </h3>
                    {/* Client Sub-tag */}
                    <div
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "0.35rem",
                        background: "#f8fafc",
                        border: "1px solid #e2e8f0",
                        borderRadius: "6px",
                        padding: "2px 8px",
                        fontSize: "0.8rem",
                        fontWeight: 600,
                        color: "#475569",
                      }}
                    >
                      <Building2 size={13} style={{ color: "#3b82f6" }} />
                      <span>{item.client}</span>
                    </div>
                  </div>

                  <div style={{ display: "flex", alignItems: "center", gap: "0.35rem" }}>
                    <span
                      style={{
                        fontSize: "0.7rem",
                        fontWeight: 700,
                        padding: "3px 8px",
                        borderRadius: "6px",
                        textTransform: "uppercase",
                        letterSpacing: "0.5px",
                        background: item.status === "OPEN" ? "#fef3c7" : "#dcfce7",
                        color: item.status === "OPEN" ? "#d97706" : "#16a34a",
                      }}
                    >
                      {item.status}
                    </span>

                    <div style={{ position: "relative" }}>
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          setActiveMenuId((prev) => (prev === item.id ? null : item.id));
                        }}
                        style={{ background: "none", border: "none", color: "#94a3b8", cursor: "pointer", padding: "2px" }}
                      >
                        <MoreVertical size={16} />
                      </button>

                      {activeMenuId === item.id && (
                        <div className="sourcing-dropdown-menu">
                          <button
                            className="dropdown-item danger"
                            onClick={(e) => {
                              e.stopPropagation();
                              handleDeleteOrder(item.id);
                            }}
                          >
                            <Trash2 size={14} />
                            <span>Delete</span>
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                {/* Headcount & Salary Split Boxes */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem", marginTop: "0.25rem" }}>
                  <div style={{ background: "#ffffff", border: "1px solid #f1f5f9", borderRadius: "10px", padding: "0.65rem 0.85rem", boxShadow: "0 1px 2px rgba(0,0,0,0.02)" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                      <Users size={14} style={{ color: "#a855f7" }} />
                      <span style={{ fontSize: "0.65rem", fontWeight: 700, color: "#64748b", letterSpacing: "0.5px" }}>HEADCOUNT</span>
                    </div>
                    <div style={{ fontSize: "0.95rem", fontWeight: 700, color: "#0f172a", marginTop: "2px" }}>
                      {fulfilled} / {item.headcount} required
                    </div>
                  </div>

                  <div style={{ background: "#ffffff", border: "1px solid #f1f5f9", borderRadius: "10px", padding: "0.65rem 0.85rem", boxShadow: "0 1px 2px rgba(0,0,0,0.02)" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                      <IndianRupee size={14} style={{ color: "#10b981" }} />
                      <span style={{ fontSize: "0.65rem", fontWeight: 700, color: "#64748b", letterSpacing: "0.5px" }}>SALARY</span>
                    </div>
                    <div style={{ fontSize: "0.95rem", fontWeight: 700, color: "#0f172a", marginTop: "2px" }}>
                      {item.salary}
                    </div>
                  </div>
                </div>

                {/* Required Skills & Description */}
                <div style={{ fontSize: "0.825rem", color: "#475569", marginTop: "0.15rem" }}>
                  <span style={{ fontWeight: 700, color: "#0f172a" }}>Skills: </span>
                  <span>{item.skills.join(", ")}</span>
                  {item.description && (
                    <div style={{ fontStyle: "italic", color: "#64748b", fontSize: "0.775rem", marginTop: "0.15rem" }}>
                      {item.description}
                    </div>
                  )}
                </div>

                {/* Card Footer: Due Date & Order ID */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", paddingTop: "0.5rem", marginTop: "0.25rem", borderTop: "1px solid #f8fafc" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.35rem", color: "#2563eb", fontSize: "0.8rem", fontWeight: 700 }}>
                    <Calendar size={14} />
                    <span>Due: {item.dueDate}</span>
                  </div>
                  <span style={{ color: "#94a3b8", fontSize: "0.775rem", fontWeight: 500 }}>ID: {item.id}</span>
                </div>
              </div>
            );
          })
        )}
      </div>

      {/* Edit Job Order Pop-up Modal */}
      {isEditModalOpen && (
        <div className="cm-overlay active" onClick={() => setIsEditModalOpen(false)}>
          <div
            className="cm-dialog client-modal-dialog"
            style={{ maxWidth: "600px", borderRadius: "16px", padding: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-header" style={{ padding: "1.5rem 1.75rem", borderBottom: "1px solid #f1f5f9" }}>
              <div>
                <h3 className="modal-title" style={{ fontSize: "1.4rem", fontWeight: 700 }}>
                  Edit Job Order
                </h3>
              </div>
              <button className="modal-close-btn" onClick={() => setIsEditModalOpen(false)}>
                <X size={20} />
              </button>
            </div>

            <form onSubmit={handleUpdateOrder}>
              <div className="modal-body" style={{ padding: "1.75rem", display: "flex", flexDirection: "column", gap: "1.25rem" }}>
                <div>
                  <label className="modal-label">Job Title</label>
                  <input
                    type="text"
                    className="modal-input"
                    value={editTitle}
                    onChange={(e) => setEditTitle(e.target.value)}
                    required
                  />
                </div>

                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">Headcount</label>
                    <input
                      type="number"
                      min="1"
                      className="modal-input"
                      value={editHeadcount}
                      onChange={(e) => setEditHeadcount(e.target.value)}
                      required
                    />
                  </div>
                  <div>
                    <label className="modal-label">Due Date</label>
                    <input
                      type="text"
                      className="modal-input"
                      value={editDueDate}
                      onChange={(e) => setEditDueDate(e.target.value)}
                    />
                  </div>
                </div>

                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">Required Industry</label>
                    <input
                      type="text"
                      className="modal-input"
                      value={editIndustry}
                      onChange={(e) => setEditIndustry(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="modal-label">Required Designation</label>
                    <input
                      type="text"
                      className="modal-input"
                      value={editDesignation}
                      onChange={(e) => setEditDesignation(e.target.value)}
                    />
                  </div>
                </div>

                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">Min. Experience (Years)</label>
                    <input
                      type="text"
                      className="modal-input"
                      value={editMinExperience}
                      onChange={(e) => setEditMinExperience(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="modal-label">Expected Salary</label>
                    <input
                      type="text"
                      className="modal-input"
                      value={editSalary}
                      onChange={(e) => setEditSalary(e.target.value)}
                    />
                  </div>
                </div>

                <div>
                  <label className="modal-label">Required Skills</label>
                  <input
                    type="text"
                    className="modal-input"
                    value={editSkills}
                    onChange={(e) => setEditSkills(e.target.value)}
                  />
                </div>

                <div>
                  <label className="modal-label">Remarks</label>
                  <textarea
                    className="modal-textarea"
                    value={editRemarks}
                    onChange={(e) => setEditRemarks(e.target.value)}
                  />
                </div>
              </div>

              <div className="modal-footer" style={{ padding: "1.25rem 1.75rem", borderTop: "1px solid #f1f5f9" }}>
                <button
                  type="button"
                  className="modal-cancel-btn"
                  onClick={() => setIsEditModalOpen(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="modal-submit-btn">
                  Update
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Create New Order Pop-up Modal */}
      {isCreateModalOpen && (
        <div className="cm-overlay active" onClick={() => setIsCreateModalOpen(false)}>
          <div
            className="cm-dialog client-modal-dialog"
            style={{ maxWidth: "620px", borderRadius: "16px", padding: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div className="modal-header" style={{ padding: "1.5rem 1.75rem", borderBottom: "1px solid #f1f5f9" }}>
              <div>
                <h3 className="modal-title" style={{ fontSize: "1.4rem", fontWeight: 700 }}>
                  Create New Order
                </h3>
                <p className="modal-subtitle" style={{ fontSize: "0.85rem", color: "#64748b", marginTop: "0.2rem" }}>
                  Fill in the requisition details below
                </p>
              </div>
              <button className="modal-close-btn" onClick={() => setIsCreateModalOpen(false)}>
                <X size={20} />
              </button>
            </div>

            <form onSubmit={handleCreateOrder}>
              <div className="modal-body" style={{ padding: "1.75rem", display: "flex", flexDirection: "column", gap: "1.25rem" }}>
                {/* Row 1: Client (Company) & Designation / Role */}
                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">Client (Company)</label>
                    <div style={{ position: "relative" }}>
                      <input
                        type="text"
                        list="job-client-datalist"
                        className="modal-input"
                        placeholder="Select or type Client name..."
                        required
                        value={selectedClient}
                        onChange={(e) => setSelectedClient(e.target.value)}
                      />
                      <datalist id="job-client-datalist">
                        {clientOptions.map((client) => (
                          <option key={client} value={client} />
                        ))}
                      </datalist>
                    </div>
                  </div>

                  <div>
                    <label className="modal-label">Designation / Role</label>
                    <input
                      type="text"
                      className="modal-input"
                      placeholder="e.g. Senior Frontend Developer"
                      required
                      value={designationRole}
                      onChange={(e) => setDesignationRole(e.target.value)}
                    />
                  </div>
                </div>

                {/* Row 2: Min. Experience (Years) & Industry */}
                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">Min. Experience (Years)</label>
                    <div style={{ position: "relative" }}>
                      <input
                        type="text"
                        list="job-exp-datalist"
                        className="modal-input"
                        placeholder="Select or type Experience..."
                        value={minExperience}
                        onChange={(e) => setMinExperience(e.target.value)}
                      />
                      <datalist id="job-exp-datalist">
                        <option value="Freshers (0 yrs)" />
                        <option value="1-2 Years" />
                        <option value="3-5 Years" />
                        <option value="5-8 Years" />
                        <option value="8+ Years" />
                      </datalist>
                    </div>
                  </div>

                  <div>
                    <label className="modal-label">Industry</label>
                    <div style={{ position: "relative" }}>
                      <input
                        type="text"
                        list="job-industry-datalist"
                        className="modal-input"
                        placeholder="Select or type Industry..."
                        value={industry}
                        onChange={(e) => setIndustry(e.target.value)}
                      />
                      <datalist id="job-industry-datalist">
                        <option value="IT Services & Consulting" />
                        <option value="Software & Technology" />
                        <option value="Logistics & Transport" />
                        <option value="Healthcare" />
                        <option value="Finance & Banking" />
                        <option value="Manufacturing" />
                      </datalist>
                    </div>
                  </div>
                </div>

                {/* Row 3: Required Skills & Expertise */}
                <div>
                  <label className="modal-label">Required Skills & Expertise</label>
                  <input
                    type="text"
                    className="modal-input"
                    placeholder="e.g. React, Node, AI, Typescript..."
                    value={requiredSkills}
                    onChange={(e) => setRequiredSkills(e.target.value)}
                  />
                </div>

                {/* Row 4: Salary Range / Budget */}
                <div>
                  <label className="modal-label">Salary Range / Budget</label>
                  <input
                    type="text"
                    className="modal-input"
                    placeholder="e.g. ₹80,000 - ₹120,000"
                    value={salaryRange}
                    onChange={(e) => setSalaryRange(e.target.value)}
                  />
                </div>

                {/* Row 5: Internal Remarks / Notes */}
                <div>
                  <label className="modal-label">Internal Remarks / Notes</label>
                  <textarea
                    className="modal-textarea"
                    placeholder="Add any internal notes here..."
                    value={internalNotes}
                    onChange={(e) => setInternalNotes(e.target.value)}
                  />
                </div>
              </div>

              {/* Modal Footer */}
              <div className="modal-footer" style={{ padding: "1.25rem 1.75rem", borderTop: "1px solid #f1f5f9" }}>
                <button
                  type="button"
                  className="modal-cancel-btn"
                  onClick={() => setIsCreateModalOpen(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="modal-submit-btn">
                  Create Order
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
