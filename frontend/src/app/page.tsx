"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Menu, Plus, RefreshCw } from "lucide-react";

import Sidebar from "@/components/Sidebar";
import DashboardView from "@/screens/DashboardView";
import CandidatesView from "@/screens/CandidatesView";
import FlowVisualizer, { IDLE_FLOW, type FlowState } from "@/screens/FlowVisualizer";
import SourcingHub from "@/screens/SourcingHub";
import JobOrders from "@/screens/JobOrders";
import CandidateModal from "@/components/CandidateModal";
import Toast, { type ToastState, type ToastType } from "@/components/Toast";
import type { LogEntry } from "@/components/LogsConsole";
import {
  deleteCandidateAPI,
  listCandidates,
  triggerPoll,
  updateCandidateProfile,
  verifyCandidate,
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
  const [activeTab, setActiveTab] = useState<TabId>("dashboard");
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  const [candidates, setCandidates] = useState<CandidateRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [toast, setToast] = useState<ToastState | null>(null);

  const [selected, setSelected] = useState<CandidateRecord | null>(null);
  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);

  const [flow, setFlow] = useState<FlowState>(IDLE_FLOW);
  const [syncing, setSyncing] = useState(false);
  const syncingRef = useRef(false);

  // ---- helpers ---------------------------------------------------------- //
  const log = useCallback((message: string, type: LogEntry["type"] = "info") => {
    setLogs((prev) => [...prev, logEntry(message, type)]);
  }, []);

  const showToast = useCallback((message: string, type: ToastType = "info") => {
    setToast({ message, type, key: Date.now() });
  }, []);

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

  // ---- bootstrap -------------------------------------------------------- //
  useEffect(() => {
    let active = true;

    listCandidates().then(
      (data) => {
        if (!active) return;
        setCandidates(data.items ?? []);
        setTotal(data.total ?? 0);
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
        if (!active) return;
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
  }, [refreshCandidates]);

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

      const summary = await triggerPoll();

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
    try {
      await updateCandidateProfile(candidateId, profile);
      showToast("Candidate profile updated successfully.", "success");
      log(`Profile ${candidateId} updated.`, "success");
      setSelected(null);
      await refreshCandidates();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error saving profile.";
      showToast("Failed to save profile changes.", "error");
      log(`Failed to save profile ${candidateId}: ${message}`, "error");
    } finally {
      setSaving(false);
    }
  };

  const handleVerify = async (candidateId: string) => {
    setVerifying(true);
    try {
      await verifyCandidate(candidateId);
      showToast("Candidate marked as verified.", "success");
      log(`Candidate ${candidateId} marked verified.`, "success");
      setSelected(null);
      await refreshCandidates();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error verifying candidate.";
      showToast("Failed to verify candidate.", "error");
      log(`Failed to verify ${candidateId}: ${message}`, "error");
    } finally {
      setVerifying(false);
    }
  };

  const handleDeleteCandidate = async (candidateId: string) => {
    try {
      await deleteCandidateAPI(candidateId);
      showToast("Candidate permanently deleted from MongoDB Atlas.", "success");
      log(`Candidate ${candidateId} permanently deleted from MongoDB Atlas.`, "success");
      setSelected(null);
      await refreshCandidates();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error deleting candidate.";
      showToast("Failed to delete candidate.", "error");
      log(`Failed to delete candidate ${candidateId}: ${message}`, "error");
    }
  };

  const meta = PAGE_META[activeTab];

  return (
    <>
      <Sidebar
        collapsed={collapsed}
        onToggleCollapse={() => setCollapsed((prev) => !prev)}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        activeTab={activeTab}
        onTabChange={handleTabChange}
      />

      <div className={`main-content ${collapsed ? "sidebar-collapsed" : ""}`}>
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
          <DashboardView total={total} candidates={candidates} logs={logs} />
        )}

        {activeTab === "candidates" && (
          <CandidatesView candidates={candidates} onOpenCandidate={setSelected} />
        )}

        {activeTab === "visualizer" && (
          <FlowVisualizer flow={flow} syncing={syncing} onTrigger={runPipeline} />
        )}

        {activeTab === "sourcing" && <SourcingHub />}

        {activeTab === "job-orders" && <JobOrders candidates={candidates} />}
      </div>

      <CandidateModal
        candidate={selected}
        saving={saving}
        verifying={verifying}
        onClose={() => setSelected(null)}
        onSave={handleSave}
        onVerify={handleVerify}
        onDelete={handleDeleteCandidate}
      />

      <Toast toast={toast} />
    </>
  );
}
