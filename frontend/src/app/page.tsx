"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Menu, RefreshCw } from "lucide-react";

import Sidebar from "@/components/Sidebar";
import DashboardView from "@/screens/DashboardView";
import CandidatesView, { type CandidateLog } from "@/screens/CandidatesView";
import FlowVisualizer, { IDLE_FLOW, type FlowState } from "@/screens/FlowVisualizer";
import SourcingHub from "@/screens/SourcingHub";
import JobOrders from "@/screens/JobOrders";
import LoginScreen from "@/screens/LoginScreen";
import CandidateModal from "@/components/CandidateModal";
import Toast, { type ToastState, type ToastType } from "@/components/Toast";
import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { candidateNameOf } from "@/lib/format";
import {
  fetchMe,
  getToken,
  logout as clearSession,
  deleteCandidateAPI,
  listCandidates,
  runPollCycle,
  updateCandidateProfile,
  verifyCandidate,
  type AuthUser,
  type CandidateProfile,
  type CandidateRecord,
} from "@/lib/api";

type TabId = "dashboard" | "candidates" | "visualizer" | "sourcing" | "job-orders";

const PAGE_META: Record<TabId, { title: string; subtitle: string }> = {
  dashboard: {
    title: "Dashboard Overview",
    subtitle: "Real-time candidate ingestion statistics.",
  },
  candidates: {
    title: "Candidates Directory",
    subtitle: "Browse, filter, and inspect parsed profiles.",
  },
  visualizer: {
    title: "Automation Pipeline",
    subtitle: "Visually monitor the Gmail-to-MongoDB flow.",
  },
  sourcing: {
    title: "Sourcing Hub",
    subtitle: "Manage Associations and Business Clients.",
  },
  "job-orders": {
    title: "Job Orders",
    subtitle: "Manage your open positions and client requirements.",
  },
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const logEntry = (message: string, type: LogEntry["type"] = "info"): LogEntry => ({
  time: new Date().toLocaleTimeString(),
  type,
  message,
});

export default function Home() {
  // null = signed out. `checking` covers the first paint, where a stored token
  // exists but has not been validated yet — without it the login screen would
  // flash on every refresh for an already-signed-in user.
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checking, setChecking] = useState(true);

  const [activeTab, setActiveTab] = useState<TabId>("dashboard");
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  const [candidates, setCandidates] = useState<CandidateRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [candidateLogs, setCandidateLogs] = useState<CandidateLog[]>([]);
  const [toast, setToast] = useState<ToastState | null>(null);

  const [selected, setSelected] = useState<CandidateRecord | null>(null);
  const [openInEdit, setOpenInEdit] = useState(false);
  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);

  const [flow, setFlow] = useState<FlowState>(IDLE_FLOW);
  const [syncing, setSyncing] = useState(false);
  const syncingRef = useRef(false);
  const bootLoggedRef = useRef(false);

  // ---- helpers ---------------------------------------------------------- //
  /**
   * Two logs, and an entry belongs to exactly one of them. Anything scoped to a
   * candidate — viewed, edited, verified, deleted — is that person's history and
   * lives on their row in the directory. The dashboard's trace is reserved for
   * the pipeline and for record-level changes, so a few minutes of browsing
   * profiles no longer buries the sync it is there to show.
   */
  const log = useCallback((message: string, type: LogEntry["type"] = "info", candidateId?: string) => {
    const entry = logEntry(message, type);
    if (candidateId) {
      setCandidateLogs((prev) => [...prev, { ...entry, candidateId }]);
      return;
    }
    setLogs((prev) => [...prev, entry]);
  }, []);

  const showToast = useCallback((message: string, type: ToastType = "info") => {
    setToast({ message, type, key: Date.now() });
  }, []);

  /** Resolve an id to the person's name so the log reads like a sentence. */
  const nameOf = useCallback(
    (candidateId: string) => candidateNameOf(candidates.find((c) => c.id === candidateId)),
    [candidates],
  );

  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(null), 4000);
    return () => clearTimeout(timer);
  }, [toast]);

  const refreshCandidates = useCallback(async () => {
    try {
      const data = await listCandidates();
      setCandidates(data.items ?? []);
      setTotal(data.total ?? 0);
      return data.items ?? [];
    } catch (err) {
      log(err instanceof Error ? err.message : "Failed to fetch candidates from DB.", "error");
      return [];
    }
  }, [log]);

  // ---- session ---------------------------------------------------------- //
  useEffect(() => {
    let active = true;
    const token = getToken();
    // Resolve through a promise even when there is no token, so every state
    // update happens in a callback rather than synchronously inside the effect.
    const check = token ? fetchMe() : Promise.reject(new Error("no session"));

    check.then(
      (me) => {
        if (!active) return;
        setUser(me);
        setChecking(false);
      },
      () => {
        // No token, or expired/tampered — api.ts has already cleared it.
        if (active) setChecking(false);
      },
    );

    return () => {
      active = false;
    };
  }, []);

  const handleSignOut = useCallback(() => {
    clearSession();
    setUser(null);
    setCandidates([]);
    setTotal(0);
    setLogs([]);
    setCandidateLogs([]);
    // So the next session opens with its own connect banner.
    bootLoggedRef.current = false;
    setSelected(null);
    setActiveTab("dashboard");
  }, []);

  // ---- bootstrap -------------------------------------------------------- //
  useEffect(() => {
    if (!user) return;
    let active = true;

    listCandidates().then(
      (data) => {
        if (!active) return;
        setCandidates(data.items ?? []);
        setTotal(data.total ?? 0);
        // The connect banner is a once-per-session statement. A remount — a
        // dev fast-refresh, a re-run of this effect — was appending a second
        // identical pair, so the trace opened with the same two lines twice.
        if (bootLoggedRef.current) return;
        bootLoggedRef.current = true;
        setLogs((prev) => [
          ...prev,
          logEntry(
            `Connected to MongoDB Atlas — ${data.total} candidate record(s) loaded.`,
            "success",
          ),
          logEntry("Waiting for Gmail sync requests...", "info"),
        ]);
      },
      (err: unknown) => {
        if (!active || bootLoggedRef.current) return;
        bootLoggedRef.current = true;
        setLogs((prev) => [
          ...prev,
          logEntry(
            `Backend unreachable: ${err instanceof Error ? err.message : String(err)}`,
            "error",
          ),
        ]);
      },
    );

    const interval = setInterval(() => {
      refreshCandidates();
    }, 5000);

    return () => {
      active = false;
      clearInterval(interval);
    };
  }, [refreshCandidates, user]);

  const handleTabChange = (tab: string) => {
    const next = tab as TabId;
    setActiveTab(next);
    if (next === "candidates") void refreshCandidates();
  };

  // ---- pipeline run ----------------------------------------------------- //
  const runPipeline = useCallback(async () => {
    if (syncingRef.current) return;
    syncingRef.current = true;
    setSyncing(true);

    log("Step 1: Connecting to Gmail API & searching for unread candidate emails...", "info");
    setFlow({ ...IDLE_FLOW, gmail: "active" });
    await sleep(600);

    try {
      setFlow((prev) => ({ ...prev, gmail: "success", connGmailFilter: true, filter: "active" }));
      log("Step 2: Applying resume detection filter & score validation...", "info");
      await sleep(600);

      const summary = await runPollCycle();

      setFlow((prev) => ({ ...prev, filter: "success", connFilterVeris: true, veris: "active" }));
      log("Step 3: Sending attachments to Veris LLM Resume API for character-by-character OCR & LLM extraction...", "info");
      await sleep(700);

      if (summary.results && summary.results.length > 0) {
        for (const msgRes of summary.results) {
          for (const attRes of msgRes.attachments) {
            if (attRes.status === "ingested") {
              log(`[SUCCESS] Attachment '${attRes.filename}': Extracted cleanly & saved to MongoDB Atlas.`, "success");
            } else if (attRes.status === "duplicate") {
              log(`[NOTICE] Attachment '${attRes.filename}': Skipped (${attRes.detail || "candidate/file already exists"}).`, "warn");
            } else if (attRes.status === "failed") {
              log(`[ERROR] Attachment '${attRes.filename}': ${attRes.detail || "failed to parse"}.`, "error");
            }
          }
        }
      }

      setFlow((prev) => ({ ...prev, veris: "success", connVerisDb: true, db: "active" }));
      log("Step 4: Writing structured Candidate Profile to MongoDB Atlas collection 'candidates'...", "info");
      await sleep(700);

      setFlow((prev) => ({ ...prev, db: "success" }));

      // A cycle that declined the lock did no work at all. Saying "no new
      // resumes found" there would be a lie — nothing was even looked at.
      if (summary.skipped_reason) {
        log(`[NOTICE] ${summary.skipped_reason}`, "warn");
        showToast("A sync is already running. Its results will appear shortly.", "info");
      } else {
        log(
          `[COMPLETE] Pipeline finished. Fetched=${summary.fetched}, Processed=${summary.processed}, Ingested Candidates=${summary.ingested_candidates}.`,
          "success",
        );

        if (summary.ingested_candidates > 0) {
          showToast(
            `Ingestion completed! Added ${summary.ingested_candidates} new candidate profile(s).`,
            "success",
          );
        } else {
          showToast("Sync completed. No new unread candidate resumes found in Gmail inbox.", "info");
        }
      }

      await refreshCandidates();
    } catch (err: unknown) {
      setFlow(IDLE_FLOW);
      const message = err instanceof Error ? err.message : "Unknown error";
      log(`[ERROR] Inbound pipeline execution failed: ${message}`, "error");
      showToast("Failed to poll Gmail inbox.", "error");
    }

    await sleep(4000);
    setFlow(IDLE_FLOW);
    setSyncing(false);
    syncingRef.current = false;
  }, [log, refreshCandidates, showToast]);

  // ---- candidate mutations ---------------------------------------------- //
  const handleSave = async (candidateId: string, profile: CandidateProfile) => {
    setSaving(true);
    // Read the name before the save lands — an edit can rename the record, and
    // the log should say who was edited, not who they became.
    const previousName = nameOf(candidateId);
    const nextName = profile.full_name?.trim() || previousName;
    const renamed = nextName !== previousName;

    try {
      await updateCandidateProfile(candidateId, profile);
      showToast("Candidate profile updated successfully.", "success");
      log(
        renamed
          ? `Updated profile: ${previousName} — renamed to ${nextName}.`
          : `Updated profile: ${nextName}.`,
        "success",
        candidateId,
      );
      setSelected(null);
      setOpenInEdit(false);
      await refreshCandidates();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error saving profile.";
      showToast("Failed to save profile changes.", "error");
      log(`Failed to update profile: ${previousName} — ${message}`, "error", candidateId);
    } finally {
      setSaving(false);
    }
  };

  const handleVerify = async (candidateId: string) => {
    setVerifying(true);
    const who = nameOf(candidateId);
    try {
      await verifyCandidate(candidateId);
      showToast("Candidate marked as verified.", "success");
      log(`Verified profile: ${who}.`, "success", candidateId);
      setSelected(null);
      await refreshCandidates();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error verifying candidate.";
      showToast("Failed to verify candidate.", "error");
      log(`Failed to verify: ${who} — ${message}`, "error", candidateId);
    } finally {
      setVerifying(false);
    }
  };

  const handleDeleteCandidate = async (candidateId: string) => {
    // Resolved up front: once the record is gone the name cannot be looked up.
    const who = nameOf(candidateId);
    try {
      log(`Deleting profile: ${who}…`, "warn", candidateId);
      await deleteCandidateAPI(candidateId);
      showToast("Candidate permanently deleted from MongoDB Atlas.", "success");
      log(`Deleted profile: ${who} — removed from MongoDB Atlas.`, "success", candidateId);
      setSelected(null);
      await refreshCandidates();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error deleting candidate.";
      showToast("Failed to delete candidate.", "error");
      log(`Failed to delete: ${who} — ${message}`, "error", candidateId);
    }
  };

  const handleOpenCandidate = (candidate: CandidateRecord) => {
    setOpenInEdit(false);
    setSelected(candidate);
    log(`Viewed profile: ${candidateNameOf(candidate)}.`, "info", candidate.id);
  };

  const handleEditCandidate = (candidate: CandidateRecord) => {
    setOpenInEdit(true);
    setSelected(candidate);
    log(`Opened editor: ${candidateNameOf(candidate)}.`, "info", candidate.id);
  };

  const meta = PAGE_META[activeTab];

  if (checking) {
    return (
      <div className="app-boot">
        <span className="app-boot-spinner" />
      </div>
    );
  }

  if (!user) {
    return <LoginScreen onSuccess={setUser} />;
  }

  return (
    <>
      <Sidebar
        collapsed={collapsed}
        onToggleCollapse={() => setCollapsed((prev) => !prev)}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        activeTab={activeTab}
        onTabChange={handleTabChange}
        user={user}
        onSignOut={handleSignOut}
      />

      {/* `screen-dashboard` carries the dashboard's own typeface. It sits on the
          content column rather than on the dashboard view itself so the header
          bar above the tiles is set in the same face — scoping it any tighter
          splits one screen across two fonts at the page title. */}
      <div
        className={`main-content ${collapsed ? "sidebar-collapsed" : ""} ${
          activeTab === "dashboard" ? "screen-dashboard" : ""
        }`}
      >
        <div className="header-bar">
          <div style={{ display: "flex", alignItems: "center", gap: "1rem" }}>
            <button
              className="mobile-menu-btn"
              onClick={() => setMobileOpen((prev) => !prev)}
              aria-label="Open navigation"
            >
              <Menu size={20} />
            </button>
            <div>
              <h1 className="page-title">{meta.title}</h1>
              <p className="page-subtitle">{meta.subtitle}</p>
            </div>
          </div>

          <button className="btn" onClick={runPipeline} disabled={syncing}>
            <RefreshCw size={18} className={syncing ? "icon-spin" : undefined} />
            <span>{syncing ? "Syncing..." : "Sync Gmail Inbox"}</span>
          </button>
        </div>

        {activeTab === "dashboard" && (
          <DashboardView
            total={total}
            candidates={candidates}
            logs={logs}
            onOpenCandidate={setSelected}
          />
        )}

        {activeTab === "candidates" && (
          <CandidatesView
            candidates={candidates}
            logs={candidateLogs}
            onOpenCandidate={handleOpenCandidate}
            onEditCandidate={handleEditCandidate}
            onDeleteCandidate={handleDeleteCandidate}
          />
        )}

        {activeTab === "visualizer" && (
          <FlowVisualizer flow={flow} syncing={syncing} onTrigger={runPipeline} />
        )}

        {activeTab === "sourcing" && <SourcingHub onActivity={log} />}

        {activeTab === "job-orders" && <JobOrders candidates={candidates} onActivity={log} />}
      </div>

      <CandidateModal
        candidate={selected}
        saving={saving}
        verifying={verifying}
        initialEditMode={openInEdit}
        onClose={() => { setSelected(null); setOpenInEdit(false); }}
        onSave={handleSave}
        onVerify={handleVerify}
        onDelete={handleDeleteCandidate}
      />

      <Toast toast={toast} />
    </>
  );
}
