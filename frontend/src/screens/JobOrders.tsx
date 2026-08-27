"use client";

import React, { useCallback, useState, useEffect, useMemo } from "react";
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
  AlertTriangle,
  XCircle,
  RotateCcw,
  UserCheck,
  Mail,
  SlidersHorizontal,
  ArrowUpDown,
  ChevronDown,
  Clock,
  Loader2,
  Phone,
  MapPin,
  Inbox,
  Sparkles,
  Share2,
  Info,
} from "lucide-react";

import DatePicker from "@/components/ui/DatePicker";
import StatTile, { type StatTone } from "@/components/ui/StatTile";
import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { candidateNameOf, formatDateFull, formatInt, initialsOf } from "@/lib/format";
import {
  listCandidates,
  listJobOrdersAPI,
  createJobOrderAPI,
  updateJobOrderAPI,
  deleteJobOrderAPI,
  type CandidateRecord,
} from "@/lib/api";
import { CACHE_KEYS, readCache, writeCache } from "@/lib/localCache";

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

const STATUS_TONE: Record<JobOrderStatus, StatTone> = {
  OPEN: "blue",
  "IN PROGRESS": "blue",
  FILLED: "green",
  CLOSED: "slate",
};

function scoreTone(score: number): StatTone {
  if (score >= 90) return "green";
  if (score >= 65) return "blue";
  if (score >= 35) return "slate";
  return "red";
}

export function deriveStatus(order: JobOrderRecord): JobOrderStatus {
  if (order.status === "CLOSED") return "CLOSED";
  const filled = (order.shortlistedCandidateIds || []).length || order.fulfilledCount || 0;
  const required = order.headcount || 1;
  if (filled >= required) return "FILLED";
  if (filled > 0) return "IN PROGRESS";
  return "OPEN";
}

function parseDueDate(dueDate?: string): Date | null {
  if (!dueDate) return null;

  const iso = dueDate.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (iso) return new Date(Number(iso[1]), Number(iso[2]) - 1, Number(iso[3]));

  const slash = dueDate.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/);
  if (slash) return new Date(Number(slash[3]), Number(slash[1]) - 1, Number(slash[2]));

  const fallback = new Date(dueDate);
  return Number.isNaN(fallback.getTime()) ? null : fallback;
}

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

function getDueMeta(dueDate?: string): DueMeta {
  const neutral = { color: "var(--text-muted)", bg: "var(--tint-1)", border: "var(--border-blue-faint)", overdue: false, days: null };
  if (!dueDate) return { label: "No due date", ...neutral };

  const parsed = parseDueDate(dueDate);
  if (!parsed) return { label: dueDate, ...neutral };

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  parsed.setHours(0, 0, 0, 0);
  const days = Math.round((parsed.getTime() - today.getTime()) / 86400000);

  const urgent = { color: "var(--rose-ink)", bg: "var(--rose-fill)", border: "var(--rose-edge)" };
  if (days < 0) return { label: `Overdue by ${Math.abs(days)} day${Math.abs(days) === 1 ? "" : "s"}`, ...urgent, overdue: true, days };
  if (days === 0) return { label: "Due today", ...urgent, overdue: true, days };
  if (days <= 7) return { label: `${days} day${days === 1 ? "" : "s"} left`, color: "var(--warning-ink)", bg: "var(--warning-fill)", border: "var(--warning-edge)", overdue: false, days };
  return { label: `${days} days left`, color: "#15803d", bg: "var(--success-fill)", border: "var(--success-edge)", overdue: false, days };
}

function formatDueDate(dueDate?: string): string {
  const parsed = parseDueDate(dueDate);
  if (!parsed) return dueDate || "—";
  return formatDateFull(parsed);
}

function defaultDueDate(): string {
  const d = new Date();
  d.setDate(d.getDate() + 30);
  return toDateInputValue(`${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`);
}

function formatSalary(raw?: string): string {
  const value = (raw ?? "").trim();
  if (!value) return "—";
  if (!/^\d+$/.test(value)) return value;

  const last3 = value.slice(-3);
  const rest = value.slice(0, -3);
  return rest ? `${rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",")},${last3}` : last3;
}

function parseMinExperienceYears(expStr?: string): number {
  if (!expStr) return 0;
  const lower = expStr.toLowerCase();
  if (lower.includes("fresher") || lower.includes("any") || lower.includes("0")) return 0;
  const match = expStr.match(/\d+/);
  return match ? parseInt(match[0], 10) : 0;
}

export interface MatchResult {
  candidate: CandidateRecord;
  matchScore: number;
  matchedSkills: string[];
  missingSkills: string[];
  skillScore: number;
  expScore: number;
  roleScore: number;
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

  const skillRatio = reqSkills.length > 0 ? matchedSkills.length / reqSkills.length : 1;
  const skillScore = Math.round(skillRatio * 50);

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

  let finalScore = Math.round(skillScore + expScore + roleScore);

  if (reqSkills.length > 0 && matchedSkills.length === 0) {
    finalScore = Math.min(finalScore, 25);
  }

  if (matchedSkills.length === reqSkills.length && expMatched && roleMatched && finalScore >= 95) {
    finalScore = 100;
  } else {
    finalScore = Math.min(98, Math.max(10, finalScore));
  }

  let summary = "";
  if (finalScore >= 90) {
    summary = `Exceptional fit. Matched ${matchedSkills.length}/${reqSkills.length} required skills with an aligned designation and experience band.`;
  } else if (finalScore >= 65) {
    summary = `Strong potential. Matched ${matchedSkills.length}/${reqSkills.length} skills with minor experience or role variance.`;
  } else if (finalScore >= 40) {
    summary = `Partial match. Some overlapping experience (${matchedSkills.join(", ") || "General"}), but missing key required skills.`;
  } else {
    summary = `Low compatibility (${finalScore}%). Missing key skills (${missingSkills.join(", ") || "None"}) and designation differs from requirement.`;
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

  return (
    <div className="jo-ring" title={`${filled} of ${headcount} filled`}>
      <svg width="52" height="52" viewBox="0 0 52 52" aria-hidden="true">
        <circle cx="26" cy="26" r={radius} fill="none" stroke="var(--dash-track)" strokeWidth="4.5" />
        <circle
          cx="26"
          cy="26"
          r={radius}
          fill="none"
          stroke="var(--tone)"
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

function ScoreRadialGauge({ score, state }: { score: number; state: "open" | "on" | "off" }) {
  const radius = 22;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (score / 100) * circumference;
  const stroke = state === "on" ? "#047857" : state === "off" ? "var(--border-blue-strong)" : "var(--primary)";

  return (
    <div className="mc-gauge">
      <svg width="48" height="48" viewBox="0 0 56 56" aria-hidden="true">
        <circle cx="28" cy="28" r={radius} fill="none" stroke="var(--dash-track)" strokeWidth="4.5" />
        <circle
          cx="28"
          cy="28"
          r={radius}
          fill="none"
          stroke={stroke}
          strokeWidth="4.5"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          strokeLinecap="round"
          transform="rotate(-90 28 28)"
          style={{ transition: "stroke-dashoffset 0.6s ease" }}
        />
      </svg>
      <span className="mc-gauge-value">{score}%</span>
    </div>
  );
}



interface JobOrdersProps {
  candidates?: CandidateRecord[];
  onActivity?: (message: string, type?: LogEntry["type"]) => void;
}

export default function JobOrders({ candidates: initialCandidates = [], onActivity }: JobOrdersProps) {
  const activity = useCallback(
    (message: string, type: LogEntry["type"] = "info") => onActivity?.(message, type),
    [onActivity],
  );

  const [searchQuery, setSearchQuery] = useState("");
  const [orders, setOrders] = useState<JobOrderRecord[]>(DEFAULT_JOB_ORDERS);
  const [statusFilter, setStatusFilter] = useState<"ALL" | "OVERDUE" | JobOrderStatus>("ALL");
  const [ordersLoading, setOrdersLoading] = useState(true);
  const [fetchingCandidates, setFetchingCandidates] = useState(initialCandidates.length === 0);

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

  const [fetchedCandidates, setFetchedCandidates] = useState<CandidateRecord[]>([]);
  const dbCandidates = initialCandidates.length > 0 ? initialCandidates : fetchedCandidates;
  const candidatesLoading = initialCandidates.length === 0 && fetchingCandidates;

  const [clientOptions, setClientOptions] = useState<string[]>([]);

  // Modals state
  const [isCreateModalOpen, setIsCreateModalOpen] = useState(false);
  const [isEditModalOpen, setIsEditModalOpen] = useState(false);
  const [editingOrder, setEditingOrder] = useState<JobOrderRecord | null>(null);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);

  const [copiedSummary, setCopiedSummary] = useState(false);

  // Candidate matching filter + sort state
  const [matchFilter, setMatchFilter] = useState<"ALL" | "TOP" | "SHORTLISTED" | "REJECTED" | "ROLE">("ALL");
  const [matchSort, setMatchSort] = useState<"SCORE" | "EXP" | "NAME">("SCORE");
  const [showWeakMatches, setShowWeakMatches] = useState(false);

  const [skillFilter, setSkillFilter] = useState<string[]>([]);
  const [skillMode, setSkillMode] = useState<"ANY" | "ALL">("ANY");
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);

  // Form state
  const [selectedClient, setSelectedClient] = useState("");
  const [designationRole, setDesignationRole] = useState("");
  const [minExperience, setMinExperience] = useState("");
  const [industry, setIndustry] = useState("");
  const [requiredSkills, setRequiredSkills] = useState("");
  const [salaryRange, setSalaryRange] = useState("");
  const [internalNotes, setInternalNotes] = useState("");
  const [newHeadcount, setNewHeadcount] = useState("1");
  const [newDueDate, setNewDueDate] = useState("");

  const [editTitle, setEditTitle] = useState("");
  const [editHeadcount, setEditHeadcount] = useState("1");
  const [editDueDate, setEditDueDate] = useState("");
  const [editIndustry, setEditIndustry] = useState("");
  const [editDesignation, setEditDesignation] = useState("");
  const [editMinExperience, setEditMinExperience] = useState("");
  const [editSalary, setEditSalary] = useState("");
  const [editSkills, setEditSkills] = useState("");
  const [editRemarks, setEditRemarks] = useState("");

  useEffect(() => {
    let active = true;
    listJobOrdersAPI()
      .then((res) => {
        if (!active) return;
        const items = (res.items ?? []) as JobOrderRecord[];
        setOrders(items);
        writeCache(CACHE_KEYS.jobOrders, items);
      })
      .catch(() => {
        const cached = readCache<JobOrderRecord>(CACHE_KEYS.jobOrders);
        if (active && cached) setOrders(cached);
      })
      .finally(() => {
        if (active) setOrdersLoading(false);
      });

    return () => {
      active = false;
    };
  }, []);

  const hasParentCandidates = initialCandidates.length > 0;
  useEffect(() => {
    if (hasParentCandidates) return;

    let active = true;
    listCandidates()
      .then((res) => {
        if (active) setFetchedCandidates(res.items);
      })
      .catch(() => {})
      .finally(() => {
        if (active) setFetchingCandidates(false);
      });

    return () => {
      active = false;
    };
  }, [hasParentCandidates]);

  useEffect(() => {
    const namesOf = (records: { name?: string }[]) =>
      Array.from(new Set(records.map((r) => r.name).filter(Boolean) as string[]));

    let active = true;
    import("@/lib/api").then(({ listSourcingClientsAPI }) => {
      listSourcingClientsAPI()
        .then((res) => {
          if (active) setClientOptions(namesOf(res.items ?? []));
        })
        .catch(() => {
          const cached = readCache<{ name?: string }>(CACHE_KEYS.sourcingClients);
          if (active && cached) setClientOptions(namesOf(cached));
        });
    });

    return () => {
      active = false;
    };
  }, [isCreateModalOpen, isEditModalOpen]);

  const saveOrdersToStorage = (updated: JobOrderRecord[]) => {
    setOrders(updated);
    writeCache(CACHE_KEYS.jobOrders, updated);
  };

  useEffect(() => {
    const handleOpenModal = () => setIsCreateModalOpen(true);
    window.addEventListener("open-new-order-modal", handleOpenModal);
    return () => window.removeEventListener("open-new-order-modal", handleOpenModal);
  }, []);

  useEffect(() => {
    if (!activeMenuId) return;
    const closeMenu = () => setActiveMenuId(null);
    window.addEventListener("click", closeMenu);
    return () => window.removeEventListener("click", closeMenu);
  }, [activeMenuId]);

  useEffect(() => {
    if (!skillMenuOpen) return;
    const close = () => setSkillMenuOpen(false);
    const onEsc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSkillMenuOpen(false);
    };
    window.addEventListener("click", close);
    window.addEventListener("keydown", onEsc);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("keydown", onEsc);
    };
  }, [skillMenuOpen]);

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

    activity(`Updated job order: ${updated.title} (${updated.client}).`, "success");
    updateJobOrderAPI(updated.id, updated).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === updated.id ? updated : ord));
    saveOrdersToStorage(newOrders);
    if (selectedOrder?.id === updated.id) {
      setSelectedOrder(updated);
    }
    closeEditModal();
  };

  const handleExtendDueDate = (item: JobOrderRecord, days: number) => {
    const parsed = parseDueDate(item.dueDate);
    const today = new Date();
    today.setHours(0, 0, 0, 0);

    const base = parsed && parsed.getTime() > today.getTime() ? parsed : today;
    base.setDate(base.getDate() + days);

    const nextDue = `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`;
    const updated: JobOrderRecord = { ...item, dueDate: nextDue };

    activity(`Extended deadline on ${item.title} (${item.client}) by ${days} days.`, "info");
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
    activity(
      newStatus === "CLOSED"
        ? `Closed job order: ${item.title} (${item.client}).`
        : `Reopened job order: ${item.title} (${item.client}).`,
      newStatus === "CLOSED" ? "warn" : "success",
    );
    updateJobOrderAPI(updated.id, updated).catch(() => {});
    const newOrders = orders.map((ord) => (ord.id === updated.id ? updated : ord));
    saveOrdersToStorage(newOrders);
    if (selectedOrder?.id === updated.id) {
      setSelectedOrder(updated);
    }
  };

  const candidateLabel = (candidateId: string) =>
    candidateNameOf(dbCandidates.find((c) => c.id === candidateId));

  const handleToggleShortlistCandidate = (orderId: string, candidateId: string) => {
    const currentOrder = orders.find((o) => o.id === orderId);
    if (!currentOrder) return;

    const currentShortlisted = currentOrder.shortlistedCandidateIds || [];
    const isCurrentlySelected = currentShortlisted.includes(candidateId);

    const who = candidateLabel(candidateId);
    activity(
      isCurrentlySelected
        ? `Removed ${who} from shortlist for ${currentOrder.title} (${currentOrder.client}).`
        : `Shortlisted ${who} for ${currentOrder.title} (${currentOrder.client}).`,
      isCurrentlySelected ? "warn" : "success",
    );

    const newShortlisted = isCurrentlySelected
      ? currentShortlisted.filter((id) => id !== candidateId)
      : [...currentShortlisted, candidateId];

    const draft: JobOrderRecord = {
      ...currentOrder,
      shortlistedCandidateIds: newShortlisted,
      fulfilledCount: newShortlisted.length,
      rejectedCandidateIds: (currentOrder.rejectedCandidateIds || []).filter((id) => id !== candidateId),
    };
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

    const who = candidateLabel(candidateId);
    activity(
      isCurrentlyRejected
        ? `Restored ${who} to pool for ${currentOrder.title} (${currentOrder.client}).`
        : `Rejected ${who} for ${currentOrder.title} (${currentOrder.client}).`,
      isCurrentlyRejected ? "info" : "warn",
    );

    const newRejected = isCurrentlyRejected
      ? currentRejected.filter((id) => id !== candidateId)
      : [...currentRejected, candidateId];

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

    activity(`Created job order: ${newRecord.title} (${newRecord.client}) — ${newRecord.headcount} seat${newRecord.headcount === 1 ? "" : "s"}.`, "success");
    createJobOrderAPI(newRecord).catch(() => {});
    saveOrdersToStorage([newRecord, ...orders]);
    setIsCreateModalOpen(false);

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

    activity(`Deleted job order: ${target ? `${target.title} (${target.client})` : id}.`, "warn");
    deleteJobOrderAPI(id).catch(() => {});
    const updated = orders.filter((o) => o.id !== id);
    saveOrdersToStorage(updated);
    if (selectedOrder?.id === id) {
      setSelectedOrder(null);
    }
    setActiveMenuId(null);
  };


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

  const activeSkillFilter = useMemo(() => {
    const required = new Set(selectedOrder?.skills || []);
    return skillFilter.filter((s) => required.has(s));
  }, [skillFilter, selectedOrder]);

  const skillTriggerLabel = useMemo(() => {
    if (activeSkillFilter.length === 0) return "All skills";
    if (activeSkillFilter.length === 1) return activeSkillFilter[0];
    return `${activeSkillFilter.length} skills · ${skillMode === "ALL" ? "all" : "any"}`;
  }, [activeSkillFilter, skillMode]);

  const toggleSkillFilter = (skill: string) => {
    setSkillFilter((prev) =>
      prev.includes(skill) ? prev.filter((s) => s !== skill) : [...prev, skill]
    );
  };

  const skillCoverage = useMemo(() => {
    const counts = new Map<string, number>();
    if (!selectedOrder) return counts;

    const shortlistedIds = selectedOrder.shortlistedCandidateIds || [];
    const rejectedIds = selectedOrder.rejectedCandidateIds || [];

    for (const skill of selectedOrder.skills || []) counts.set(skill, 0);

    for (const cand of dbCandidates) {
      const res = calculateCandidateMatch(selectedOrder, cand, shortlistedIds, rejectedIds);
      if (res.isRejected) continue;
      for (const skill of res.matchedSkills) {
        counts.set(skill, (counts.get(skill) || 0) + 1);
      }
    }
    return counts;
  }, [selectedOrder, dbCandidates]);

  const matchedCandidateResults = useMemo(() => {
    if (!selectedOrder) return [];
    const shortlistedIds = selectedOrder.shortlistedCandidateIds || [];
    const rejectedIds = selectedOrder.rejectedCandidateIds || [];

    if (dbCandidates.length === 0) return [];

    let results = dbCandidates.map((cand) =>
      calculateCandidateMatch(selectedOrder, cand, shortlistedIds, rejectedIds)
    );

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

    if (activeSkillFilter.length > 0) {
      results = results.filter((r) => {
        const held = new Set(r.matchedSkills.map((s) => s.toLowerCase()));
        return skillMode === "ALL"
          ? activeSkillFilter.every((s) => held.has(s.toLowerCase()))
          : activeSkillFilter.some((s) => held.has(s.toLowerCase()));
      });
    }

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
  }, [selectedOrder, dbCandidates, matchFilter, matchSort, activeSkillFilter, skillMode]);

  const handleCopyShortlistSummary = () => {
    if (!selectedOrder) return;

    const shortlisted = matchedCandidateResults.filter((r) => r.isSelected);
    let summaryText = `📋 EXECUTIVE SHORTLIST SUMMARY: ${selectedOrder.title} (${selectedOrder.client})\n`;
    summaryText += `Requisition ID: ${selectedOrder.id} | Headcount: ${selectedOrder.headcount} | Budget: ₹${formatSalary(selectedOrder.salary)}\n`;
    summaryText += `Required Skills: ${selectedOrder.skills.join(", ")}\n`;
    summaryText += `--------------------------------------------------\n`;

    if (shortlisted.length === 0) {
      summaryText += `No candidates currently shortlisted for this requisition.\n`;
    } else {
      shortlisted.forEach((res, i) => {
        const name = res.candidate.profile?.full_name || "Candidate";
        const email = res.candidate.profile?.email || "N/A";
        const phone = res.candidate.profile?.phone || "N/A";
        const exp = res.candidate.profile?.total_experience_years || 0;
        summaryText += `${i + 1}. ${name} (${res.matchScore}% Match)\n`;
        summaryText += `   • Designation: ${res.candidate.profile?.current_designation || "N/A"}\n`;
        summaryText += `   • Experience: ${exp} yrs | Email: ${email} | Phone: ${phone}\n`;
        summaryText += `   • Matched Skills: ${res.matchedSkills.join(", ")}\n\n`;
      });
    }

    navigator.clipboard.writeText(summaryText);
    setCopiedSummary(true);
    activity(`Copied executive shortlist summary for ${selectedOrder.title} to clipboard.`, "success");
    setTimeout(() => setCopiedSummary(false), 2500);
  };

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
        <div className="cm-dialog sh-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
          <div className="sh-modal-head">
            <div>
              <h3 className="sh-modal-title">Edit job order</h3>
              {editingOrder && (
                <p className="sh-modal-sub">
                  {editingOrder.client} · {editingOrder.id}
                </p>
              )}
            </div>
            <button className="sh-modal-close" onClick={closeEditModal} aria-label="Close">
              <X size={18} />
            </button>
          </div>

          <form onSubmit={handleUpdateOrder}>
            <div className="modal-body sh-modal-body">
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
                  <span className="modal-label">Due Date</span>
                  <DatePicker
                    value={editDueDate}
                    onChange={setEditDueDate}
                    ariaLabel="Due date"
                    placeholder="No due date"
                  />

                  <div style={{ display: "flex", alignItems: "center", gap: "5px", flexWrap: "wrap", marginTop: "6px" }}>
                    <span style={{ fontSize: "0.72rem", fontWeight: 700, color: "var(--text-light)" }}>EXTEND:</span>
                    {[7, 15, 30].map((d) => (
                      <button
                        key={d}
                        type="button"
                        onClick={() => bumpEditDueDate(d)}
                        style={{
                          background: "var(--tint-1)",
                          border: "1px solid var(--border-blue)",
                          color: "var(--text-muted)",
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

            <div className="modal-footer">
              <button type="button" className="modal-cancel-btn" onClick={closeEditModal}>
                Cancel
              </button>
              <button type="submit" className="modal-submit-btn">
                Save changes
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
    const order = selectedOrder;

    const renderMatchCard = (res: MatchResult) => {
      const profile = res.candidate.profile || {};
      const name = profile.full_name || res.candidate.source_email?.from_name || "Extracted candidate";
      const designation = profile.current_designation || "Candidate";
      const company =
        profile.current_company && profile.current_company !== designation ? profile.current_company : "";
      const expLabel = profile.total_experience_years
        ? `${profile.total_experience_years} yrs experience`
        : "Fresher";
      const email = profile.email || res.candidate.source_email?.from_addr;

      const band =
        res.matchScore >= 90
          ? { key: "perfect", label: "Perfect match" }
          : res.matchScore >= 65
            ? { key: "strong", label: "Strong match" }
            : res.matchScore >= 35
              ? { key: "partial", label: "Partial match" }
              : { key: "low", label: "Low compatibility" };

      const required = order.skills.length;
      const skillRisk =
        res.matchedSkills.length === 0 ? "bad" : res.matchedSkills.length >= required ? "good" : "warn";
      const roleRisk = res.roleMatched ? "good" : "bad";
      const expRisk = res.expMatched ? "good" : "warn";

      return (
        <article
          key={res.candidate.id}
          className={`mc-card ${res.isSelected ? "is-shortlisted" : ""} ${res.isRejected ? "is-rejected" : ""}`}
        >
          <div className="mc-main">
            <ScoreRadialGauge
              score={res.matchScore}
              state={res.isSelected ? "on" : res.isRejected ? "off" : "open"}
            />

            <div className="mc-identity">
              <div className="mc-name-row">
                <h4 className="mc-name" title={name}>
                  {name}
                </h4>
                <span className={`mc-badge band-${band.key}`}>{band.label}</span>
                {res.isSelected && (
                  <span className="mc-badge mc-badge-on">
                    <CheckCircle size={12} />
                    Shortlisted
                  </span>
                )}
                {res.isRejected && (
                  <span className="mc-badge mc-badge-rejected">
                    <XCircle size={12} />
                    Rejected
                  </span>
                )}
              </div>

              <p className="mc-role" title={`${designation}${company ? ` · ${company}` : ""}`}>
                {designation}
                {company ? ` · ${company}` : ""} · <strong>{expLabel}</strong>
              </p>

              <div className="mc-contacts">
                {email && (
                  <a href={`mailto:${email}`} title={email}>
                    <Mail size={12} />
                    {email}
                  </a>
                )}
                {profile.phone && (
                  <a href={`tel:${profile.phone.replace(/\s+/g, "")}`}>
                    <Phone size={12} />
                    {profile.phone}
                  </a>
                )}
                {profile.location && (
                  <span title={profile.location}>
                    <MapPin size={12} />
                    {profile.location}
                  </span>
                )}
              </div>
            </div>

            <div className="mc-actions" style={{ flexWrap: "wrap", gap: "6px" }}>

              <button
                className={`mc-btn mc-btn-reject ${res.isRejected ? "is-on" : ""}`}
                onClick={() => handleToggleRejectCandidate(order.id, res.candidate.id)}
              >
                {res.isRejected ? <RotateCcw size={15} /> : <XCircle size={15} />}
                <span>{res.isRejected ? "Restore" : "Reject"}</span>
              </button>

              <button
                className={`mc-btn mc-btn-primary ${res.isSelected ? "is-on" : ""}`}
                onClick={() => handleToggleShortlistCandidate(order.id, res.candidate.id)}
              >
                {res.isSelected ? <CheckCircle size={15} /> : <Plus size={15} />}
                <span>{res.isSelected ? "Shortlisted" : "Shortlist"}</span>
              </button>
            </div>
          </div>

          <div className="mc-breakdown">
            <div className={`mc-stat is-${skillRisk}`}>
              <div className="mc-stat-top">
                <span className="mc-stat-label">Skills</span>
                <span className="mc-stat-value">
                  {res.matchedSkills.length}/{required}
                </span>
              </div>
              <span className="mc-track">
                <span className="mc-track-fill" style={{ width: `${(res.skillScore / 50) * 100}%` }} />
              </span>
            </div>

            <div className={`mc-stat is-${roleRisk}`}>
              <div className="mc-stat-top">
                <span className="mc-stat-label">Role</span>
                <span className="mc-stat-value" title={res.roleStatusText}>
                  {res.roleMatched ? "Aligned" : "Mismatch"}
                </span>
              </div>
              <span className="mc-track">
                <span className="mc-track-fill" style={{ width: `${(res.roleScore / 25) * 100}%` }} />
              </span>
            </div>

            <div className={`mc-stat is-${expRisk}`}>
              <div className="mc-stat-top">
                <span className="mc-stat-label">Experience</span>
                <span className="mc-stat-value" title={res.expStatusText}>
                  {res.expMatched ? "Meets" : "Short"}
                </span>
              </div>
              <span className="mc-track">
                <span className="mc-track-fill" style={{ width: `${(res.expScore / 25) * 100}%` }} />
              </span>
            </div>
          </div>

          {/* AI Fit Diagnostic Text */}
          <div style={{ padding: "6px 12px", background: "var(--tint-1, #f8fafc)", borderRadius: "6px", fontSize: "0.76rem", color: "var(--text-muted)", margin: "8px 0 4px 0", display: "flex", alignItems: "center", gap: "6px" }}>
            <Info size={13} style={{ color: "var(--primary)", flexShrink: 0 }} />
            <span>{res.summary}</span>
          </div>

          <div className="mc-matrix">
            {res.matchedSkills.length > 0 && (
              <>
                <span className="mc-matrix-label">Has</span>
                {res.matchedSkills.map((sk) => (
                  <span
                    className={`mc-chip mc-chip-hit ${activeSkillFilter.includes(sk) ? "is-filtered" : ""}`}
                    key={`matched-${sk}`}
                  >
                    <Check size={12} />
                    {sk}
                  </span>
                ))}
              </>
            )}

            {res.missingSkills.length > 0 && (
              <>
                <span className="mc-matrix-label is-gap">Missing</span>
                {res.missingSkills.map((sk) => (
                  <span className="mc-chip mc-chip-miss" key={`missing-${sk}`}>
                    <X size={12} />
                    {sk}
                  </span>
                ))}
              </>
            )}
          </div>
        </article>
      );
    };

    const renderMatchList = () => {
      const weak = matchedCandidateResults.filter((r) => r.matchScore < 35);
      if (matchFilter !== "ALL" || weak.length === 0) {
        return matchedCandidateResults.map(renderMatchCard);
      }

      const worthReading = matchedCandidateResults.filter((r) => r.matchScore >= 35);
      return (
        <>
          {worthReading.map(renderMatchCard)}
          <button
            className="mc-more"
            onClick={() => setShowWeakMatches((prev) => !prev)}
            aria-expanded={showWeakMatches}
          >
            <ChevronDown size={16} className={showWeakMatches ? "is-open" : ""} />
            <span>
              {showWeakMatches ? "Hide" : "Show"} {formatInt(weak.length)} low-compatibility profile
              {weak.length === 1 ? "" : "s"} (under 35%)
            </span>
          </button>
          {showWeakMatches && weak.map(renderMatchCard)}
        </>
      );
    };

    const detailStatus = deriveStatus(selectedOrder);
    const dueMeta = getDueMeta(selectedOrder.dueDate);
    const strongMatches = matchedCandidateResults.filter((r) => r.matchScore >= 65).length;

    return (
      <div className="jod-root">
        <div>
          <button className="jod-back" onClick={() => setSelectedOrder(null)} title="Back to the job orders list">
            <ArrowLeft size={17} />
            <span>All job orders</span>
          </button>
        </div>

        {dueMeta.overdue && selectedOrder.status !== "CLOSED" && (
          <div className="jod-alert">
            <div className="jod-alert-text">
              <AlertTriangle size={20} />
              <div>
                <p className="jod-alert-title">
                  {dueMeta.days === 0 ? "This order is due today" : `Deadline passed — ${dueMeta.label.toLowerCase()}`}
                </p>
                <p className="jod-alert-note">
                  Target close date was {formatDueDate(selectedOrder.dueDate)}. Extend the deadline or close the order.
                </p>
              </div>
            </div>

            <div className="jod-alert-actions">
              <span className="jod-alert-label">Extend by</span>
              {[7, 15, 30].map((d) => (
                <button key={d} className="jod-alert-btn" onClick={() => handleExtendDueDate(selectedOrder, d)}>
                  +{d} days
                </button>
              ))}
              <button className="jod-alert-btn is-solid" onClick={() => openEditModal(selectedOrder)}>
                <Calendar size={13} />
                Pick date
              </button>
            </div>
          </div>
        )}

        <section className={`jod-card tone-${dueMeta.overdue && detailStatus !== "CLOSED" ? "red" : STATUS_TONE[detailStatus]}`}>
          <div className="jod-top">
            <div className="jod-identity">
              <div className="jod-badges">
                <span className="jod-ref">{selectedOrder.id}</span>
                <span className="jod-badge is-status">{detailStatus}</span>
                <span
                  className="jod-badge"
                  style={{ background: dueMeta.bg, color: dueMeta.color, borderColor: dueMeta.border }}
                >
                  <Clock size={12} />
                  {dueMeta.label}
                </span>
              </div>

              <div className="jod-heading">
                <h1 className="jod-title">{selectedOrder.title}</h1>
                <span className="jod-client">
                  <Building2 size={15} />
                  {selectedOrder.client}
                </span>
              </div>

              <div className="jod-actions" style={{ flexWrap: "wrap", gap: "8px" }}>
                {/* Executive Share Shortlist Summary Button */}
                <button className="jod-btn" onClick={handleCopyShortlistSummary} title="Copy Executive Shortlist Summary">
                  {copiedSummary ? <Check size={14} style={{ color: "#16a34a" }} /> : <Share2 size={14} />}
                  <span>{copiedSummary ? "Shortlist Copied!" : "Share Shortlist"}</span>
                </button>

                <button className="jod-btn" onClick={() => openEditModal(selectedOrder)}>
                  <Pencil size={14} />
                  <span>Edit order</span>
                </button>
                <button className="jod-btn" onClick={() => handleToggleCloseOrder(selectedOrder)}>
                  <CheckCircle size={14} />
                  <span>{selectedOrder.status === "CLOSED" ? "Reopen order" : "Close order"}</span>
                </button>
                <button className="jod-btn is-danger" onClick={() => handleDeleteOrder(selectedOrder.id)}>
                  <Trash2 size={14} />
                  <span>Delete</span>
                </button>
              </div>
            </div>

            <div className="jod-progress">
              <div className="jod-progress-top">
                <span className="jod-progress-label">Fulfilment</span>
                <span className="jod-progress-value">
                  {fulfilled} / {totalHead}
                </span>
              </div>
              <div className="jod-progress-track">
                <span className="jod-progress-fill" style={{ width: `${progressPct}%` }} />
              </div>
              <span className="jod-progress-note">
                {progressPct >= 100
                  ? "All positions covered by the shortlist"
                  : `${totalHead - fulfilled} position${totalHead - fulfilled === 1 ? "" : "s"} still to fill`}
              </span>
            </div>
          </div>

          <dl className="jod-specs">
            <div className="jod-spec jod-spec-wide">
              <dt>Required skills</dt>
              <dd>
                {(selectedOrder.skills || []).length === 0 ? (
                  <span className="jod-spec-empty">None specified</span>
                ) : (
                  <span className="jod-spec-chips">
                    {selectedOrder.skills.map((sk) => (
                      <span className="jod-spec-chip" key={`req-${sk}`}>
                        {sk}
                      </span>
                    ))}
                  </span>
                )}
              </dd>
            </div>

            <div className="jod-spec">
              <dt>Industry</dt>
              <dd>{selectedOrder.industry || "General"}</dd>
            </div>

            <div className="jod-spec">
              <dt>Designation</dt>
              <dd>{selectedOrder.designation || selectedOrder.title}</dd>
            </div>

            <div className="jod-spec">
              <dt>Min. experience</dt>
              <dd>{selectedOrder.minExperience || "Any"}</dd>
            </div>

            <div className="jod-spec">
              <dt>Expected salary</dt>
              <dd>₹ {formatSalary(selectedOrder.salary)}</dd>
            </div>

            <div className="jod-spec">
              <dt>Due date</dt>
              <dd style={{ color: dueMeta.color }}>
                <Calendar size={13} />
                {formatDueDate(selectedOrder.dueDate)}
              </dd>
            </div>

            <div className="jod-spec jod-spec-wide">
              <dt>Remarks</dt>
              <dd>
                {selectedOrder.description || <span className="jod-spec-empty">No internal notes added</span>}
              </dd>
            </div>
          </dl>
        </section>

        <div className="jod-matches">
          <div className="jod-section-head">
            <h2 className="jod-section-title">
              <Users size={19} />
              Candidate matches
            </h2>
            <span className="jod-section-count">
              {formatInt(matchedCandidateResults.length)} of {formatInt(dbCandidates.length)} profiles shown
            </span>
          </div>

          <div className="stat-tiles">
            <StatTile
              tone={scoreTone(matchedCandidateResults[0]?.matchScore ?? 0)}
              icon={Target}
              label="Top match"
              value={`${matchedCandidateResults[0]?.matchScore ?? 0}%`}
              note={matchedCandidateResults[0]?.candidate.profile?.full_name || "No candidates evaluated"}
            />
            <StatTile
              tone="blue"
              icon={UserCheck}
              label="Strong fits (65%+)"
              value={formatInt(strongMatches)}
              note={`out of ${formatInt(dbCandidates.length)} profile${dbCandidates.length === 1 ? "" : "s"} evaluated`}
            />
            <StatTile
              tone="green"
              icon={CheckCircle}
              label="Shortlisted"
              value={formatInt(shortlistedIds.length)}
              note={`${formatInt(totalHead)} seat${totalHead === 1 ? "" : "s"} on this order`}
            />
            <StatTile
              tone={rejectedIds.length > 0 ? "red" : "slate"}
              icon={XCircle}
              label="Rejected"
              value={formatInt(rejectedIds.length)}
              note="Screened out of this order"
            />
          </div>

          <div className="jo-toolbar jod-toolbar">
            <div className="jo-filters">
              <span className="jo-filters-label">
                <SlidersHorizontal size={14} /> FILTER
              </span>
              {([
                { id: "ALL", label: "All matches", count: matchedCandidateResults.length },
                { id: "TOP", label: "Strong fits", count: dbCandidates.length > 0 ? strongMatches : 0 },
                { id: "SHORTLISTED", label: "Shortlisted", count: shortlistedIds.length },
                { id: "REJECTED", label: "Rejected", count: rejectedIds.length },
                { id: "ROLE", label: "Role aligned", count: null },
              ] as const).map((tab) => (
                <button
                  key={tab.id}
                  className={`job-order-status-chip ${matchFilter === tab.id ? "active" : ""}`}
                  onClick={() => setMatchFilter(tab.id)}
                  aria-pressed={matchFilter === tab.id}
                >
                  {tab.label}
                  {tab.count !== null && <span className="jo-chip-count">{tab.count}</span>}
                </button>
              ))}
            </div>

            <div className="jod-sort">
              <span className="jo-filters-label">
                <ArrowUpDown size={14} /> SORT
              </span>
              <div className="sh-select">
                <select
                  value={matchSort}
                  onChange={(e) => setMatchSort(e.target.value as typeof matchSort)}
                  aria-label="Sort candidate matches"
                >
                  <option value="SCORE">Match score (high to low)</option>
                  <option value="EXP">Experience (high to low)</option>
                  <option value="NAME">Candidate name (A–Z)</option>
                </select>
                <ChevronDown size={14} />
              </div>
            </div>

            {(selectedOrder.skills || []).length > 0 && (
              <div className="jod-skillsel">
                <span className="jo-filters-label">
                  <Sparkles size={14} /> SKILLS
                </span>

                <div className="jod-dd">
                  <div onClick={(e) => e.stopPropagation()}>
                    <button
                      className={`jod-dd-trigger ${skillMenuOpen ? "is-open" : ""} ${activeSkillFilter.length > 0 ? "is-set" : ""}`}
                      onClick={() => setSkillMenuOpen((prev) => !prev)}
                      aria-expanded={skillMenuOpen}
                      aria-haspopup="true"
                    >
                      <span>{skillTriggerLabel}</span>
                      {activeSkillFilter.length > 0 && (
                        <span className="jo-chip-count">{matchedCandidateResults.length}</span>
                      )}
                      <ChevronDown size={14} className={skillMenuOpen ? "is-open" : ""} />
                    </button>

                    {skillMenuOpen && (
                      <div className="jod-dd-panel" role="dialog" aria-label="Filter by skill">
                        <div className="jod-dd-list">
                          {(selectedOrder.skills || []).map((skill) => {
                            const count = skillCoverage.get(skill) ?? 0;
                            const on = activeSkillFilter.includes(skill);
                            return (
                              <label
                                key={skill}
                                className={`jod-dd-opt ${count === 0 ? "is-empty" : ""}`}
                                title={
                                  count === 0
                                    ? `No candidate in this pool has ${skill}`
                                    : `${count} candidate${count === 1 ? "" : "s"} with ${skill}`
                                }
                              >
                                <input
                                  type="checkbox"
                                  checked={on}
                                  onChange={() => toggleSkillFilter(skill)}
                                />
                                <span className="jod-dd-opt-name">{skill}</span>
                                <span className="jo-chip-count">{count}</span>
                              </label>
                            );
                          })}
                        </div>

                        {activeSkillFilter.length > 1 && (
                          <div className="jod-dd-mode" role="radiogroup" aria-label="Skill match mode">
                            {(["ANY", "ALL"] as const).map((mode) => (
                              <label key={mode} className={skillMode === mode ? "is-on" : ""}>
                                <input
                                  type="radio"
                                  name="skill-mode"
                                  checked={skillMode === mode}
                                  onChange={() => setSkillMode(mode)}
                                />
                                <span>{mode === "ANY" ? "Any of these" : "All of these"}</span>
                              </label>
                            ))}
                          </div>
                        )}

                        <div className="jod-dd-foot">
                          <button
                            className="jod-dd-clear"
                            onClick={() => setSkillFilter([])}
                            disabled={activeSkillFilter.length === 0}
                          >
                            Clear
                          </button>
                          <button className="jod-dd-done" onClick={() => setSkillMenuOpen(false)}>
                            Done
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )}
          </div>

          <div className="jod-match-list">
            {candidatesLoading ? (
              <div className="jo-state">
                <Loader2 size={26} className="jo-spinner" />
                <p>Scanning the talent database for matches…</p>
              </div>
            ) : matchedCandidateResults.length === 0 ? (
              <div className="jo-state jo-state-empty">
                <Inbox size={40} strokeWidth={1.5} />
                <p>
                  {dbCandidates.length === 0
                    ? "No candidate profiles in the database yet"
                    : matchFilter === "REJECTED"
                      ? "No rejected candidates for this order"
                      : activeSkillFilter.length > 0
                        ? `Nobody in this pool has ${skillMode === "ALL" ? "all of" : "any of"
                        } ${activeSkillFilter.join(", ")}`
                        : "No candidates match this filter"}
                </p>
                <span>
                  {dbCandidates.length === 0
                    ? "Run the inbox poller to ingest resumes — matches will appear here automatically."
                    : activeSkillFilter.length > 1 && skillMode === "ALL"
                      ? "Switch the skill filter to “Any of these” to see who covers part of the requirement."
                      : "Try switching back to “All matches”, or widen the required skills on this order."}
                </span>
                {dbCandidates.length > 0 && (matchFilter !== "ALL" || activeSkillFilter.length > 0) && (
                  <button
                    className="btn-new-client"
                    onClick={() => {
                      setMatchFilter("ALL");
                      setSkillFilter([]);
                    }}
                  >
                    <RotateCcw size={15} />
                    <span>Show all matches</span>
                  </button>
                )}
              </div>
            ) : (
              renderMatchList()
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
    <div className="job-orders-wrapper">
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

      {!ordersLoading && filteredOrders.length > 0 && (
        <section className="ds-panel jo-register" aria-label="Job orders">
          <div className="ds-table-wrap is-ruled">
            <table className="ds-table is-ruled jo-table">
              <thead>
                <tr>
                  <th>Job order</th>
                  <th>Client</th>
                  <th>Status</th>
                  <th>Salary</th>
                  <th>Experience</th>
                  <th className="is-num">Filled</th>
                  <th>Due date</th>
                  <th>Match</th>
                  <th className="is-actions" aria-label="Actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredOrders.map((item) => {
                  const fulfilled = (item.shortlistedCandidateIds || []).length || item.fulfilledCount || 0;
                  const rowStatus = deriveStatus(item);
                  const due = getDueMeta(item.dueDate);
                  const isLate = due.overdue && rowStatus !== "CLOSED";
                  const match = matchSummaryByOrder.get(item.id);
                  const statusTone = isLate
                    ? "bad"
                    : rowStatus === "FILLED"
                      ? "ok"
                      : rowStatus === "CLOSED"
                        ? "neutral"
                        : "info";

                  return (
                    <tr className="is-clickable" key={item.id} onClick={() => setSelectedOrder(item)}>
                      <td>
                        <span className="jo-table-job">
                          <strong title={item.title}>{item.title}</strong>
                          <small>
                            {(item.skills || []).slice(0, 3).join(" · ") || "No skills specified"}
                          </small>
                        </span>
                      </td>
                      <td>
                        <span className="jo-table-client">
                          <span className="ds-avatar" aria-hidden="true">{initialsOf(item.client)}</span>
                          <span title={item.client}>{item.client}</span>
                        </span>
                      </td>
                      <td>
                        <span className={`ds-status is-${statusTone}`}>
                          <i aria-hidden="true" />
                          {isLate ? "Overdue" : rowStatus.replace("IN PROGRESS", "In progress")}
                        </span>
                      </td>
                      <td>{formatSalary(item.salary)}</td>
                      <td>{item.minExperience && item.minExperience !== "Any" ? item.minExperience : "Open"}</td>
                      <td className="is-num"><strong>{fulfilled}</strong> of {item.headcount}</td>
                      <td>
                        <span className={`jo-table-due ${isLate ? "is-late" : ""}`}>
                          {formatDueDate(item.dueDate)}
                          <small>{due.label}</small>
                        </span>
                      </td>
                      <td>
                        <span className="jo-table-match">
                          <strong>{match?.best ?? 0}%</strong>
                          <small>{match?.strong ? `${match.strong} strong` : "No strong matches"}</small>
                        </span>
                      </td>
                      <td className="is-actions" onClick={(event) => event.stopPropagation()}>
                        <div className="jo-actions">
                          <button
                            type="button"
                            className="jo-icon-btn"
                            onClick={() => openEditModal(item)}
                            title="Edit this job order"
                            aria-label={`Edit ${item.title}`}
                          >
                            <Pencil size={14} />
                          </button>
                          <div className="jo-menu-wrap">
                            <button
                              type="button"
                              className="jo-icon-btn"
                              onClick={() => setActiveMenuId((prev) => (prev === item.id ? null : item.id))}
                              aria-label={`Actions for ${item.title}`}
                            >
                              <MoreVertical size={15} />
                            </button>
                            {activeMenuId === item.id && (
                              <div className="sourcing-dropdown-menu">
                                <button type="button" className="dropdown-item" onClick={() => openEditModal(item)}>
                                  <Pencil size={14} /><span>Edit</span>
                                </button>
                                <button type="button" className="dropdown-item" onClick={() => handleToggleCloseOrder(item)}>
                                  <CheckCircle size={14} />
                                  <span>{item.status === "CLOSED" ? "Reopen order" : "Close order"}</span>
                                </button>
                                <button type="button" className="dropdown-item danger" onClick={() => handleDeleteOrder(item.id)}>
                                  <Trash2 size={14} /><span>Delete</span>
                                </button>
                              </div>
                            )}
                          </div>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <div className={`jo-grid ${!ordersLoading && filteredOrders.length > 0 ? "is-hidden" : ""}`}>
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
            const cardDue = getDueMeta(item.dueDate);
            const isLate = cardDue.overdue && cardStatus !== "CLOSED";
            const extraSkills = Math.max(0, (item.skills || []).length - 4);
            const match = matchSummaryByOrder.get(item.id);

            return (
              <article
                className={`job-order-card tone-${isLate ? "red" : STATUS_TONE[cardStatus]} ${isLate ? "is-late" : ""}`}
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
                      <span className="jo-status">{isLate ? "OVERDUE" : cardStatus}</span>
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

                <div className="jo-skill-tags">
                  {(item.skills || []).length > 0 ? (
                    <>
                      {(item.skills || []).slice(0, 4).map((sk) => (
                        <span className="jo-skill" key={`${item.id}-${sk}`}>
                          {sk}
                        </span>
                      ))}
                      {extraSkills > 0 && <span className="jo-skill-more">+{extraSkills} more</span>}
                    </>
                  ) : (
                    <span className="jo-skill jo-skill-empty">No skills specified</span>
                  )}
                </div>

                <p className={`jo-match ${match && match.strong > 0 ? "has-matches" : ""}`}>
                  <Target size={14} />
                  {match ? (
                    match.strong > 0 ? (
                      <span>
                        <strong>{match.strong}</strong> strong match
                        {match.strong === 1 ? "" : "es"} · best <strong>{match.best}%</strong>
                      </span>
                    ) : (
                      <span>
                        No strong matches yet{match.best > 0 ? ` · best ${match.best}%` : ""}
                      </span>
                    )
                  ) : (
                    <span>No candidate profiles to match against yet</span>
                  )}
                </p>

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

      {isCreateModalOpen && (
        <div className="cm-overlay active" onClick={() => setIsCreateModalOpen(false)}>
          <div className="cm-dialog sh-modal" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
            <div className="sh-modal-head">
              <div>
                <h3 className="sh-modal-title">New job order</h3>
                <p className="sh-modal-sub">
                  Raise a requisition against a sourcing client — candidates are matched to it automatically.
                </p>
              </div>
              <button className="sh-modal-close" onClick={() => setIsCreateModalOpen(false)} aria-label="Close">
                <X size={18} />
              </button>
            </div>

            <form onSubmit={handleCreateOrder}>
              <div className="modal-body sh-modal-body">
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
                    <span className="modal-label">Target Close Date</span>
                    <DatePicker
                      value={newDueDate}
                      onChange={setNewDueDate}
                      ariaLabel="Target close date"
                      placeholder="30 days from today"
                    />
                    <span style={{ fontSize: "0.72rem", color: "var(--text-light)", fontWeight: 500 }}>
                      Leave blank to default to 30 days from today.
                    </span>
                  </div>
                </div>

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

              <div className="modal-footer">
                <button
                  type="button"
                  className="modal-cancel-btn"
                  onClick={() => setIsCreateModalOpen(false)}
                >
                  Cancel
                </button>
                <button type="submit" className="modal-submit-btn">
                  Create order
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
