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
  Copy,
  SlidersHorizontal,
  ArrowUpDown,
  Clock,
  Send,
  Loader2,
  Phone,
  MapPin,
  Inbox,
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
  rejectedCandidateIds?: string[];
}

const DEFAULT_JOB_ORDERS: JobOrderRecord[] = [];

export type JobOrderStatus = JobOrderRecord["status"];

const STATUS_STYLES: Record<JobOrderStatus, { bg: string; color: string; border: string }> = {
  OPEN: { bg: "#fef3c7", color: "#b45309", border: "#fde68a" },
  "IN PROGRESS": { bg: "#dbeafe", color: "#1d4ed8", border: "#bfdbfe" },
  FILLED: { bg: "#dcfce7", color: "#15803d", border: "#bbf7d0" },
  CLOSED: { bg: "#f1f5f9", color: "#64748b", border: "#e2e8f0" },
};

function getStatusStyle(status: string) {
  return STATUS_STYLES[status as JobOrderStatus] || STATUS_STYLES.CLOSED;
}

/** Live status derived from fulfillment — CLOSED is always honoured as manual. */
function deriveStatus(order: JobOrderRecord): JobOrderStatus {
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
  const neutral = { color: "#475569", bg: "#f8fafc", border: "#e2e8f0", overdue: false, days: null };
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
  return parsed.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric" });
}

/** Default due date for new orders: 30 days out, stored as ISO yyyy-mm-dd. */
function defaultDueDate(): string {
  const d = new Date();
  d.setDate(d.getDate() + 30);
  return toDateInputValue(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`);
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

/** Builds the recruiter outreach email shown in the pitch modal. */
export function buildPitchEmail(order: JobOrderRecord, res: MatchResult): { subject: string; body: string } {
  const profile = res.candidate.profile || {};
  const firstName = (profile.full_name || res.candidate.source_email?.from_name || "there").split(" ")[0];
  const role = order.designation || order.title;
  const strengths = res.matchedSkills.length > 0 ? res.matchedSkills.join(", ") : "your overall profile";

  const subject = `${role} opportunity at ${order.client}`;

  const body = [
    `Hi ${firstName},`,
    "",
    `I came across your profile while shortlisting for a ${role} position with ${order.client}, and your background lines up closely with what the team is looking for.`,
    "",
    "Why I think this is worth a conversation:",
    `• Strong overlap on ${strengths}.`,
    `• ${res.roleStatusText} for the ${role} track.`,
    `• ${res.expStatusText}.`,
    "",
    "Role snapshot:",
    `• Client: ${order.client}`,
    `• Position: ${role}${order.industry ? ` (${order.industry})` : ""}`,
    `• Experience expected: ${order.minExperience || "Open"}`,
    `• Budget: ₹ ${order.salary}`,
    `• Skills in focus: ${(order.skills || []).join(", ") || "General"}`,
    ...(res.missingSkills.length > 0
      ? ["", `A couple of areas I'd like to understand better: ${res.missingSkills.join(", ")}.`]
      : []),
    "",
    "Would you be open to a short call this week to walk through the details?",
    "",
    "Best regards,",
    "Talent Acquisition Team",
  ].join("\n");

  return { subject, body };
}

function ScoreRadialGauge({ score }: { score: number }) {
  const radius = 22;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (score / 100) * circumference;

  const color =
    score >= 90 ? "#16a34a" : score >= 65 ? "#2563eb" : score >= 35 ? "#d97706" : "#e11d48";

  return (
    <div style={{ position: "relative", width: "56px", height: "56px", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <svg width="56" height="56" viewBox="0 0 56 56">
        <circle cx="28" cy="28" r={radius} fill="none" stroke="#f1f5f9" strokeWidth="4.5" />
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

  // Candidate Matching Filter & Pitch state
  const [matchFilter, setMatchFilter] = useState<"ALL" | "TOP" | "SHORTLISTED" | "REJECTED" | "ROLE">("ALL");
  const [matchSort, setMatchSort] = useState<"SCORE" | "EXP" | "NAME">("SCORE");
  const [pitchResult, setPitchResult] = useState<MatchResult | null>(null);
  const [pitchCopied, setPitchCopied] = useState(false);

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

  // Reset the "Copied" confirmation whenever a different pitch is opened
  useEffect(() => {
    setPitchCopied(false);
  }, [pitchResult]);

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
  const totalOrdersCount = orders.length;
  const activeOrdersCount = orders.filter((o) => o.status === "OPEN" || o.status === "IN PROGRESS").length;
  const totalHeadcount = orders.reduce((sum, o) => sum + (o.headcount || 1), 0);
  const totalShortlisted = orders.reduce((sum, o) => sum + (o.shortlistedCandidateIds?.length || o.fulfilledCount || 0), 0);

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
          <div className="modal-header" style={{ padding: "1.5rem 1.75rem", borderBottom: "1px solid #f1f5f9" }}>
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
                          background: "#f8fafc",
                          border: "1px solid #e2e8f0",
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

            <div className="modal-footer" style={{ padding: "1.25rem 1.75rem", borderTop: "1px solid #f1f5f9" }}>
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
    const progressColor = progressPct >= 100 ? "#16a34a" : progressPct > 0 ? "#3b82f6" : "#cbd5e1";
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
                <span style={{ color: progressColor === "#cbd5e1" ? "#94a3b8" : progressColor }}>
                  {fulfilled} / {totalHead}
                </span>
              </div>
              <div style={{ width: "100%", height: "8px", background: "#f1f5f9", borderRadius: "999px", overflow: "hidden" }}>
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
              <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", marginTop: "6px" }}>
                {(selectedOrder.skills || []).length === 0 ? (
                  <span style={{ fontSize: "0.9rem", fontWeight: 700, color: "#94a3b8" }}>—</span>
                ) : (
                  selectedOrder.skills.map((sk) => (
                    <span
                      key={`req-${sk}`}
                      style={{
                        background: "#eff6ff",
                        color: "#1d4ed8",
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
              <Users size={20} style={{ color: "#2563eb" }} />
              <span>Candidate Matches</span>
            </div>
            <span style={{ background: "#ffffff", border: "1px solid #e2e8f0", color: "#64748b", fontSize: "0.8rem", fontWeight: 600, padding: "5px 14px", borderRadius: "999px", boxShadow: "0 1px 2px rgba(0,0,0,0.02)" }}>
              {matchedCandidateResults.length} of {dbCandidates.length} profiles shown
            </span>
          </div>

          {/* Talent Pool Summary Card */}
          <div
            style={{
              background: "#ffffff",
              border: "1px solid #e2e8f0",
              borderRadius: "16px",
              padding: "1.1rem 1.4rem",
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: "1.5rem",
              flexWrap: "wrap",
              boxShadow: "0 1px 3px rgba(0,0,0,0.03)",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
              <div style={{ width: "42px", height: "42px", borderRadius: "12px", background: "rgba(37, 99, 235, 0.08)", color: "#2563eb", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <Target size={22} />
              </div>
              <div>
                <h4 style={{ margin: 0, fontSize: "0.95rem", fontWeight: 700, color: "#0f172a" }}>
                  Talent Pool Summary
                </h4>
                <p style={{ margin: "2px 0 0 0", fontSize: "0.82rem", color: "#475569" }}>
                  Top Match: <strong style={{ color: "#16a34a" }}>{matchedCandidateResults[0]?.matchScore || 0}%</strong>
                  {" • "}Strong fits (&ge;65%): <strong>{strongMatches}</strong>
                  {" • "}Shortlisted: <strong>{shortlistedIds.length}</strong>
                  {" • "}Rejected: <strong>{rejectedIds.length}</strong>
                  {" • "}Pool evaluated: <strong>{dbCandidates.length} profiles</strong>
                </p>
              </div>
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.6rem" }}>
              <span style={{ background: "#ffffff", border: "1px solid #cbd5e1", color: "#334155", fontSize: "0.75rem", fontWeight: 700, padding: "5px 12px", borderRadius: "8px" }}>
                Target Skills: {selectedOrder.skills.join(", ")}
              </span>
            </div>
          </div>

          {/* Filter & Sort Controls Bar */}
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: "1rem", background: "#ffffff", border: "1px solid #e2e8f0", padding: "0.75rem 1.25rem", borderRadius: "14px" }}>
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
                    background: matchFilter === tab.id ? "#2563eb" : "#f8fafc",
                    color: matchFilter === tab.id ? "#ffffff" : "#475569",
                    border: matchFilter === tab.id ? "1px solid #2563eb" : "1px solid #e2e8f0",
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
                  background: "#f8fafc",
                  border: "1px solid #cbd5e1",
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
                  border: "1px solid #e2e8f0",
                  borderRadius: "20px",
                  padding: "3rem 1.5rem",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: "0.75rem",
                  color: "#64748b",
                }}
              >
                <Loader2 size={26} style={{ color: "#2563eb", animation: "job-order-spin 1s linear infinite" }} />
                <span style={{ fontSize: "0.9rem", fontWeight: 600 }}>Scanning the talent database for matches…</span>
              </div>
            ) : matchedCandidateResults.length === 0 ? (
              <div
                style={{
                  background: "#ffffff",
                  border: "1px dashed #cbd5e1",
                  borderRadius: "20px",
                  padding: "3rem 1.5rem",
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  gap: "0.5rem",
                  textAlign: "center",
                }}
              >
                <div style={{ width: "48px", height: "48px", borderRadius: "14px", background: "#f1f5f9", color: "#94a3b8", display: "flex", alignItems: "center", justifyContent: "center" }}>
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
                      background: "#2563eb",
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
                if (score >= 65) return { bg: "#dbeafe", color: "#1d4ed8", border: "#bfdbfe", label: `${score}% HIGH MATCH` };
                if (score >= 35) return { bg: "#fef3c7", color: "#b45309", border: "#fde68a", label: `${score}% PARTIAL MATCH` };
                return { bg: "#fee2e2", color: "#b91c1c", border: "#fca5a5", label: `${score}% LOW COMPATIBILITY` };
              };

              const badge = getBadgeStyle(res.matchScore);

              return (
                <div
                  key={res.candidate.id}
                  style={{
                    background: res.isRejected ? "#fefefe" : "#ffffff",
                    border: res.isRejected
                      ? "1px solid #fecdd3"
                      : res.isSelected
                      ? "2px solid #22c55e"
                      : "1px solid #e2e8f0",
                    borderRadius: "20px",
                    padding: "1.5rem 1.75rem",
                    display: "flex",
                    flexDirection: "column",
                    gap: "1.15rem",
                    opacity: res.isRejected ? 0.72 : 1,
                    boxShadow: res.isSelected ? "0 4px 16px rgba(34, 197, 94, 0.12)" : "0 2px 8px rgba(0,0,0,0.03)",
                    transition: "all 0.2s ease",
                  }}
                >
                  {/* Top Row: Radial Gauge, Candidate Meta & Action Buttons */}
                  <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: "1rem", flexWrap: "wrap" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "1.1rem" }}>
                      {/* SVG Circular Score Radial */}
                      <ScoreRadialGauge score={res.matchScore} />

                      <div>
                        <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap" }}>
                          <span style={{ fontSize: "1.1rem", fontWeight: 700, color: "#0f172a" }}>{name}</span>
                          <span
                            style={{
                              background: badge.bg,
                              color: badge.color,
                              border: `1px solid ${badge.border}`,
                              fontSize: "0.75rem",
                              fontWeight: 700,
                              padding: "3px 10px",
                              borderRadius: "999px",
                              letterSpacing: "0.2px",
                            }}
                          >
                            {badge.label}
                          </span>

                          {res.isRejected && (
                            <span
                              style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: "4px",
                                background: "#fee2e2",
                                color: "#b91c1c",
                                border: "1px solid #fca5a5",
                                fontSize: "0.72rem",
                                fontWeight: 700,
                                padding: "3px 9px",
                                borderRadius: "999px",
                                textTransform: "uppercase",
                                letterSpacing: "0.3px",
                              }}
                            >
                              <XCircle size={12} />
                              Rejected
                            </span>
                          )}
                        </div>
                        <div style={{ fontSize: "0.85rem", color: "#64748b", marginTop: "3px", fontWeight: 500 }}>
                          {profile.current_designation || "Candidate"}
                          {profile.current_company ? ` @ ${profile.current_company}` : ""} •{" "}
                          <span style={{ color: "#334155", fontWeight: 600 }}>{expYears}</span>
                        </div>

                        {/* Contact strip pulled from the parsed resume */}
                        <div style={{ display: "flex", flexWrap: "wrap", gap: "0.35rem 1rem", marginTop: "5px", fontSize: "0.76rem", color: "#94a3b8", fontWeight: 500 }}>
                          {(profile.email || res.candidate.source_email?.from_addr) && (
                            <span style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
                              <Mail size={12} />
                              {profile.email || res.candidate.source_email?.from_addr}
                            </span>
                          )}
                          {profile.phone && (
                            <span style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
                              <Phone size={12} />
                              {profile.phone}
                            </span>
                          )}
                          {profile.location && (
                            <span style={{ display: "inline-flex", alignItems: "center", gap: "4px" }}>
                              <MapPin size={12} />
                              {profile.location}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>

                    <div style={{ display: "flex", alignItems: "center", gap: "0.6rem", flexWrap: "wrap" }}>
                      {/* Recruiter Outreach Email */}
                      <button
                        onClick={() => setPitchResult(res)}
                        style={{
                          background: "#ffffff",
                          color: "#475569",
                          border: "1px solid #cbd5e1",
                          padding: "0.6rem 1.1rem",
                          borderRadius: "12px",
                          fontSize: "0.82rem",
                          fontWeight: 600,
                          cursor: "pointer",
                          display: "flex",
                          alignItems: "center",
                          gap: "0.4rem",
                          transition: "all 0.15s ease",
                        }}
                        title="Draft a recruiter outreach email for this candidate"
                      >
                        <Mail size={15} style={{ color: "#64748b" }} />
                        <span>Outreach Email</span>
                      </button>

                      {/* Reject Action */}
                      <button
                        onClick={() => handleToggleRejectCandidate(selectedOrder.id, res.candidate.id)}
                        style={{
                          background: res.isRejected ? "#fee2e2" : "#ffffff",
                          color: res.isRejected ? "#b91c1c" : "#be123c",
                          border: `1px solid ${res.isRejected ? "#fca5a5" : "#fecdd3"}`,
                          padding: "0.6rem 1.1rem",
                          borderRadius: "12px",
                          fontSize: "0.82rem",
                          fontWeight: 600,
                          cursor: "pointer",
                          display: "flex",
                          alignItems: "center",
                          gap: "0.4rem",
                          transition: "all 0.15s ease",
                        }}
                        title={res.isRejected ? "Move this candidate back into the active pool" : "Reject this candidate for this order"}
                      >
                        {res.isRejected ? (
                          <>
                            <RotateCcw size={15} />
                            <span>Undo Reject</span>
                          </>
                        ) : (
                          <>
                            <XCircle size={15} />
                            <span>Reject</span>
                          </>
                        )}
                      </button>

                      {/* Shortlist Action */}
                      <button
                        onClick={() => handleToggleShortlistCandidate(selectedOrder.id, res.candidate.id)}
                        style={{
                          background: res.isSelected ? "#22c55e" : "#2563eb",
                          color: "#ffffff",
                          border: "none",
                          padding: "0.6rem 1.35rem",
                          borderRadius: "12px",
                          fontSize: "0.85rem",
                          fontWeight: 600,
                          cursor: "pointer",
                          display: "flex",
                          alignItems: "center",
                          gap: "0.4rem",
                          boxShadow: res.isSelected ? "0 2px 8px rgba(34, 197, 94, 0.35)" : "0 2px 8px rgba(37, 99, 235, 0.3)",
                          transition: "all 0.15s ease",
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
                            <span>{res.isRejected ? "Restore & Shortlist" : "Shortlist Candidate"}</span>
                          </>
                        )}
                      </button>
                    </div>
                  </div>

                  {/* 3-Pillar Match Meters */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
                      gap: "0.75rem",
                      background: "#f8fafc",
                      border: "1px solid #f1f5f9",
                      borderRadius: "14px",
                      padding: "0.9rem 1.1rem",
                    }}
                  >
                    {/* Pillar 1: Skill Fit (50 pts) */}
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.75rem", fontWeight: 700, color: "#475569" }}>
                        <span>Skill Fit</span>
                        <span style={{ color: "#2563eb" }}>{res.skillScore} / 50 pts</span>
                      </div>
                      <div style={{ width: "100%", height: "6px", background: "#e2e8f0", borderRadius: "999px", overflow: "hidden" }}>
                        <div style={{ width: `${(res.skillScore / 50) * 100}%`, height: "100%", background: "#2563eb", borderRadius: "999px" }} />
                      </div>
                      <span style={{ fontSize: "0.7rem", color: "#64748b", fontWeight: 500 }}>
                        {res.matchedSkills.length} of {selectedOrder.skills.length} skills matched
                      </span>
                    </div>

                    {/* Pillar 2: Role Fit (25 pts) */}
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.75rem", fontWeight: 700, color: "#475569" }}>
                        <span>Role Fit</span>
                        <span style={{ color: res.roleMatched ? "#15803d" : "#b45309" }}>{res.roleScore} / 25 pts</span>
                      </div>
                      <div style={{ width: "100%", height: "6px", background: "#e2e8f0", borderRadius: "999px", overflow: "hidden" }}>
                        <div style={{ width: `${(res.roleScore / 25) * 100}%`, height: "100%", background: res.roleMatched ? "#16a34a" : "#f59e0b", borderRadius: "999px" }} />
                      </div>
                      <span style={{ fontSize: "0.7rem", color: "#64748b", fontWeight: 500 }}>
                        {res.roleStatusText}
                      </span>
                    </div>

                    {/* Pillar 3: Exp Fit (25 pts) */}
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.25rem" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.75rem", fontWeight: 700, color: "#475569" }}>
                        <span>Experience Fit</span>
                        <span style={{ color: res.expMatched ? "#15803d" : "#b45309" }}>{res.expScore} / 25 pts</span>
                      </div>
                      <div style={{ width: "100%", height: "6px", background: "#e2e8f0", borderRadius: "999px", overflow: "hidden" }}>
                        <div style={{ width: `${(res.expScore / 25) * 100}%`, height: "100%", background: res.expMatched ? "#16a34a" : "#f59e0b", borderRadius: "999px" }} />
                      </div>
                      <span style={{ fontSize: "0.7rem", color: "#64748b", fontWeight: 500 }}>
                        {res.expStatusText}
                      </span>
                    </div>
                  </div>

                  {/* Screening Summary Callout Box */}
                  <div
                    style={{
                      background: res.matchScore >= 65 ? "#f0fdf4" : res.matchScore >= 35 ? "#fefce8" : "#fff1f2",
                      border: `1px solid ${res.matchScore >= 65 ? "#bbf7d0" : res.matchScore >= 35 ? "#fef08a" : "#fecdd3"}`,
                      borderRadius: "12px",
                      padding: "0.75rem 1rem",
                      display: "flex",
                      alignItems: "flex-start",
                      gap: "0.6rem",
                    }}
                  >
                    <Info size={16} style={{ color: res.matchScore >= 65 ? "#16a34a" : res.matchScore >= 35 ? "#d97706" : "#e11d48", marginTop: "2px", flexShrink: 0 }} />
                    <div style={{ fontSize: "0.82rem", color: "#334155", fontWeight: 500, lineHeight: 1.4 }}>
                      <strong style={{ color: "#0f172a" }}>Screening Summary: </strong>
                      {res.summary}
                    </div>
                  </div>

                  {/* Skills Grid: Matched Skills vs Missing Skills */}
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem" }}>
                    <div style={{ fontSize: "0.72rem", fontWeight: 700, color: "#94a3b8", textTransform: "uppercase", letterSpacing: "0.5px" }}>
                      SKILL MATRIX EVALUATION
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap" }}>
                      {/* Matched Skills (Green) */}
                      {res.matchedSkills.map((sk) => (
                        <span
                          key={`matched-${sk}`}
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "4px",
                            fontSize: "0.75rem",
                            fontWeight: 600,
                            padding: "3px 10px",
                            borderRadius: "6px",
                            background: "#f0fdf4",
                            color: "#15803d",
                            border: "1px solid #bbf7d0",
                          }}
                        >
                          <Check size={13} style={{ color: "#16a34a" }} />
                          <span>{sk}</span>
                        </span>
                      ))}

                      {/* Missing Skills (Rose/Red) */}
                      {res.missingSkills.map((sk) => (
                        <span
                          key={`missing-${sk}`}
                          style={{
                            display: "inline-flex",
                            alignItems: "center",
                            gap: "4px",
                            fontSize: "0.75rem",
                            fontWeight: 600,
                            padding: "3px 10px",
                            borderRadius: "6px",
                            background: "#fff1f2",
                            color: "#be123c",
                            border: "1px solid #fecdd3",
                          }}
                        >
                          <X size={13} style={{ color: "#e11d48" }} />
                          <span>{sk}</span>
                        </span>
                      ))}
                    </div>
                  </div>
                </div>
              );
              })
            )}
          </div>
        </div>

        {/* Recruiter Outreach Email Modal */}
        {pitchResult && (() => {
          const pitch = buildPitchEmail(selectedOrder, pitchResult);
          const pitchProfile = pitchResult.candidate.profile || {};
          const pitchName =
            pitchProfile.full_name || pitchResult.candidate.source_email?.from_name || "Candidate";
          const pitchEmail = pitchProfile.email || pitchResult.candidate.source_email?.from_addr || "";
          const mailto = `mailto:${pitchEmail}?subject=${encodeURIComponent(pitch.subject)}&body=${encodeURIComponent(pitch.body)}`;

          const copyPitch = async () => {
            const text = `Subject: ${pitch.subject}\n\n${pitch.body}`;
            try {
              await navigator.clipboard.writeText(text);
              setPitchCopied(true);
            } catch {
              setPitchCopied(false);
            }
          };

          return (
            <div className="cm-overlay active" onClick={() => setPitchResult(null)}>
              <div
                className="cm-dialog client-modal-dialog"
                style={{ maxWidth: "640px", borderRadius: "16px", padding: 0 }}
                onClick={(e) => e.stopPropagation()}
              >
                <div className="modal-header" style={{ padding: "1.4rem 1.75rem", borderBottom: "1px solid #f1f5f9" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
                    <div style={{ width: "38px", height: "38px", borderRadius: "11px", background: "rgba(37, 99, 235, 0.08)", color: "#2563eb", display: "flex", alignItems: "center", justifyContent: "center" }}>
                      <Mail size={19} />
                    </div>
                    <div>
                      <h3 className="modal-title" style={{ fontSize: "1.25rem", fontWeight: 700, margin: 0 }}>
                        Outreach Email
                      </h3>
                      <p className="modal-subtitle" style={{ fontSize: "0.82rem", color: "#64748b", margin: "2px 0 0 0" }}>
                        Drafted for {pitchName} • {pitchResult.matchScore}% match
                      </p>
                    </div>
                  </div>
                  <button className="modal-close-btn" onClick={() => setPitchResult(null)}>
                    <X size={20} />
                  </button>
                </div>

                <div className="modal-body" style={{ padding: "1.5rem 1.75rem", display: "flex", flexDirection: "column", gap: "1rem" }}>
                  {/* Candidate contact strip */}
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "0.5rem 1.25rem", fontSize: "0.8rem", color: "#475569", fontWeight: 500 }}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
                      <Mail size={13} style={{ color: "#94a3b8" }} />
                      {pitchEmail || "No email on file"}
                    </span>
                    {pitchProfile.phone && (
                      <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
                        <Phone size={13} style={{ color: "#94a3b8" }} />
                        {pitchProfile.phone}
                      </span>
                    )}
                    {pitchProfile.location && (
                      <span style={{ display: "inline-flex", alignItems: "center", gap: "5px" }}>
                        <MapPin size={13} style={{ color: "#94a3b8" }} />
                        {pitchProfile.location}
                      </span>
                    )}
                  </div>

                  <div>
                    <label className="modal-label">Subject</label>
                    <div
                      style={{
                        background: "#f8fafc",
                        border: "1px solid #e2e8f0",
                        borderRadius: "10px",
                        padding: "0.65rem 0.9rem",
                        fontSize: "0.87rem",
                        fontWeight: 600,
                        color: "#0f172a",
                      }}
                    >
                      {pitch.subject}
                    </div>
                  </div>

                  <div>
                    <label className="modal-label">Email body</label>
                    <pre
                      style={{
                        background: "#f8fafc",
                        border: "1px solid #e2e8f0",
                        borderRadius: "12px",
                        padding: "1rem 1.1rem",
                        fontSize: "0.83rem",
                        lineHeight: 1.6,
                        color: "#334155",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                        fontFamily: "inherit",
                        margin: 0,
                        maxHeight: "300px",
                        overflowY: "auto",
                      }}
                    >
                      {pitch.body}
                    </pre>
                  </div>
                </div>

                <div className="modal-footer" style={{ padding: "1.15rem 1.75rem", borderTop: "1px solid #f1f5f9", display: "flex", justifyContent: "flex-end", gap: "0.6rem", flexWrap: "wrap" }}>
                  <button
                    type="button"
                    onClick={copyPitch}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.4rem",
                      background: pitchCopied ? "#f0fdf4" : "#ffffff",
                      color: pitchCopied ? "#15803d" : "#475569",
                      border: `1px solid ${pitchCopied ? "#bbf7d0" : "#cbd5e1"}`,
                      fontSize: "0.85rem",
                      fontWeight: 600,
                      padding: "0.6rem 1.1rem",
                      borderRadius: "10px",
                      cursor: "pointer",
                    }}
                  >
                    {pitchCopied ? <Check size={15} /> : <Copy size={15} />}
                    <span>{pitchCopied ? "Copied" : "Copy email"}</span>
                  </button>

                  <a
                    href={pitchEmail ? mailto : undefined}
                    onClick={(e) => {
                      if (!pitchEmail) e.preventDefault();
                    }}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "0.4rem",
                      background: pitchEmail ? "#2563eb" : "#e2e8f0",
                      color: pitchEmail ? "#ffffff" : "#94a3b8",
                      fontSize: "0.85rem",
                      fontWeight: 600,
                      padding: "0.6rem 1.2rem",
                      borderRadius: "10px",
                      textDecoration: "none",
                      cursor: pitchEmail ? "pointer" : "not-allowed",
                      boxShadow: pitchEmail ? "0 2px 8px rgba(37,99,235,0.3)" : "none",
                    }}
                    title={pitchEmail ? "Open in your mail client" : "No email address on this profile"}
                  >
                    <Send size={15} />
                    <span>Open in mail client</span>
                  </a>
                </div>
              </div>
            </div>
          );
        })()}

        {renderEditModal()}
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
            <Target size={20} />
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

      {/* Status Filter Chips */}
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem", flexWrap: "wrap", marginTop: "-0.85rem" }}>
        <span style={{ fontSize: "0.78rem", fontWeight: 700, color: "#64748b", display: "flex", alignItems: "center", gap: "5px", marginRight: "0.25rem" }}>
          <SlidersHorizontal size={14} /> STATUS:
        </span>
        {(["ALL", "OPEN", "IN PROGRESS", "FILLED", "CLOSED", "OVERDUE"] as const).map((st) => {
          const isActive = statusFilter === st;
          const count =
            st === "ALL"
              ? orders.length
              : st === "OVERDUE"
              ? orders.filter((o) => deriveStatus(o) !== "CLOSED" && getDueMeta(o.dueDate).overdue).length
              : orders.filter((o) => deriveStatus(o) === st).length;
          const style =
            st === "ALL"
              ? { bg: "#f8fafc", color: "#475569", border: "#e2e8f0" }
              : st === "OVERDUE"
              ? { bg: "#fff1f2", color: "#be123c", border: "#fecdd3" }
              : getStatusStyle(st);
          return (
            <button
              key={st}
              className="job-order-status-chip"
              onClick={() => setStatusFilter(st)}
              style={{
                background: isActive ? "#0f172a" : style.bg,
                color: isActive ? "#ffffff" : style.color,
                border: `1px solid ${isActive ? "#0f172a" : style.border}`,
              }}
            >
              {st === "ALL" ? "All Orders" : st === "OVERDUE" ? "Overdue" : st} ({count})
            </button>
          );
        })}
      </div>

      {/* Cards Grid */}
      <div className="job-orders-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))", gap: "1.25rem" }}>
        {ordersLoading ? (
          <div style={{ gridColumn: "1 / -1", display: "flex", flexDirection: "column", alignItems: "center", gap: "0.75rem", padding: "3rem 1rem", background: "#ffffff", border: "1px solid #e2e8f0", borderRadius: "16px", color: "#64748b" }}>
            <Loader2 size={26} style={{ color: "#2563eb", animation: "job-order-spin 1s linear infinite" }} />
            <span style={{ fontSize: "0.9rem", fontWeight: 600 }}>Loading job orders…</span>
          </div>
        ) : filteredOrders.length === 0 ? (
          <div className="job-orders-empty" style={{ gridColumn: "1 / -1", textAlign: "center", padding: "3rem 1rem", background: "white", border: "1px dashed #cbd5e1", borderRadius: "16px", color: "#94a3b8" }}>
            <Briefcase size={40} style={{ marginBottom: "0.5rem" }} />
            <p style={{ fontWeight: 700, color: "#334155", margin: "0 0 0.25rem 0" }}>
              {orders.length === 0 ? "No job orders yet" : "No orders match the current view"}
            </p>
            <p style={{ margin: 0, fontSize: "0.85rem" }}>
              {orders.length === 0
                ? "Create your first requisition to start matching candidates from the talent database."
                : "Clear the search box or pick a different status filter."}
            </p>
            {orders.length === 0 ? (
              <button
                className="btn-new-client"
                style={{ marginTop: "1rem" }}
                onClick={() => setIsCreateModalOpen(true)}
              >
                <Plus size={16} />
                <span>Create New Order</span>
              </button>
            ) : (
              <button
                onClick={() => {
                  setSearchQuery("");
                  setStatusFilter("ALL");
                }}
                style={{ marginTop: "1rem", background: "#2563eb", color: "#ffffff", border: "none", fontSize: "0.85rem", fontWeight: 600, padding: "0.55rem 1.2rem", borderRadius: "10px", cursor: "pointer" }}
              >
                Reset filters
              </button>
            )}
          </div>
        ) : (
          filteredOrders.map((item) => {
            const fulfilled = (item.shortlistedCandidateIds || []).length || item.fulfilledCount || 0;
            const cardStatus = deriveStatus(item);
            const cardStatusStyle = getStatusStyle(cardStatus);
            const cardDue = getDueMeta(item.dueDate);
            const cardPct = Math.min(100, Math.round((fulfilled / (item.headcount || 1)) * 100));
            return (
              <div
                className="job-order-card"
                key={item.id}
                onClick={() => setSelectedOrder(item)}
                style={{
                  background: "#ffffff",
                  border: cardDue.overdue && cardStatus !== "CLOSED" ? "1px solid #fecdd3" : "1px solid #e2e8f0",
                  borderTop: cardDue.overdue && cardStatus !== "CLOSED" ? "3px solid #f43f5e" : "1px solid #e2e8f0",
                  borderRadius: "16px",
                  padding: "1.35rem",
                  display: "flex",
                  flexDirection: "column",
                  gap: "0.85rem",
                  height: "100%",
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
                        whiteSpace: "nowrap",
                        background: cardStatusStyle.bg,
                        color: cardStatusStyle.color,
                        border: `1px solid ${cardStatusStyle.border}`,
                      }}
                    >
                      {cardStatus}
                    </span>

                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        openEditModal(item);
                      }}
                      title="Edit this job order"
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        width: "28px",
                        height: "28px",
                        background: "#ffffff",
                        border: "1px solid #e2e8f0",
                        borderRadius: "8px",
                        color: "#64748b",
                        cursor: "pointer",
                      }}
                    >
                      <Pencil size={14} />
                    </button>

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
                            <span>{item.status === "CLOSED" ? "Reopen order" : "Close order"}</span>
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

                {/* Headcount & Salary Split Boxes */}
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem", marginTop: "0.25rem" }}>
                  <div style={{ background: "#ffffff", border: "1px solid #f1f5f9", borderRadius: "10px", padding: "0.65rem 0.85rem", boxShadow: "0 1px 2px rgba(0,0,0,0.02)" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem" }}>
                      <Users size={14} style={{ color: "#a855f7" }} />
                      <span style={{ fontSize: "0.65rem", fontWeight: 700, color: "#64748b", letterSpacing: "0.5px" }}>HEADCOUNT</span>
                    </div>
                    <div style={{ fontSize: "0.95rem", fontWeight: 700, color: "#0f172a", marginTop: "2px" }}>
                      {fulfilled} / {item.headcount} filled
                    </div>
                    <div style={{ width: "100%", height: "4px", background: "#f1f5f9", borderRadius: "999px", overflow: "hidden", marginTop: "6px" }}>
                      <div
                        style={{
                          width: `${cardPct}%`,
                          height: "100%",
                          background: cardPct >= 100 ? "#16a34a" : "#a855f7",
                          borderRadius: "999px",
                          transition: "width 0.3s ease",
                        }}
                      />
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
                    <div style={{ fontSize: "0.7rem", color: "#94a3b8", fontWeight: 600, marginTop: "6px" }}>
                      {item.minExperience && item.minExperience !== "Any" ? `${item.minExperience} exp` : "Experience: open"}
                    </div>
                  </div>
                </div>

                {/* Required Skills */}
                <div style={{ marginTop: "0.15rem" }}>
                  <div style={{ fontSize: "0.65rem", fontWeight: 700, color: "#94a3b8", letterSpacing: "0.5px", marginBottom: "0.35rem" }}>
                    REQUIRED SKILLS
                  </div>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: "4px" }}>
                    {(item.skills || []).slice(0, 4).map((sk) => (
                      <span
                        key={`${item.id}-${sk}`}
                        style={{
                          background: "#f8fafc",
                          color: "#475569",
                          border: "1px solid #e2e8f0",
                          fontSize: "0.72rem",
                          fontWeight: 600,
                          padding: "2px 8px",
                          borderRadius: "6px",
                        }}
                      >
                        {sk}
                      </span>
                    ))}
                    {(item.skills || []).length > 4 && (
                      <span style={{ fontSize: "0.72rem", fontWeight: 600, color: "#94a3b8", padding: "2px 4px" }}>
                        +{item.skills.length - 4} more
                      </span>
                    )}
                  </div>
                  {item.description && (
                    <div style={{ fontStyle: "italic", color: "#64748b", fontSize: "0.775rem", marginTop: "0.5rem", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {item.description}
                    </div>
                  )}
                </div>

                {/* Overdue strip — extend the deadline without leaving the list */}
                {cardDue.overdue && cardStatus !== "CLOSED" && (
                  <div
                    style={{
                      background: "#fff1f2",
                      border: "1px solid #fecdd3",
                      borderRadius: "10px",
                      padding: "0.6rem 0.75rem",
                      display: "flex",
                      flexDirection: "column",
                      gap: "0.5rem",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontSize: "0.78rem", fontWeight: 700, color: "#9f1239" }}>
                      <AlertTriangle size={14} style={{ flexShrink: 0 }} />
                      <span>{cardDue.days === 0 ? "Due today" : cardDue.label}</span>
                    </div>
                    <div style={{ display: "flex", alignItems: "center", gap: "0.35rem", flexWrap: "wrap" }}>
                      <span style={{ fontSize: "0.7rem", fontWeight: 700, color: "#be123c" }}>EXTEND:</span>
                      {[7, 15, 30].map((d) => (
                        <button
                          key={d}
                          onClick={(e) => {
                            e.stopPropagation();
                            handleExtendDueDate(item, d);
                          }}
                          style={{
                            background: "#ffffff",
                            border: "1px solid #fda4af",
                            color: "#be123c",
                            fontSize: "0.72rem",
                            fontWeight: 700,
                            padding: "2px 9px",
                            borderRadius: "6px",
                            cursor: "pointer",
                          }}
                        >
                          +{d}d
                        </button>
                      ))}
                      <button
                        onClick={(e) => {
                          e.stopPropagation();
                          openEditModal(item);
                        }}
                        style={{
                          background: "#e11d48",
                          border: "none",
                          color: "#ffffff",
                          fontSize: "0.72rem",
                          fontWeight: 700,
                          padding: "2px 9px",
                          borderRadius: "6px",
                          cursor: "pointer",
                        }}
                      >
                        Pick date
                      </button>
                    </div>
                  </div>
                )}

                {/* Card Footer: Due Date & Order ID */}
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: "0.5rem", paddingTop: "0.65rem", marginTop: "auto", borderTop: "1px solid #f1f5f9" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", flexWrap: "wrap" }}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem", color: "#475569", fontSize: "0.78rem", fontWeight: 600 }}>
                      <Calendar size={13} style={{ color: "#94a3b8" }} />
                      {formatDueDate(item.dueDate)}
                    </span>
                    <span
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: "3px",
                        background: cardDue.bg,
                        color: cardDue.color,
                        border: `1px solid ${cardDue.border}`,
                        fontSize: "0.7rem",
                        fontWeight: 700,
                        padding: "2px 7px",
                        borderRadius: "999px",
                      }}
                    >
                      <Clock size={11} />
                      {cardDue.label}
                    </span>
                  </div>
                  <span style={{ color: "#94a3b8", fontSize: "0.72rem", fontWeight: 600 }}>{item.id}</span>
                </div>
              </div>
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
