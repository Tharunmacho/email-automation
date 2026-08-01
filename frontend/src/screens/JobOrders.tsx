"use client";

import React, { useState, useEffect, useMemo } from "react";
import {
  Plus,
  Search,
  Building2,
  Users,
  Calendar,
  X,
  Trash2,
  MoreVertical,
  Briefcase,
  ArrowLeft,
  Pencil,
  CheckCircle,
  Check,
  Target,
  Info,
  AlertTriangle,
  XCircle,
  RotateCcw,
  UserCheck,
  Mail,
  SlidersHorizontal,
  ArrowUpDown,
  Clock,
  Loader2,
  Phone,
  MapPin,
  Inbox,
} from "lucide-react";

import MetricTile from "@/components/ui/MetricTile";
import { formatDateFull, formatInt, initialsOf } from "@/lib/format";
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
  rejectedCandidateIds?: string[];
}

const DEFAULT_JOB_ORDERS: JobOrderRecord[] = [];

export type JobOrderStatus = JobOrderRecord["status"];

/** Status palette. Also feeds the card's top cap, so `color` doubles as the
 *  cap fill — each value has to read clearly as a 3px bar, not just as text. */
const STATUS_STYLES: Record<JobOrderStatus, { bg: string; color: string; border: string }> = {
  OPEN: { bg: "#fef3c7", color: "#b45309", border: "#fde68a" },
  "IN PROGRESS": { bg: "#e6edfb", color: "var(--primary-hover)", border: "#c7d7f5" },
  FILLED: { bg: "#dcfce7", color: "#15803d", border: "#bbf7d0" },
  CLOSED: { bg: "#f6f9fd", color: "#64748b", border: "var(--border-blue)" },
};

function getStatusStyle(status: string) {
  return STATUS_STYLES[status as JobOrderStatus] || STATUS_STYLES.CLOSED;
}

/** Live status derived from fulfillment — CLOSED is always honoured as manual. */
export function deriveStatus(order: JobOrderRecord): JobOrderStatus {
  if (order.status === "CLOSED") return "CLOSED";
  const filled = (order.shortlistedCandidateIds || []).length || order.fulfilledCount || 0;
  const required = order.headcount || 1;
  if (filled >= required) return "FILLED";
  if (filled > 0) return "IN PROGRESS";
  return "OPEN";
}

/** Parses both the legacy M/D/YYYY records and ISO yyyy-mm-dd values as *local* dates. */
function parseDueDate(dueDate?: string): Date | null {
  if (!dueDate) return null;

  const iso = dueDate.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));

  const slash = dueDate.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (slash) return new Date(Number(slash[3]), Number(slash[1]) - 1, Number(slash[2]));

  const fallback = new Date(dueDate);
  return Number.isNaN(fallback.getTime()) ? null : fallback;
}

/** Value for an `<input type="date">` (yyyy-mm-dd), or "" when unparseable. */
function toDateInputValue(dueDate?: string): string {
  const parsed = parseDueDate(dueDate);
  if (!parsed) return "";
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${parsed.getFullYear()}-${month}-${day}`;
}

export interface DueMeta {
  label: string;
  color: string;
  bg: string;
  border: string;
  overdue: boolean;
  days: number | null;
}

/** Due-date urgency chip metadata — accepts both M/D/YYYY and ISO strings. */
function getDueMeta(dueDate?: string): DueMeta {
  const neutral = { color: "#475569", bg: "#f6f9fd", border: "var(--border-blue-faint)", overdue: false, days: null };
  if (!dueDate) return { label: "No due date", ...neutral };

  const parsed = parseDueDate(dueDate);
  if (!parsed) return { label: dueDate, ...neutral };

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  parsed.setHours(0, 0, 0, 0);
  const days = Math.round((parsed.getTime() - today.getTime()) / 86400000);

  const urgent = { color: "#be123c", bg: "#fff1f2", border: "#fecdd3" };
  if (days < 0) return { label: `Overdue by ${Math.abs(days)} day${Math.abs(days) === 1 ? "" : "s"}`, ...urgent, overdue: true, days };
  if (days === 0) return { label: "Due today", ...urgent, overdue: true, days };
  if (days <= 7) return { label: `${days} day${days === 1 ? "" : "s"} left`, color: "#b45309", bg: "#fffbeb", border: "#fde68a", overdue: false, days };
  return { label: `${days} days left`, color: "#15803d", bg: "#f0fdf4", border: "#bbf7d0", overdue: false, days };
}

function formatDueDate(dueDate?: string): string {
  const parsed = parseDueDate(dueDate);
  if (!parsed) return dueDate || "—";
  return formatDateFull(parsed);
}

/** Default due date for new orders: 30 days out, stored as ISO yyyy-mm-dd. */
function defaultDueDate(): string {
  const d = new Date();
  d.setDate(d.getDate() + 30);
  return toDateInputValue(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`);
}

/** A bare number reads as "100000"; group it the Indian way so it scans. */
function formatSalary(raw?: string): string {
  const value = (raw ?? "").trim();
  if (!value) return "—";
  if (!/^\d+$/.test(value)) return value;

  const last3 = value.slice(-3);
  const rest = value.slice(0, -3);
  return rest ? `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${last3}` : last3;
}

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
  missingSkills: string[];
  skillScore: number; // max 50
  expScore: number;   // max 25
  roleScore: number;  // max 25
  expMatched: boolean;
  expStatusText: string;
  roleMatched: boolean;
  roleStatusText: string;
  summary: string;
  isSelected: boolean;
  isRejected: boolean;
}

export function calculateCandidateMatch(
  order: JobOrderRecord,
  candidate: CandidateRecord,
  shortlistedIds: string[] = [],
  rejectedIds: string[] = []
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
  const missingSkills: string[] = [];

  reqSkills.forEach((skill) => {
    const sLower = skill.toLowerCase();
    const isMatched =
      candSkills.some((cs) => cs.includes(sLower) || sLower.includes(cs)) ||
      candSummary.includes(sLower) ||
      workExpText.includes(sLower);

    if (isMatched) {
      matchedSkills.push(skill);
    } else {
      missingSkills.push(skill);
    }
  });

  // 1. Skill Match Weight (50% max)
  const skillRatio = reqSkills.length > 0 ? matchedSkills.length / reqSkills.length : 1;
  const skillScore = Math.round(skillRatio * 50);

  // 2. Experience Match Weight (25% max)
  const reqExpYears = parseMinExperienceYears(order.minExperience);
  const candExpYears = profile.total_experience_years ?? (candRole.includes("intern") || candRole.includes("fresher") ? 0 : 1);

  let expScore = 25;
  let expMatched = true;
  let expStatusText = "Exp Requirement Met";

  if (reqExpYears > 0) {
    if (candExpYears >= reqExpYears) {
      expScore = 25;
      expMatched = true;
      expStatusText = `Met Min Exp (${candExpYears} yrs)`;
    } else {
      expScore = Math.round(Math.max(0, (candExpYears / reqExpYears) * 20));
      expMatched = false;
      expStatusText = `Exp Gap (${candExpYears}/${reqExpYears} yrs)`;
    }
  } else {
    // If job accepts Fresher/Any
    if (candExpYears > 12) {
      expScore = 15;
      expMatched = false;
      expStatusText = `Overqualified (${candExpYears} yrs exp)`;
    } else {
      expScore = 25;
      expMatched = true;
      expStatusText = "Fresher Suitable";
    }
  }

  // 3. Designation / Industry Weight (25% max)
  const targetRole = (order.designation || order.title || "").toLowerCase();
  const STOP_WORDS = new Set(["and", "for", "the", "in", "with", "or", "of", "a", "an", "at", "to", "senior", "junior", "lead", "intern", "developer", "engineer"]);

  let roleScore = 0;
  let roleMatched = false;
  let roleStatusText = "Role Difference";

  if (targetRole) {
    const roleTokens = targetRole
      .split(/[\s/\-,]+/)
      .map((t) => t.trim())
      .filter((t) => t.length > 2 && !STOP_WORDS.has(t));

    const candRoleTokens = candRole
      .split(/[\s/\-,]+/)
      .map((t) => t.trim())
      .filter((t) => t.length > 2);

    // Check exact token overlap in designation
    const tokenMatches = roleTokens.filter((tok) =>
      candRoleTokens.some((crt) => crt.includes(tok) || tok.includes(crt))
    );

    if (roleTokens.length > 0 && tokenMatches.length > 0) {
      const matchRatio = tokenMatches.length / roleTokens.length;
      roleScore = Math.round(matchRatio * 25);
      roleMatched = true;
      roleStatusText = "Role Match: Aligned";
    } else if (workExpText.includes(targetRole) || candSummary.includes(targetRole)) {
      roleScore = 15;
      roleMatched = true;
      roleStatusText = "Role Found in Summary";
    } else {
      roleScore = 5;
      roleMatched = false;
      roleStatusText = "Role Mismatch";
    }
  } else {
    roleScore = 20;
    roleMatched = true;
    roleStatusText = "Role General";
  }

  // Overall Score Calculation (Strict & Realistic)
  let finalScore = Math.round(skillScore + expScore + roleScore);

  // If candidate matched ZERO required skills, cap final score at 25% max!
  if (reqSkills.length > 0 && matchedSkills.length === 0) {
    finalScore = Math.min(finalScore, 25);
  }

  // Only assign 100% if ALL skills, experience, and role match cleanly
  if (matchedSkills.length === reqSkills.length && expMatched && roleMatched && finalScore >= 95) {
    finalScore = 100;
  } else {
    finalScore = Math.min(98, Math.max(10, finalScore));
  }

  // Screening summary
  let summary = "";
  if (finalScore >= 90) {
    summary = `Exceptional fit. Matched ${matchedSkills.length}/${reqSkills.length} required skills with an aligned designation and experience band.`;
  } else if (finalScore >= 65) {
    summary = `Strong potential. Matched ${matchedSkills.length}/${reqSkills.length} skills with minor experience or role variance.`;
  } else if (finalScore >= 40) {
    summary = `Partial match. Some overlapping experience (${matchedSkills.join(", ") || "General"}), but missing key required skills.`;
  } else {
    summary = `Low compatibility (${finalScore}%). Missing key skills (${missingSkills.join(", ") || "None"}) and the designation differs from the requirement.`;
  }

  return {
    candidate,
    matchScore: finalScore,
    matchedSkills,
    missingSkills,
    skillScore,
    expScore,
    roleScore,
    expMatched,
    expStatusText,
    roleMatched,
    roleStatusText,
    summary,
    isSelected: shortlistedIds.includes(candidate.id),
    isRejected: rejectedIds.includes(candidate.id),
  };
}

function FulfilmentRing({ filled, headcount }: { filled: number; headcount: number }) {
  const pct = Math.min(100, Math.round((filled / (headcount || 1)) * 100));
  const radius = 20;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (pct / 100) * circumference;
  const color = pct >= 100 ? "var(--success)" : "var(--primary)";

  return (
    <div className="jo-ring" title={`${filled} of ${headcount} filled`}>
      <svg width="52" height="52" viewBox="0 0 52 52" aria-hidden="true">
        <circle cx="26" cy="26" r={radius} fill="none" stroke="var(--dash-track)" strokeWidth="4.5" />
        <circle
          cx="26"
          cy="26"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="4.5"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform="rotate(-90 26 26)"
          style={{ transition: "stroke-dashoffset 0.6s cubic-bezier(0.16, 1, 0.3, 1)" }}
        />
      </svg>
      <span className="jo-ring-value">{pct}%</span>
    </div>
  );
}

function ScoreRadialGauge({ score }: { score: number }) {
  const radius = 22;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (score / 100) * circumference;

  const color =
    score >= 90 ? "#16a34a" : score >= 65 ? "var(--primary)" : score >= 35 ? "#d97706" : "#e11d48";

  return (
    <div style={{ position: "relative", width: "56px", height: "56px", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <svg width="56" height="56" viewBox="0 0 56 56">
        <circle cx="28" cy="28" r={radius} fill="none" stroke="#f6f9fd" strokeWidth="4.5" />
        <circle
          cx="28"
          cy="28"
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth="4.5"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform="rotate(-90 28 28)"
          style={{ transition: "stroke-dashoffset 0.6s ease" }}
        />
      </svg>
      <span style={{ position: "absolute", fontSize: "0.78rem", fontWeight: 700, color: "#0f172a" }}>
        {score}%
      </span>
    </div>
  );
}

interface JobOrdersProps {
  candidates?: CandidateRecord[];
}

export default function JobOrders({ candidates: initialCandidates = [] }: JobOrdersProps) {
  const [searchQuery, setSearchQuery] = useState("");
  const [orders, setOrders] = useState<JobOrderRecord[]>(DEFAULT_JOB_ORDERS);
  const [statusFilter, setStatusFilter] = useState<"ALL" | "OVERDUE" | JobOrderStatus>("ALL");
  const [ordersLoading, setOrdersLoading] = useState(true);
  const [candidatesLoading, setCandidatesLoading] = useState(initialCandidates.length === 0);

  const filteredOrders = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return orders.filter((ord) => {
      const ordStatus = deriveStatus(ord);
      if (statusFilter === "OVERDUE") {
        if (ordStatus === "CLOSED" || !getDueMeta(ord.dueDate).overdue) return false;
      } else if (statusFilter !== "ALL" && ordStatus !== statusFilter) {
        return false;
      }
      if (!q) return true;
      return (
        ord.title.toLowerCase().includes(q) ||
        ord.client.toLowerCase().includes(q) ||
        ord.id.toLowerCase().includes(q) ||
        (ord.skills || []).some((s) => s.toLowerCase().includes(q))
      );
    });
  }, [orders, searchQuery, statusFilter]);
  const [selectedOrder, setSelectedOrder] = useState<JobOrderRecord | null>(null);
  const [dbCandidates, setDbCandidates] = useState<CandidateRecord[]>(initialCandidates);

  // Dynamic Client Options from Sourcing Hub Database API
  const [clientOptions, setClientOptions] = useState<string[]>([]);

  // Modals state
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState<JobOrderRecord | null>(null);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);

  // Candidate matching filter + sort state
  const [matchFilter, setMatchFilter] = useState<"ALL" | "TOP" | "SHORTLISTED" | "REJECTED" | "ROLE">("ALL");
  const [matchSort, setMatchSort] = useState<"SCORE" | "EXP" | "NAME">("SCORE");

  // Form state for "Create New Order"
  const [selectedClient, setSelectedClient] = useState("");
  const [designationRole, setDesignationRole] = useState("");
  const [minExperience, setMinExperience] = useState("");
  const [industry, setIndustry] = useState("");
  const [requiredSkills, setRequiredSkills] = useState("");
  const [salaryRange, setSalaryRange] = useState("");
  const [internalNotes, setInternalNotes] = useState("");
  const [newHeadcount, setNewHeadcount] = useState("1");
  const [newDueDate, setNewDueDate] = useState("");

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
      })
      .finally(() => setOrdersLoading(false));
  }, []);

  // Load backend database candidates if prop is empty
  useEffect(() => {
    if (initialCandidates.length > 0) {
      setDbCandidates(initialCandidates);
      setCandidatesLoading(false);
    } else {
      setCandidatesLoading(true);
      listCandidates()
        .then((res) => setDbCandidates(res.items))
        .catch(() => {})
        .finally(() => setCandidatesLoading(false));
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

  // Dismiss the card row menu on any outside click
  useEffect(() => {
    if (!activeMenuId) return;
    const closeMenu = () => setActiveMenuId(null);
    window.addEventListener("click", closeMenu);
    return () => window.removeEventListener("click", closeMenu);
  }, [activeMenuId]);

  // Open Edit Modal pre-filled with the chosen order's values.
  // `editingOrder` is tracked separately from `selectedOrder` so an order can be
  // edited straight from the list card without opening the detail view first.
  const openEditModal = (item: JobOrderRecord) => {
    setEditingOrder(item);
    setEditTitle(item.title);
    setEditHeadcount(String(item.headcount));
    setEditDueDate(toDateInputValue(item.dueDate));
    setEditIndustry(item.industry || "");
    setEditDesignation(item.designation || item.title);
    setEditMinExperience(item.minExperience || "");
    setEditSalary(item.salary);
    setEditSkills(item.skills.join(", "));
    setEditRemarks(item.description || "");
    setIsEditModalOpen(true);
  };

  const closeEditModal = () => {
    setIsEditModalOpen(false);
    setEditingOrder(null);
  };

  const handleUpdateOrder = (e: React.FormEvent) => {
    e.preventDefault();
    const target = editingOrder || selectedOrder;
    if (!target) return;

    const parsedSkills = editSkills
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

    const draft: JobOrderRecord = {
      ...target,
      title: editTitle,
      headcount: Math.max(1, parseInt(editHeadcount, 10) || 1),
      dueDate: editDueDate || defaultDueDate(),
      industry: editIndustry || "General",
      designation: editDesignation || editTitle,
      minExperience: editMinExperience || "Any",
      salary: editSalary || "Not disclosed",
      skills: parsedSkills.length > 0 ? parsedSkills : ["General"],
      description: editRemarks,
    };
    const updated: JobOrderRecord = { ...draft, status: deriveStatus(draft) };

    updateJobOrderAPI(updated.id, updated).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === updated.id ? updated : ord));
    saveOrdersToStorage(newOrders);
    if (selectedOrder?.id === updated.id) {
      setSelectedOrder(updated);
    }
    closeEditModal();
  };

  /** Quick "extend deadline" action — counts from today when the order is already overdue. */
  const handleExtendDueDate = (item: JobOrderRecord, days: number) => {
    const parsed = parseDueDate(item.dueDate);
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    const base = parsed && parsed.getTime() > today.getTime() ? parsed : today;
    base.setDate(base.getDate() + days);

    // Build the ISO value from local parts — toISOString() would shift the day by the UTC offset
    const nextDue = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`;
    const updated: JobOrderRecord = { ...item, dueDate: nextDue };

    updateJobOrderAPI(updated.id, updated).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === updated.id ? updated : ord));
    saveOrdersToStorage(newOrders);
    if (selectedOrder?.id === updated.id) {
      setSelectedOrder(updated);
    }
  };

  const handleToggleCloseOrder = (item: JobOrderRecord) => {
    const newStatus: JobOrderStatus =
      item.status === "CLOSED" ? deriveStatus({ ...item, status: "OPEN" }) : "CLOSED";
    const updated: JobOrderRecord = { ...item, status: newStatus };
    updateJobOrderAPI(updated.id, updated).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === updated.id ? updated : ord));
    saveOrdersToStorage(newOrders);
    if (selectedOrder?.id === updated.id) {
      setSelectedOrder(updated);
    }
  };

  const handleToggleShortlistCandidate = (orderId: string, candidateId: string) => {
    const currentOrder = orders.find((o) => o.id === orderId);
    if (!currentOrder) return;

    const currentShortlisted = currentOrder.shortlistedCandidateIds || [];
    const isCurrentlySelected = currentShortlisted.includes(candidateId);

    const newShortlisted = isCurrentlySelected
      ? currentShortlisted.filter((id) => id !== candidateId)
      : [...currentShortlisted, candidateId];

    const draft: JobOrderRecord = {
      ...currentOrder,
      shortlistedCandidateIds: newShortlisted,
      fulfilledCount: newShortlisted.length,
      // Shortlisting clears any earlier rejection — the two states are exclusive
      rejectedCandidateIds: (currentOrder.rejectedCandidateIds || []).filter((id) => id !== candidateId),
    };
    // Keep the status in step with fulfilment (OPEN → IN PROGRESS → FILLED)
    const updatedOrder: JobOrderRecord = { ...draft, status: deriveStatus(draft) };

    updateJobOrderAPI(orderId, updatedOrder).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === orderId ? updatedOrder : ord));
    saveOrdersToStorage(newOrders);

    if (selectedOrder?.id === orderId) {
      setSelectedOrder(updatedOrder);
    }
  };

  const handleToggleRejectCandidate = (orderId: string, candidateId: string) => {
    const currentOrder = orders.find((o) => o.id === orderId);
    if (!currentOrder) return;

    const currentRejected = currentOrder.rejectedCandidateIds || [];
    const isCurrentlyRejected = currentRejected.includes(candidateId);

    const newRejected = isCurrentlyRejected
      ? currentRejected.filter((id) => id !== candidateId)
      : [...currentRejected, candidateId];

    // Rejecting also pulls the candidate out of the shortlist
    const newShortlisted = isCurrentlyRejected
      ? currentOrder.shortlistedCandidateIds || []
      : (currentOrder.shortlistedCandidateIds || []).filter((id) => id !== candidateId);

    const draft: JobOrderRecord = {
      ...currentOrder,
      rejectedCandidateIds: newRejected,
      shortlistedCandidateIds: newShortlisted,
      fulfilledCount: newShortlisted.length,
    };
    const updatedOrder: JobOrderRecord = { ...draft, status: deriveStatus(draft) };

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
      client: selectedClient || clientOptions[0] || "Unassigned Client",
      headcount: Math.max(1, parseInt(newHeadcount, 10) || 1),
      fulfilledCount: 0,
      salary: salaryRange || "Not disclosed",
      skills: parsedSkills.length > 0 ? parsedSkills : ["General"],
      description: internalNotes || "",
      dueDate: newDueDate || defaultDueDate(),
      status: "OPEN",
      minExperience: minExperience || "Any",
      industry: industry || "General",
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
    setNewHeadcount("1");
    setNewDueDate("");
  };

  const handleDeleteOrder = (id: string) => {
    const target = orders.find((o) => o.id === id);
    const label = target ? `"${target.title}" (${id})` : id;
    if (typeof window !== "undefined" && !window.confirm(`Delete job order ${label}? This cannot be undone.`)) {
      setActiveMenuId(null);
      return;
    }

    deleteJobOrderAPI(id).catch(() => {});
    const updated = orders.filter((o) => o.id !== id);
    saveOrdersToStorage(updated);
    if (selectedOrder?.id === id) {
      setSelectedOrder(null);
    }
    setActiveMenuId(null);
  };

  // Hero statistics bar calculations
  // Everything else on this screen reads live status via deriveStatus(); using
  // the stored `o.status` here made the tile disagree with the filter chips
  // whenever an order had been filled but not re-saved.
  const activeOrdersCount = orders.filter((o) => {
    const live = deriveStatus(o);
    return live === "OPEN" || live === "IN PROGRESS";
  }).length;
  const totalHeadcount = orders.reduce((sum, o) => sum + (o.headcount || 1), 0);
  const totalShortlisted = orders.reduce((sum, o) => sum + (o.shortlistedCandidateIds?.length || o.fulfilledCount || 0), 0);
  const openPositions = Math.max(0, totalHeadcount - totalShortlisted);
  const fillRate = totalHeadcount > 0 ? Math.round((totalShortlisted / totalHeadcount) * 100) : 0;
  const overdueCount = orders.filter(
    (o) => deriveStatus(o) !== "CLOSED" && getDueMeta(o.dueDate).overdue,
  ).length;

  /**
   * The matching engine already exists but only ever ran for the order you had
   * opened. Running it across the list tells you which requisitions actually
   * have talent waiting, without opening each one.
   */
  const matchSummaryByOrder = useMemo(() => {
    const summary = new Map<string, { strong: number; best: number }>();
    if (dbCandidates.length === 0) return summary;

    for (const order of orders) {
      const shortlisted = order.shortlistedCandidateIds || [];
      const rejected = order.rejectedCandidateIds || [];
      let strong = 0;
      let best = 0;

      for (const cand of dbCandidates) {
        const result = calculateCandidateMatch(order, cand, shortlisted, rejected);
        if (result.isRejected) continue;
        if (result.matchScore >= 65) strong += 1;
        if (result.matchScore > best) best = result.matchScore;
      }
      summary.set(order.id, { strong, best });
    }
    return summary;
  }, [orders, dbCandidates]);

  // Compute matched candidates from Database OCR for selectedOrder
  const matchedCandidateResults = useMemo(() => {
    if (!selectedOrder) return [];
    const shortlistedIds = selectedOrder.shortlistedCandidateIds || [];
    const rejectedIds = selectedOrder.rejectedCandidateIds || [];

    if (dbCandidates.length === 0) return [];

    let results = dbCandidates.map((cand) =>
      calculateCandidateMatch(selectedOrder, cand, shortlistedIds, rejectedIds)
    );

    // Apply Filter — rejected candidates stay hidden unless explicitly requested
    if (matchFilter === "REJECTED") {
      results = results.filter((r) => r.isRejected);
    } else {
      results = results.filter((r) => !r.isRejected);
      if (matchFilter === "TOP") {
        results = results.filter((r) => r.matchScore >= 65);
      } else if (matchFilter === "SHORTLISTED") {
        results = results.filter((r) => r.isSelected);
      } else if (matchFilter === "ROLE") {
        results = results.filter((r) => r.roleMatched);
      }
    }

    // Apply Sort
    if (matchSort === "SCORE") {
      results.sort((a, b) => b.matchScore - a.matchScore);
    } else if (matchSort === "EXP") {
      results.sort(
        (a, b) => (b.candidate.profile?.total_experience_years || 0) - (a.candidate.profile?.total_experience_years || 0)
      );
    } else if (matchSort === "NAME") {
      results.sort((a, b) => {
        const nameA = a.candidate.profile?.full_name || "";
        const nameB = b.candidate.profile?.full_name || "";
        return nameA.localeCompare(nameB);
      });
    }

    return results;
  }, [selectedOrder, dbCandidates, matchFilter, matchSort]);

  // -------------------------------------------------------------
  // EDIT MODAL — rendered by BOTH the list and the detail view so an
  // order can be updated from wherever the user happens to be.
  // -------------------------------------------------------------
  const bumpEditDueDate = (days: number) => {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    const current = parseDueDate(editDueDate);
    const base = current && current.getTime() > today.getTime() ? current : today;
    base.setDate(base.getDate() + days);
    setEditDueDate(
      `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`
    );
  };

  function renderEditModal() {
    if (!isEditModalOpen) return null;

    const editingDueMeta = getDueMeta(editDueDate);

    return (
      <div className="cm-overlay active" onClick={closeEditModal}>
        <div
          className="cm-dialog client-modal-dialog"
          style={{ maxWidth: "600px", borderRadius: "16px", padding: 0 }}
          onClick={(e) => e.stopPropagation()}
        >
          <div className="modal-header" style={{ padding: "1.5rem 1.75rem", borderBottom: "1px solid #f6f9fd" }}>
            <div>
              <h3 className="modal-title" style={{ fontSize: "1.4rem", fontWeight: 700 }}>
                Edit Job Order
              </h3>
              {editingOrder && (
                <p className="modal-subtitle" style={{ fontSize: "0.82rem", color: "#64748b", margin: "2px 0 0 0" }}>
                  {editingOrder.client} • {editingOrder.id}
                </p>
              )}
            </div>
            <button className="modal-close-btn" onClick={closeEditModal}>
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
                    type="date"
                    className="modal-input"
                    value={editDueDate}
                    onChange={(e) => setEditDueDate(e.target.value)}
                  />

                  {/* Quick deadline extension */}
                  <div style={{ display: "flex", alignItems: "center", gap: "5px", flexWrap: "wrap", marginTop: "6px" }}>
                    <span style={{ fontSize: "0.72rem", fontWeight: 700, color: "#94a3b8" }}>EXTEND:</span>
                    {[7, 15, 30].map((d) => (
                      <button
                        key={d}
                        type="button"
                        onClick={() => bumpEditDueDate(d)}
                        style={{
                          background: "#f6f9fd",
                          border: "1px solid var(--border-blue)",
                          color: "#475569",
                          fontSize: "0.72rem",
                          fontWeight: 600,
                          padding: "2px 9px",
                          borderRadius: "6px",
                          cursor: "pointer",
                        }}
                      >
                        +{d}d
                      </button>
                    ))}
                    <span style={{ fontSize: "0.72rem", fontWeight: 600, color: editingDueMeta.color }}>
                      {editingDueMeta.label}
                    </span>
                  </div>
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
                  placeholder="Comma separated — e.g. React, Node, TypeScript"
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

            <div className="modal-footer" style={{ padding: "1.25rem 1.75rem", borderTop: "1px solid #f6f9fd" }}>
              <button type="button" className="modal-cancel-btn" onClick={closeEditModal}>
                Cancel
              </button>
              <button type="submit" className="modal-submit-btn">
                Save Changes
              </button>
            </div>
          </form>
        </div>
      </div>
    );
  }

  // -------------------------------------------------------------
  // RENDER DETAILED VIEW IF AN ORDER IS SELECTED
  // -------------------------------------------------------------
  if (selectedOrder) {
    const shortlistedIds = selectedOrder.shortlistedCandidateIds || [];
    const rejectedIds = selectedOrder.rejectedCandidateIds || [];
    const fulfilled = shortlistedIds.length || selectedOrder.fulfilledCount || 0;
    const totalHead = selectedOrder.headcount || 1;
    const progressPct = Math.min(100, Math.round((fulfilled / totalHead) * 100));
    const detailStatus = deriveStatus(selectedOrder);
    const statusStyle = getStatusStyle(detailStatus);
    const dueMeta = getDueMeta(selectedOrder.dueDate);
    const progressColor = progressPct >= 100 ? "#16a34a" : progressPct > 0 ? "var(--primary)" : "var(--border-blue-strong)";
    const strongMatches = matchedCandidateResults.filter((r) => r.matchScore >= 65).length;

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
              border: "1px solid var(--border-blue)",
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

        {/* Overdue banner with one-click deadline extension */}
        {dueMeta.overdue && selectedOrder.status !== "CLOSED" && (
          <div
            style={{
              background: "#fff1f2",
              border: "1px solid #fecdd3",
              borderRadius: "14px",
              padding: "0.9rem 1.25rem",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "1rem",
              flexWrap: "wrap",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "0.7rem" }}>
              <AlertTriangle size={20} style={{ color: "#e11d48", flexShrink: 0 }} />
              <div>
                <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#9f1239" }}>
                  {dueMeta.days === 0 ? "This order is due today" : `Deadline passed — ${dueMeta.label.toLowerCase()}`}
                </div>
                <div style={{ fontSize: "0.8rem", color: "#be123c" }}>
                  Target close date was {formatDueDate(selectedOrder.dueDate)}. Extend the deadline or close the order.
                </div>
              </div>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", flexWrap: "wrap" }}>
              <span style={{ fontSize: "0.75rem", fontWeight: 700, color: "#9f1239" }}>EXTEND BY:</span>
              {[7, 15, 30].map((d) => (
                <button
                  key={d}
                  onClick={() => handleExtendDueDate(selectedOrder, d)}
                  style={{
                    background: "#ffffff",
                    border: "1px solid #fda4af",
                    color: "#be123c",
                    fontSize: "0.78rem",
                    fontWeight: 700,
                    padding: "5px 12px",
                    borderRadius: "8px",
                    cursor: "pointer",
                  }}
                >
                  +{d} days
                </button>
              ))}
              <button
                onClick={() => openEditModal(selectedOrder)}
                style={{
                  background: "#e11d48",
                  border: "none",
                  color: "#ffffff",
                  fontSize: "0.78rem",
                  fontWeight: 700,
                  padding: "5px 12px",
                  borderRadius: "8px",
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: "4px",
                }}
              >
                <Calendar size={13} />
                Pick date
              </button>
            </div>
          </div>
        )}

        {/* Top Details Card */}
        <div
          style={{
            background: "#ffffff",
            border: "1px solid var(--border-blue)",
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
                <span style={{ background: "#e6edfb", color: "var(--primary)", fontSize: "0.75rem", fontWeight: 700, padding: "3px 10px", borderRadius: "6px" }}>
                  {selectedOrder.id}
                </span>

                <span
                  style={{
                    background: statusStyle.bg,
                    color: statusStyle.color,
                    border: `1px solid ${statusStyle.border}`,
                    fontSize: "0.75rem",
                    fontWeight: 700,
                    padding: "3px 10px",
                    borderRadius: "6px",
                    textTransform: "uppercase",
                  }}
                >
                  {detailStatus}
                </span>

                <span
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "4px",
                    background: dueMeta.bg,
                    color: dueMeta.color,
                    border: `1px solid ${dueMeta.border}`,
                    fontSize: "0.75rem",
                    fontWeight: 700,
                    padding: "3px 10px",
                    borderRadius: "6px",
                  }}
                >
                  <Clock size={12} />
                  {dueMeta.label}
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
                    border: "1px solid var(--border-blue-strong)",
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
                <div style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", fontSize: "0.9rem", fontWeight: 600, color: "var(--primary)" }}>
                  <Building2 size={15} />
                  <span>{selectedOrder.client}</span>
                </div>
              </div>
            </div>

            {/* Fulfillment Progress Bar */}
            <div style={{ minWidth: "220px", display: "flex", flexDirection: "column", gap: "0.4rem", alignItems: "flex-end" }}>
              <div style={{ display: "flex", gap: "2rem", fontSize: "0.8rem", fontWeight: 700 }}>
                <span style={{ color: "#64748b" }}>Fulfillment Progress</span>
                <span style={{ color: progressColor === "var(--border-blue-strong)" ? "#94a3b8" : progressColor }}>
                  {fulfilled} / {totalHead}
                </span>
              </div>
              <div style={{ width: "100%", height: "8px", background: "#f6f9fd", borderRadius: "999px", overflow: "hidden" }}>
                <div style={{ width: `${progressPct}%`, height: "100%", background: progressColor, borderRadius: "999px", transition: "width 0.3s ease" }}></div>
              </div>
              <span style={{ fontSize: "0.72rem", fontWeight: 600, color: "#94a3b8" }}>
                {progressPct >= 100
                  ? "All positions covered by shortlist"
                  : `${totalHead - fulfilled} position${totalHead - fulfilled === 1 ? "" : "s"} still to fill`}
              </span>
            </div>
          </div>

          {/* Specifications Inner Box */}
          <div
            style={{
              background: "#ffffff",
              border: "1px solid #f6f9fd",
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
              <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", marginTop: "6px" }}>
                {(selectedOrder.skills || []).length === 0 ? (
                  <span style={{ fontSize: "0.9rem", fontWeight: 700, color: "#94a3b8" }}>—</span>
                ) : (
                  selectedOrder.skills.map((sk) => (
                    <span
                      key={`req-${sk}`}
                      style={{
                        background: "#f6f9fd",
                        color: "var(--primary-hover)",
                        border: "1px solid #bfdbfe",
                        fontSize: "0.72rem",
                        fontWeight: 600,
                        padding: "2px 8px",
                        borderRadius: "6px",
                      }}
                    >
                      {sk}
                    </span>
                  ))
                )}
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                INDUSTRY
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: "#0f172a", marginTop: "4px" }}>
                {selectedOrder.industry || "General"}
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
              <div style={{ fontSize: "0.9rem", fontWeight: 700, color: dueMeta.color, marginTop: "4px", display: "flex", alignItems: "center", gap: "4px" }}>
                <Calendar size={13} />
                <span>{formatDueDate(selectedOrder.dueDate)}</span>
              </div>
            </div>

            <div>
              <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                REMARKS
              </div>
              <div style={{ fontSize: "0.9rem", fontWeight: selectedOrder.description ? 700 : 500, color: selectedOrder.description ? "#0f172a" : "#94a3b8", marginTop: "4px" }}>
                {selectedOrder.description || "No internal notes added"}
              </div>
            </div>
          </div>
        </div>

        {/* Candidate Matching Section with Real-Time Database Matches */}
        <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem", marginTop: "0.5rem" }}>
          {/* Header & Match Counter */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "1rem" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", fontSize: "1.2rem", fontWeight: 700, color: "#0f172a" }}>
              <Users size={20} style={{ color: "var(--primary)" }} />
              <span>Candidate Matches</span>
            </div>
            <span style={{ background: "#ffffff", border: "1px solid var(--border-blue)", color: "#64748b", fontSize: "0.8rem", fontWeight: 600, padding: "5px 14px", borderRadius: "999px", boxShadow: "0 1px 2px rgba(0,0,0,0.02)" }}>
              {matchedCandidateResults.length} of {dbCandidates.length} profiles shown
            </span>
          </div>

          {/* Pool summary as tiles — same shell the dashboard and the orders
              list use, so a number reads the same way everywhere. */}
          <div className="metric-tiles">
            <MetricTile
              label="Top match"
              value={`${matchedCandidateResults[0]?.matchScore ?? 0}%`}
              icon={Target}
              accent="#047857"
              accentSoft="rgba(16, 185, 129, 0.10)"
              caption={matchedCandidateResults[0]?.candidate.profile?.full_name || "No candidates evaluated"}
            />
            <MetricTile
              label="Strong fits (65%+)"
              value={formatInt(strongMatches)}
              icon={UserCheck}
              caption={`out of ${formatInt(dbCandidates.length)} profile${dbCandidates.length === 1 ? "" : "s"} evaluated`}
            />
            <MetricTile
              label="Shortlisted"
              value={formatInt(shortlistedIds.length)}
              icon={CheckCircle}
              accent="#047857"
              accentSoft="rgba(16, 185, 129, 0.10)"
              caption={`${formatInt(totalHead)} seat${totalHead === 1 ? "" : "s"} on this order`}
            />
            <MetricTile
              label="Rejected"
              value={formatInt(rejectedIds.length)}
              icon={XCircle}
              accent={rejectedIds.length > 0 ? "#b91c1c" : "var(--text-muted)"}
              accentSoft={rejectedIds.length > 0 ? "rgba(239, 68, 68, 0.10)" : "var(--dash-track)"}
              caption="Screened out of this order"
            />
          </div>

          <div className="mc-targets">
            <span className="mc-targets-label">
              <Target size={13} /> Target skills
            </span>
            <div className="mc-targets-list">
              {selectedOrder.skills.map((sk) => (
                <span className="mc-target" key={`target-${sk}`}>
                  {sk}
                </span>
              ))}
            </div>
          </div>

          {/* Filter & Sort Controls Bar */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "1rem", background: "#ffffff", border: "1px solid var(--border-blue)", padding: "0.75rem 1.25rem", borderRadius: "14px" }}>
            {/* Filter Pills */}
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
              <span style={{ fontSize: "0.78rem", fontWeight: 700, color: "#64748b", display: "flex", alignItems: "center", gap: "4px", marginRight: "0.25rem" }}>
                <SlidersHorizontal size={14} /> FILTER:
              </span>

              {[
                { id: "ALL", label: "All Matches" },
                { id: "TOP", label: "Top Matches (65%+)" },
                { id: "SHORTLISTED", label: `Shortlisted (${shortlistedIds.length})` },
                { id: "REJECTED", label: `Rejected (${rejectedIds.length})` },
                { id: "ROLE", label: "Role Aligned" },
              ].map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setMatchFilter(tab.id as any)}
                  style={{
                    background: matchFilter === tab.id ? "var(--primary)" : "#f6f9fd",
                    color: matchFilter === tab.id ? "#ffffff" : "#475569",
                    border: matchFilter === tab.id ? "1px solid var(--primary)" : "1px solid var(--border-blue)",
                    fontSize: "0.78rem",
                    fontWeight: 600,
                    padding: "4px 12px",
                    borderRadius: "8px",
                    cursor: "pointer",
                    transition: "all 0.15s ease",
                  }}
                >
                  {tab.label}
                </button>
              ))}
            </div>

            {/* Sort Control */}
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <span style={{ fontSize: "0.78rem", fontWeight: 700, color: "#64748b", display: "flex", alignItems: "center", gap: "4px" }}>
                <ArrowUpDown size={14} /> SORT BY:
              </span>
              <select
                value={matchSort}
                onChange={(e) => setMatchSort(e.target.value as any)}
                style={{
                  background: "#f6f9fd",
                  border: "1px solid var(--border-blue-strong)",
                  color: "#0f172a",
                  fontSize: "0.78rem",
                  fontWeight: 600,
                  padding: "4px 10px",
                  borderRadius: "8px",
                  cursor: "pointer",
                  outline: "none",
                }}
              >
                <option value="SCORE">Match Score (High to Low)</option>
                <option value="EXP">Experience (High to Low)</option>
                <option value="NAME">Candidate Name (A-Z)</option>
              </select>
            </div>
          </div>

          {/* Candidate Match Cards */}
          <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem" }}>
            {candidatesLoading ? (
              <div
                style={{
                  background: "#ffffff",
                  border: "1px solid var(--border-blue)",
                  borderRadius: "20px",
                  padding: "3rem 1.5rem",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: "0.75rem",
                  color: "#64748b",
                }}
              >
                <Loader2 size={26} style={{ color: "var(--primary)", animation: "job-order-spin 1s linear infinite" }} />
                <span style={{ fontSize: "0.9rem", fontWeight: 600 }}>Scanning the talent database for matches…</span>
              </div>
            ) : matchedCandidateResults.length === 0 ? (
              <div
                style={{
                  background: "#ffffff",
                  border: "1px dashed var(--border-blue-strong)",
                  borderRadius: "20px",
                  padding: "3rem 1.5rem",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: "0.5rem",
                  textAlign: "center",
                }}
              >
                <div style={{ width: "48px", height: "48px", borderRadius: "14px", background: "#f6f9fd", color: "#94a3b8", display: "flex", alignItems: "center", justifyContent: "center" }}>
                  <Inbox size={24} />
                </div>
                <span style={{ fontSize: "0.95rem", fontWeight: 700, color: "#334155" }}>
                  {dbCandidates.length === 0
                    ? "No candidate profiles in the database yet"
                    : matchFilter === "REJECTED"
                    ? "No rejected candidates for this order"
                    : "No candidates match this filter"}
                </span>
                <span style={{ fontSize: "0.83rem", color: "#94a3b8", maxWidth: "420px" }}>
                  {dbCandidates.length === 0
                    ? "Run the inbox poller to ingest resumes — matches will appear here automatically."
                    : "Try switching back to “All Matches”, or widen the required skills on this order."}
                </span>
                {dbCandidates.length > 0 && matchFilter !== "ALL" && (
                  <button
                    onClick={() => setMatchFilter("ALL")}
                    style={{
                      marginTop: "0.5rem",
                      background: "var(--primary)",
                      color: "#ffffff",
                      border: "none",
                      fontSize: "0.82rem",
                      fontWeight: 600,
                      padding: "0.5rem 1.1rem",
                      borderRadius: "10px",
                      cursor: "pointer",
                    }}
                  >
                    Show all matches
                  </button>
                )}
              </div>
            ) : (
              matchedCandidateResults.map((res) => {
              const profile = res.candidate.profile || {};
              const name = profile.full_name || res.candidate.source_email?.from_name || "Extracted Candidate";
              const expYears = profile.total_experience_years ? `${profile.total_experience_years} Years exp` : "Fresher";

              // Match badge styling based on realistic math score
              const getBadgeStyle = (score: number) => {
                if (score >= 90) return { bg: "#dcfce7", color: "#15803d", border: "#bbf7d0", label: `${score}% PERFECT MATCH` };
                if (score >= 65) return { bg: "#e6edfb", color: "var(--primary-hover)", border: "#bfdbfe", label: `${score}% HIGH MATCH` };
                if (score >= 35) return { bg: "#fef3c7", color: "#b45309", border: "#fde68a", label: `${score}% PARTIAL MATCH` };
                return { bg: "#fee2e2", color: "#b91c1c", border: "#fca5a5", label: `${score}% LOW COMPATIBILITY` };
              };

              const badge = getBadgeStyle(res.matchScore);

              return (
                <article
                  key={res.candidate.id}
                  className={`mc-card ${res.isSelected ? "is-shortlisted" : ""} ${
                    res.isRejected ? "is-rejected" : ""
                  }`}
                >
                  {/* Cap keys the card to its match band before any text is read. */}
                  <span
                    className="mc-cap"
                    style={{
                      background: res.isRejected
                        ? "#e11d48"
                        : res.matchScore >= 90
                        ? "#16a34a"
                        : res.matchScore >= 65
                        ? "var(--primary)"
                        : res.matchScore >= 35
                        ? "#d97706"
                        : "#e11d48",
                    }}
                  />

                  <header className="mc-head">
                    <ScoreRadialGauge score={res.matchScore} />

                    <div className="mc-identity">
                      <div className="mc-name-row">
                        <h4 className="mc-name" title={name}>
                          {name}
                        </h4>
                        <span
                          className="mc-badge"
                          style={{
                            background: badge.bg,
                            color: badge.color,
                            border: `1px solid ${badge.border}`,
                          }}
                        >
                          {badge.label}
                        </span>
                        {res.isRejected && (
                          <span className="mc-badge mc-badge-rejected">
                            <XCircle size={12} />
                            Rejected
                          </span>
                        )}
                      </div>

                      <p className="mc-role">
                        {profile.current_designation || "Candidate"}
                        {profile.current_company ? ` @ ${profile.current_company}` : ""}
                        {" · "}
                        <strong>{expYears}</strong>
                      </p>

                      <div className="mc-contacts">
                        {(profile.email || res.candidate.source_email?.from_addr) && (
                          <span>
                            <Mail size={12} />
                            {profile.email || res.candidate.source_email?.from_addr}
                          </span>
                        )}
                        {profile.phone && (
                          <span>
                            <Phone size={12} />
                            {profile.phone}
                          </span>
                        )}
                        {profile.location && (
                          <span>
                            <MapPin size={12} />
                            {profile.location}
                          </span>
                        )}
                      </div>
                    </div>
                  </header>

                  {/* The three scoring pillars, each a labelled meter. */}
                  <div className="mc-meters">
                    <div className="mc-meter">
                      <div className="mc-meter-top">
                        <span className="mc-meter-label">Skill fit</span>
                        <span className="mc-meter-score">{res.skillScore}<i>/50</i></span>
                      </div>
                      <span className="mc-track">
                        <span
                          className="mc-track-fill"
                          style={{
                            width: `${(res.skillScore / 50) * 100}%`,
                            background: "var(--primary)",
                          }}
                        />
                      </span>
                      <span className="mc-meter-note">
                        {res.matchedSkills.length} of {selectedOrder.skills.length} skills matched
                      </span>
                    </div>

                    <div className="mc-meter">
                      <div className="mc-meter-top">
                        <span className="mc-meter-label">Role fit</span>
                        <span className="mc-meter-score">{res.roleScore}<i>/25</i></span>
                      </div>
                      <span className="mc-track">
                        <span
                          className="mc-track-fill"
                          style={{
                            width: `${(res.roleScore / 25) * 100}%`,
                            background: res.roleMatched ? "var(--success)" : "var(--warning)",
                          }}
                        />
                      </span>
                      <span className="mc-meter-note">{res.roleStatusText}</span>
                    </div>

                    <div className="mc-meter">
                      <div className="mc-meter-top">
                        <span className="mc-meter-label">Experience fit</span>
                        <span className="mc-meter-score">{res.expScore}<i>/25</i></span>
                      </div>
                      <span className="mc-track">
                        <span
                          className="mc-track-fill"
                          style={{
                            width: `${(res.expScore / 25) * 100}%`,
                            background: res.expMatched ? "var(--success)" : "var(--warning)",
                          }}
                        />
                      </span>
                      <span className="mc-meter-note">{res.expStatusText}</span>
                    </div>
                  </div>

                  {/* Matched vs missing, against the order's required skills. */}
                  <div className="mc-matrix">
                    {res.matchedSkills.map((sk) => (
                      <span className="mc-chip mc-chip-hit" key={`matched-${sk}`}>
                        <Check size={12} />
                        {sk}
                      </span>
                    ))}
                    {res.missingSkills.map((sk) => (
                      <span className="mc-chip mc-chip-miss" key={`missing-${sk}`}>
                        <X size={12} />
                        {sk}
                      </span>
                    ))}
                  </div>

                  <p className="mc-summary">
                    <Info size={14} />
                    <span>{res.summary}</span>
                  </p>

                  {/* Actions live in a footer bar rather than competing with the
                      candidate's name for the top-right corner. */}
                  <footer className="mc-foot">
                    <button
                      className={`mc-btn mc-btn-reject ${res.isRejected ? "is-on" : ""}`}
                      onClick={() => handleToggleRejectCandidate(selectedOrder.id, res.candidate.id)}
                    >
                      {res.isRejected ? <RotateCcw size={15} /> : <XCircle size={15} />}
                      <span>{res.isRejected ? "Restore" : "Reject"}</span>
                    </button>

                    <button
                      className={`mc-btn mc-btn-primary ${res.isSelected ? "is-on" : ""}`}
                      onClick={() => handleToggleShortlistCandidate(selectedOrder.id, res.candidate.id)}
                    >
                      {res.isSelected ? <CheckCircle size={15} /> : <Plus size={15} />}
                      <span>
                        {res.isSelected
                          ? "Shortlisted"
                          : res.isRejected
                          ? "Restore & shortlist"
                          : "Shortlist candidate"}
                      </span>
                    </button>
                  </footer>
                </article>
              );
              })
            )}
          </div>
        </div>

        {renderEditModal()}
      </div>
    );
  }

  // -------------------------------------------------------------
  // RENDER CARDS LIST VIEW (Default Screen)
  // -------------------------------------------------------------
  return (
    <div className="job-orders-wrapper" style={{ display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      {/* The status chips below already count orders by state, so these report
          what the chips cannot: seats, progress and what has slipped. */}
      <div className="metric-tiles">
        <MetricTile
          label="Positions to fill"
          value={formatInt(openPositions)}
          icon={Target}
          caption={`${formatInt(totalHeadcount)} seat${totalHeadcount === 1 ? "" : "s"} across ${formatInt(activeOrdersCount)} active order${activeOrdersCount === 1 ? "" : "s"}`}
        />
        <MetricTile
          label="Shortlisted candidates"
          value={formatInt(totalShortlisted)}
          icon={UserCheck}
          accent="#047857"
          accentSoft="rgba(16, 185, 129, 0.10)"
          caption={`${fillRate}% of all seats filled`}
        />
        <MetricTile
          label="Overdue orders"
          value={formatInt(overdueCount)}
          icon={AlertTriangle}
          accent={overdueCount > 0 ? "#b91c1c" : "var(--text-muted)"}
          accentSoft={overdueCount > 0 ? "rgba(239, 68, 68, 0.10)" : "var(--dash-track)"}
          caption={overdueCount > 0 ? "Past their due date" : "Nothing past due"}
        />
      </div>

      {/* Search + filters share one surface instead of floating as two loose
          rows — the same "controls live in a card" rule the dashboard follows. */}
      <section className="jo-toolbar">
        <div className="jo-toolbar-top">
          <div className="search-input-wrapper">
            <Search size={18} />
            <input
              type="text"
              className="search-input"
              placeholder="Search by job title or required skills..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
            {searchQuery && (
              <button
                className="sourcing-search-clear"
                onClick={() => setSearchQuery("")}
                aria-label="Clear search"
              >
                <X size={16} />
              </button>
            )}
          </div>

          <button className="btn-new-client" onClick={() => setIsCreateModalOpen(true)}>
            <Plus size={16} />
            <span>Create New Order</span>
          </button>
        </div>

        <div className="jo-filters">
          <span className="jo-filters-label">
            <SlidersHorizontal size={14} /> STATUS
          </span>
          {(["ALL", "OPEN", "IN PROGRESS", "FILLED", "CLOSED", "OVERDUE"] as const).map((st) => {
            const isActive = statusFilter === st;
            const count =
              st === "ALL"
                ? orders.length
                : st === "OVERDUE"
                ? orders.filter((o) => deriveStatus(o) !== "CLOSED" && getDueMeta(o.dueDate).overdue).length
                : orders.filter((o) => deriveStatus(o) === st).length;
            return (
              <button
                key={st}
                className={`job-order-status-chip ${isActive ? "active" : ""}`}
                onClick={() => setStatusFilter(st)}
                aria-pressed={isActive}
              >
                {st === "ALL" ? "All orders" : st === "OVERDUE" ? "Overdue" : st}
                <span className="jo-chip-count">{count}</span>
              </button>
            );
          })}
        </div>
      </section>

      {/* Cards Grid */}
      <div className="jo-grid">
        {ordersLoading ? (
          <div className="jo-state">
            <Loader2 size={26} className="jo-spinner" />
            <p>Loading job orders…</p>
          </div>
        ) : filteredOrders.length === 0 ? (
          <div className="jo-state jo-state-empty">
            <Briefcase size={40} strokeWidth={1.5} />
            <p>{orders.length === 0 ? "No job orders yet" : "No orders match the current view"}</p>
            <span>
              {orders.length === 0
                ? "Create your first requisition to start matching candidates from the talent database."
                : "Clear the search box or pick a different status filter."}
            </span>
            {orders.length === 0 ? (
              <button className="btn-new-client" onClick={() => setIsCreateModalOpen(true)}>
                <Plus size={16} />
                <span>Create New Order</span>
              </button>
            ) : (
              <button
                className="btn-new-client"
                onClick={() => {
                  setSearchQuery("");
                  setStatusFilter("ALL");
                }}
              >
                <RotateCcw size={15} />
                <span>Reset filters</span>
              </button>
            )}
          </div>
        ) : (
          filteredOrders.map((item) => {
            const fulfilled = (item.shortlistedCandidateIds || []).length || item.fulfilledCount || 0;
            const cardStatus = deriveStatus(item);
            const cardStatusStyle = getStatusStyle(cardStatus);
            const cardDue = getDueMeta(item.dueDate);
            const isLate = cardDue.overdue && cardStatus !== "CLOSED";
            const extraSkills = Math.max(0, (item.skills || []).length - 4);
            const match = matchSummaryByOrder.get(item.id);

            return (
              <article
                className={`job-order-card ${isLate ? "is-late" : ""}`}
                key={item.id}
                onClick={() => setSelectedOrder(item)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    setSelectedOrder(item);
                  }
                }}
              >
                <span
                  className="jo-cap"
                  style={{ background: isLate ? "#e11d48" : cardStatusStyle.color }}
                />

                {/* Monogram anchors the card the way the sourcing cards do;
                    the ring closes the row with progress at a glance. */}
                <header className="jo-head">
                  <span className="jo-monogram">{initialsOf(item.client)}</span>

                  <div className="jo-headings">
                    <h3 className="jo-title" title={item.title}>
                      {item.title}
                    </h3>
                    <div className="jo-head-meta">
                      <span className="jo-client" title={item.client}>
                        <Building2 size={12} />
                        {item.client}
                      </span>
                      <span className="jo-sep" aria-hidden="true" />
                      <span
                        className="jo-status"
                        style={{
                          background: cardStatusStyle.bg,
                          color: cardStatusStyle.color,
                        }}
                      >
                        {cardStatus}
                      </span>
                    </div>
                  </div>

                  <div className="jo-head-end">
                    <FulfilmentRing filled={fulfilled} headcount={item.headcount} />
                    <div className="jo-actions">
                      <button
                        className="jo-icon-btn"
                        onClick={(e) => {
                          e.stopPropagation();
                          openEditModal(item);
                        }}
                        title="Edit this job order"
                        aria-label={`Edit ${item.title}`}
                      >
                        <Pencil size={14} />
                      </button>

                      <div className="jo-menu-wrap">
                        <button
                          className="jo-icon-btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            setActiveMenuId((prev) => (prev === item.id ? null : item.id));
                          }}
                          aria-label={`Actions for ${item.title}`}
                        >
                          <MoreVertical size={15} />
                        </button>

                        {activeMenuId === item.id && (
                          <div className="sourcing-dropdown-menu">
                            <button
                              className="dropdown-item"
                              onClick={(e) => {
                                e.stopPropagation();
                                setActiveMenuId(null);
                                openEditModal(item);
                              }}
                            >
                              <Pencil size={14} />
                              <span>Edit</span>
                            </button>
                            <button
                              className="dropdown-item"
                              onClick={(e) => {
                                e.stopPropagation();
                                setActiveMenuId(null);
                                handleToggleCloseOrder(item);
                              }}
                            >
                              <CheckCircle size={14} />
                              <span>
                                {item.status === "CLOSED" ? "Reopen order" : "Close order"}
                              </span>
                            </button>
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
                </header>

                {/* Figures carried by type, not by boxes — value over label. */}
                <dl className="jo-meta">
                  <div className="jo-meta-item">
                    <dd className="jo-meta-value" title={item.salary}>
                      {formatSalary(item.salary)}
                    </dd>
                    <dt className="jo-meta-label">Salary</dt>
                  </div>
                  <div className="jo-meta-item">
                    <dd className="jo-meta-value">
                      {item.minExperience && item.minExperience !== "Any"
                        ? item.minExperience
                        : "Open"}
                    </dd>
                    <dt className="jo-meta-label">Experience</dt>
                  </div>
                  <div className="jo-meta-item">
                    <dd className="jo-meta-value">
                      {fulfilled} <i>of</i> {item.headcount}
                    </dd>
                    <dt className="jo-meta-label">Headcount</dt>
                  </div>
                </dl>

                {(item.skills || []).length > 0 && (
                  <div className="jo-skill-tags">
                    {(item.skills || []).slice(0, 4).map((sk) => (
                      <span className="jo-skill" key={`${item.id}-${sk}`}>
                        {sk}
                      </span>
                    ))}
                    {extraSkills > 0 && <span className="jo-skill-more">+{extraSkills} more</span>}
                  </div>
                )}

                {match && (
                  <p className={`jo-match ${match.strong > 0 ? "has-matches" : ""}`}>
                    <Target size={14} />
                    {match.strong > 0 ? (
                      <span>
                        <strong>{match.strong}</strong> strong match
                        {match.strong === 1 ? "" : "es"} · best <strong>{match.best}%</strong>
                      </span>
                    ) : (
                      <span>
                        No strong matches yet{match.best > 0 ? ` · best ${match.best}%` : ""}
                      </span>
                    )}
                  </p>
                )}

                {/* An alert is the one thing that still earns a filled box. */}
                {isLate && (
                  <div className="jo-late">
                    <span className="jo-late-head">
                      <AlertTriangle size={14} />
                      {cardDue.days === 0 ? "Due today" : cardDue.label}
                    </span>
                    <div className="jo-late-actions">
                      <span className="jo-late-label">Extend</span>
                      {[7, 15, 30].map((d) => (
                        <button
                          key={d}
                          className="jo-late-btn"
                          onClick={(e) => {
                            e.stopPropagation();
                            handleExtendDueDate(item, d);
                          }}
                        >
                          +{d}d
                        </button>
                      ))}
                      <button
                        className="jo-late-btn jo-late-btn-solid"
                        onClick={(e) => {
                          e.stopPropagation();
                          openEditModal(item);
                        }}
                      >
                        Pick date
                      </button>
                    </div>
                  </div>
                )}

                <footer className="jo-foot">
                  <span className="jo-due">
                    <Calendar size={13} />
                    {formatDueDate(item.dueDate)}
                  </span>
                  <span className="jo-due-chip" style={{ color: cardDue.color }}>
                    <Clock size={11} />
                    {cardDue.label}
                  </span>
                </footer>
              </article>
            );
          })
        )}
      </div>

      {renderEditModal()}

      {/* Create New Order Pop-up Modal */}
      {isCreateModalOpen && (
        <div className="cm-overlay active" onClick={() => setIsCreateModalOpen(false)}>
          <div
            className="cm-dialog client-modal-dialog"
            style={{ maxWidth: "620px", borderRadius: "16px", padding: 0 }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div className="modal-header" style={{ padding: "1.5rem 1.75rem", borderBottom: "1px solid #f6f9fd" }}>
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

                {/* Row 3: Headcount & Target Close Date */}
                <div className="modal-row-2">
                  <div>
                    <label className="modal-label">Headcount (Positions)</label>
                    <input
                      type="number"
                      min="1"
                      className="modal-input"
                      placeholder="1"
                      required
                      value={newHeadcount}
                      onChange={(e) => setNewHeadcount(e.target.value)}
                    />
                  </div>

                  <div>
                    <label className="modal-label">Target Close Date</label>
                    <input
                      type="date"
                      className="modal-input"
                      value={newDueDate}
                      onChange={(e) => setNewDueDate(e.target.value)}
                    />
                    <span style={{ fontSize: "0.72rem", color: "#94a3b8", fontWeight: 500 }}>
                      Leave blank to default to 30 days from today.
                    </span>
                  </div>
                </div>

                {/* Row 4: Required Skills & Expertise */}
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
              <div className="modal-footer" style={{ padding: "1.25rem 1.75rem", borderTop: "1px solid #f6f9fd" }}>
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
