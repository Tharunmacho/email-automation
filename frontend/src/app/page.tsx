"use client";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import Sidebar from "@/components/Sidebar";
import TopBar from "@/components/TopBar";
import OverviewScreen from "@/screens/OverviewScreen";
import ResumeParserScreen from "@/screens/ResumeParserScreen";
import PipelineScreen from "@/screens/PipelineScreen";
import SourcingHub from "@/screens/SourcingHub";
import CandidatesView from "@/screens/CandidatesView";
import JobOrders from "@/screens/JobOrders";
import ActivityLogsScreen from "@/screens/ActivityLogsScreen";
import SettingsScreen from "@/screens/SettingsScreen";
import CandidateProfileScreen from "@/screens/CandidateProfileScreen";
import CandidateEditScreen from "@/screens/CandidateEditScreen";
import CandidateLogsScreen from "@/screens/CandidateLogsScreen";
import LoginScreen from "@/screens/LoginScreen";
import { IDLE_FLOW, type FlowState } from "@/screens/FlowVisualizer";
import Toast, { type ToastState, type ToastType } from "@/components/Toast";
import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { candidateNameOf } from "@/lib/format";
import { summariseProfileChange } from "@/lib/candidateProfile";
import { NAV_META, type NavId } from "@/lib/nav";
import {
  appendCandidateLog,
  dropCandidateLogs,
  getLogsServerSnapshot,
  getLogsSnapshot,
  subscribeLogs,
} from "@/lib/candidateLog";
import {
  fetchMe,
  getToken,
  logout as clearSession,
  deleteCandidateAPI,
  listCandidates,
  runPollCycle,
  updateCandidateProfile,
  verifyCandidate,
  fetchWorkerStatus,
  type AuthUser,
  type CandidateProfile,
  type CandidateRecord,
  type PollSummary,
} from "@/lib/api";

/**
 * A candidate-scoped screen that takes over the content column.
 *
 * These are screens, not overlays, and each one does exactly one job: read the
 * profile, change the profile, or read the profile's history. Keeping them
 * apart is what lets the executive view carry no edit control at all and the
 * editor carry nothing but controls.
 *
 * The candidate is held by id rather than by record, so the five-second poll
 * keeps whichever screen is open in step with the database instead of freezing
 * a snapshot taken when it was opened.
 */
interface CandidateScreen {
  mode: "profile" | "edit" | "logs";
  candidateId: string;
}

const CANDIDATE_SCREEN_META: Record<
  CandidateScreen["mode"],
  { eyebrow: string; title: string; subtitle: string }
> = {
  profile: {
    eyebrow: "Talent Pool",
    title: "Executive Profile",
    subtitle: "The complete parsed résumé. Read-only — nothing here can be changed.",
  },
  edit: {
    eyebrow: "Talent Pool",
    title: "Edit Candidate",
    subtitle: "Change the stored details. Only the fields you can edit are shown.",
  },
  logs: {
    eyebrow: "Talent Pool",
    title: "Candidate Activity",
    subtitle: "Everything that has happened to this one record.",
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

  const [activeTab, setActiveTab] = useState<NavId>("overview");
  const [railCollapsed, setRailCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  const [candidates, setCandidates] = useState<CandidateRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [toast, setToast] = useState<ToastState | null>(null);

  // Persisted outside React, so a reload does not erase who edited what.
  const candidateLogs = useSyncExternalStore(subscribeLogs, getLogsSnapshot, getLogsServerSnapshot);

  const [screen, setScreen] = useState<CandidateScreen | null>(null);
  const [saving, setSaving] = useState(false);
  const [verifying, setVerifying] = useState(false);
  // Ids with a DELETE in flight — a second click must not fire a second request.
  const deletingRef = useRef<Set<string>>(new Set());

  const [flow, setFlow] = useState<FlowState>(IDLE_FLOW);
  const [syncing, setSyncing] = useState(false);
  // Reported by the Overview and Gmail Sync screens. Null means "not this
  // session" rather than "never" — the backend keeps no run history.
  const [lastSyncAt, setLastSyncAt] = useState<string | null>(null);
  const [lastSummary, setLastSummary] = useState<PollSummary | null>(null);
  const [workersOnline, setWorkersOnline] = useState<boolean | null>(null);
  const syncingRef = useRef(false);
  const bootLoggedRef = useRef(false);

  // ---- helpers ---------------------------------------------------------- //
  /**
   * Two logs, and an entry belongs to exactly one of them. Anything scoped to a
   * candidate — viewed, edited, verified, deleted — is that person's history and
   * lives on their own activity screen. The dashboard's trace is reserved for
   * the pipeline and for record-level changes, so a few minutes of browsing
   * profiles no longer buries the sync it is there to show.
   */
  const log = useCallback((message: string, type: LogEntry["type"] = "info") => {
    setLogs((prev) => [...prev, logEntry(message, type)]);
  }, []);

  /** How many events each candidate has — the directory shows this on the row. */
  const logCounts = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const entry of candidateLogs) {
      counts[entry.candidateId] = (counts[entry.candidateId] ?? 0) + 1;
    }
    return counts;
  }, [candidateLogs]);

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
      // null, not []: callers that report the record count must not turn a
      // failed fetch into "0 records remaining".
      return null;
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
    // Candidate history is deliberately NOT cleared: it is the record's audit
    // trail, not this session's scratch state, and the next sign-in should find
    // it intact.
    // So the next session opens with its own connect banner.
    bootLoggedRef.current = false;
    setScreen(null);
    setActiveTab("overview");
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

  /**
   * One fact about the deployment rather than about the data: whether a Celery
   * worker is up, which the pipeline screen reports. Read once per session — it
   * does not change while the tab is open, so putting it on the five-second
   * poll would be a wasted request a tick.
   */
  useEffect(() => {
    if (!user) return;
    let active = true;

    fetchWorkerStatus().then(
      (status) => active && setWorkersOnline(status.available),
      () => active && setWorkersOnline(null),
    );

    return () => {
      active = false;
    };
  }, [user]);

  const handleNavigate = useCallback(
    (next: NavId) => {
      setActiveTab(next);
      // Leaving for another destination closes whichever candidate screen was
      // open, so coming back lands on the list rather than mid-edit.
      setScreen(null);
      if (next === "candidates") void refreshCandidates();
    },
    [refreshCandidates],
  );

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
      setLastSummary(summary);
      setLastSyncAt(new Date().toISOString());

      log(`Step 3: Fetched ${summary.fetched} email(s); reading attachments...`, "info");
      const didWork = summary.ingested_candidates > 0 || summary.processed > 0;
      setFlow((prev) =>
        didWork
          ? { ...prev, filter: "success", connFilterVeris: true, veris: "active" }
          : { ...prev, filter: "success" },
      );

      // Report what the backend actually did, every outcome of it. The previous
      // version logged attachments only on a successful cycle and matched a
      // status ("failed") the API never sends, so a poll that failed on every
      // resume looked identical to a poll with nothing to do.
      let ignoredEmails = 0;
      let alreadyHandled = 0;
      for (const msgRes of summary.results ?? []) {
        if (msgRes.status === "suppressed") {
          log("[NOTICE] Email skipped — its candidate was deleted, so it is never re-ingested.", "warn");
          continue;
        }
        // A message the pipeline returned early on carries no attachment
        // results, and those early exits mean opposite things: an email with
        // nothing resume-like in it, versus one already ingested by an earlier
        // poll. Reporting both as "no resume attachment detected" made a
        // successful ingest look like the file had been rejected.
        // The backend always says *why* it returned early. Swallowing that
        // reason turned "your filename matched a blocklist word" into an
        // unexplained "no resume attachment detected", and a real candidate's
        // CV was dropped for days before anyone could see which rule did it.
        if (msgRes.attachments.length === 0) {
          if (msgRes.status === "error") {
            log(`[ERROR] Email '${msgRes.message_id}': ${msgRes.reason || "Processing error"}.`, "error");
          } else if ((msgRes.reason ?? "").startsWith("already processed")) {
            alreadyHandled += 1;
          } else {
            ignoredEmails += 1;
            log(`[NOTICE] Email ignored — ${msgRes.reason || "no reason given"}.`, "warn");
          }
          continue;
        }
        for (const att of msgRes.attachments) {
          const why = att.detail ? ` — ${att.detail}` : "";
          switch (att.status) {
            case "ingested":
              log(`[SUCCESS] '${att.filename}': parsed and saved to MongoDB Atlas${why}.`, "success");
              break;
            case "duplicate":
              log(`[NOTICE] '${att.filename}': skipped${why || " — already ingested"}.`, "warn");
              break;
            case "suppressed":
              log(`[NOTICE] '${att.filename}': skipped — previously deleted by a user.`, "warn");
              break;
            case "not_resume":
              log(`[NOTICE] '${att.filename}': not usable as a resume${why}.`, "warn");
              break;
            case "error":
              log(`[ERROR] '${att.filename}': extraction failed${why}.`, "error");
              break;
            default:
              log(`[NOTICE] '${att.filename}': ${att.status}${why}.`, "warn");
          }
        }
      }
      if (ignoredEmails > 0) {
        log(`${ignoredEmails} email(s) ignored (reasons above).`, "info");
      }
      if (alreadyHandled > 0) {
        log(
          `${alreadyHandled} email(s) already ingested by an earlier poll — skipped, ` +
            "nothing was lost.",
          "info",
        );
      }

      if (didWork) {
        setFlow((prev) => ({ ...prev, veris: "success", connVerisDb: true, db: "active" }));
        log("Step 4: Writing structured Candidate Profile to MongoDB Atlas collection 'candidates'...", "info");
        await sleep(700);
        setFlow((prev) => ({ ...prev, db: "success" }));
      }

      // A cycle that declined the lock did no work at all. Saying "no new
      // resumes found" there would be a lie — nothing was even looked at.
      if (summary.skipped_reason) {
        log(`[NOTICE] ${summary.skipped_reason}`, "warn");
        showToast("A sync is already running. Its results will appear shortly.", "info");
      } else {
        const parts = [
          `Fetched=${summary.fetched}`,
          `Ingested=${summary.ingested_candidates}`,
          `Skipped=${summary.skipped}`,
        ];
        if (summary.suppressed) parts.push(`Deleted-and-ignored=${summary.suppressed}`);
        if (summary.errors) parts.push(`Errors=${summary.errors}`);
        log(
          `[COMPLETE] Pipeline finished. ${parts.join(", ")}.`,
          summary.errors ? "warn" : "success",
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
    // Read the name and the stored profile before the save lands — an edit can
    // rename the record, and the entry has to say what changed, which needs the
    // values as they were.
    const previousName = nameOf(candidateId);
    const previousProfile = candidates.find((c) => c.id === candidateId)?.profile;
    const nextName = profile.full_name?.trim() || previousName;
    const renamed = nextName !== previousName;

    try {
      await updateCandidateProfile(candidateId, profile);
      showToast("Candidate profile updated successfully.", "success");
      appendCandidateLog(
        candidateId,
        renamed ? "Renamed" : "Edited",
        previousProfile
          ? summariseProfileChange(previousProfile, profile)
          : `Profile saved for ${nextName}.`,
        "success",
        user?.email,
      );
      await refreshCandidates();
      // Straight to the executive view of what was just saved, so the edit is
      // confirmed by the record itself rather than by a toast alone.
      setScreen({ mode: "profile", candidateId });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error saving profile.";
      showToast("Failed to save profile changes.", "error");
      appendCandidateLog(candidateId, "Failed", `Save failed — ${message}`, "error", user?.email);
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
      appendCandidateLog(candidateId, "Verified", `Profile marked as verified.`, "success", user?.email);
      // The open screen stays open: it reads from the refreshed list, so the
      // status badge simply flips to Verified in place.
      await refreshCandidates();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error verifying candidate.";
      showToast("Failed to verify candidate.", "error");
      appendCandidateLog(
        candidateId,
        "Failed",
        `Verification failed for ${who} — ${message}`,
        "error",
        user?.email,
      );
    } finally {
      setVerifying(false);
    }
  };

  const handleDeleteCandidate = async (candidateId: string) => {
    if (deletingRef.current.has(candidateId)) return;
    deletingRef.current.add(candidateId);
    // Resolved up front: once the record is gone the name cannot be looked up.
    const who = nameOf(candidateId);
    try {
      await deleteCandidateAPI(candidateId);
      showToast("Candidate permanently deleted from MongoDB Atlas.", "success");
      dropCandidateLogs(candidateId);
      setScreen(null);
      const remaining = await refreshCandidates();
      // The deletion is reported on the dashboard trace, not in the candidate's
      // own history, which has just been dropped along with the record. It also
      // changes the count the trace opened with ("… N candidate record(s)
      // loaded"), so without this the trace shows a stale total and never says
      // why it changed.
      log(
        remaining
          ? `Deleted profile: ${who} — ${remaining.length} candidate record(s) remaining.`
          : `Deleted profile: ${who} — removed from MongoDB Atlas.`,
        "warn",
      );
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error deleting candidate.";
      showToast("Failed to delete candidate.", "error");
      // The record survived, so its own history is still reachable and is where
      // the reason belongs — and on the dashboard trace, where the user is
      // watching the count that did not change.
      appendCandidateLog(candidateId, "Failed", `Delete failed — ${message}`, "error", user?.email);
      log(`Failed to delete ${who}: ${message}`, "error");
    } finally {
      deletingRef.current.delete(candidateId);
    }
  };

  // ---- candidate screens -------------------------------------------------- //
  /**
   * The directory is the home of every candidate screen, including the ones
   * opened from the dashboard's recent list — otherwise "Back to candidates"
   * would land somewhere that is not the candidates list.
   */
  const openScreen = (mode: CandidateScreen["mode"], candidateId: string) => {
    setActiveTab("candidates");
    setScreen({ mode, candidateId });
  };

  const handleOpenCandidate = (candidate: CandidateRecord) => {
    openScreen("profile", candidate.id);
    appendCandidateLog(candidate.id, "Viewed", `Executive profile opened.`, "info", user?.email);
  };

  const handleEditCandidate = (candidate: CandidateRecord) => {
    openScreen("edit", candidate.id);
    appendCandidateLog(candidate.id, "Opened editor", `Edit screen opened.`, "info", user?.email);
  };

  /** Reading the history is not itself an event in it. */
  const handleOpenLogs = (candidate: CandidateRecord) => {
    openScreen("logs", candidate.id);
  };

  const closeScreen = () => setScreen(null);

  /**
   * Resolved from the live list, not from what was captured on click — a record
   * deleted or replaced by the poll simply stops resolving, and the directory
   * takes the column back rather than a stale copy staying on screen.
   */
  const screenCandidate = screen
    ? candidates.find((candidate) => candidate.id === screen.candidateId) ?? null
    : null;

  const meta =
    screenCandidate && screen ? CANDIDATE_SCREEN_META[screen.mode] : NAV_META[activeTab];

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
    <div className={`app-shell ${railCollapsed ? "is-collapsed" : ""}`}>
      <TopBar
        user={user}
        syncing={syncing}
        onSync={runPipeline}
        onOpenActivity={() => handleNavigate("activity")}
        onOpenSettings={() => handleNavigate("settings")}
        onToggleRail={() => setMobileOpen((open) => !open)}
      />

      <div className="app-body">
        <Sidebar
          activeId={activeTab}
          collapsed={railCollapsed}
          mobileOpen={mobileOpen}
          user={user}
          onNavigate={handleNavigate}
          onToggleCollapse={() => setRailCollapsed((open) => !open)}
          onCloseMobile={() => setMobileOpen(false)}
          onSignOut={handleSignOut}
        />

        <main className="workspace">
          <div className="db-page">
            <header className="db-page-head">
              <div>
                <span className="db-eyebrow">{meta.eyebrow}</span>
                <h1 className="db-title">{meta.title}</h1>
                <p className="db-subtitle">{meta.subtitle}</p>
              </div>
            </header>

            {/* A candidate screen owns the whole workspace while it is open.
                Nothing is layered over anything else — the profile, the editor
                and the activity log are three destinations, not three modes of
                one. */}
            {screenCandidate && screen?.mode === "profile" && (
              <CandidateProfileScreen
                candidate={screenCandidate}
                verifying={verifying}
                onBack={closeScreen}
                onVerify={handleVerify}
              />
            )}

            {screenCandidate && screen?.mode === "edit" && (
              <CandidateEditScreen
                candidate={screenCandidate}
                saving={saving}
                onBack={closeScreen}
                onSave={handleSave}
              />
            )}

            {screenCandidate && screen?.mode === "logs" && (
              <CandidateLogsScreen
                candidate={screenCandidate}
                logs={candidateLogs}
                onBack={closeScreen}
              />
            )}

            {!screenCandidate && (
              <>
                {activeTab === "overview" && (
                  <OverviewScreen
                    total={total}
                    candidates={candidates}
                    logs={logs}
                    onNavigate={handleNavigate}
                    onOpenCandidate={handleOpenCandidate}
                  />
                )}

                {activeTab === "resume-parser" && (
                  <ResumeParserScreen
                    candidates={candidates}
                    syncing={syncing}
                    onSync={runPipeline}
                    onNavigate={handleNavigate}
                  />
                )}

                {activeTab === "visualizer" && (
                  <PipelineScreen
                    flow={flow}
                    logs={logs}
                    syncing={syncing}
                    lastSyncAt={lastSyncAt}
                    lastSummary={lastSummary}
                    workersOnline={workersOnline}
                    onSync={runPipeline}
                  />
                )}

                {activeTab === "sourcing" && <SourcingHub onActivity={log} />}

                {activeTab === "candidates" && (
                  <CandidatesView
                    candidates={candidates}
                    logCounts={logCounts}
                    onOpenCandidate={handleOpenCandidate}
                    onEditCandidate={handleEditCandidate}
                    onOpenLogs={handleOpenLogs}
                    onDeleteCandidate={handleDeleteCandidate}
                  />
                )}

                {activeTab === "job-orders" && (
                  <JobOrders candidates={candidates} onActivity={log} />
                )}

                {activeTab === "activity" && (
                  <ActivityLogsScreen
                    systemLogs={logs}
                    candidateLogs={candidateLogs}
                    candidates={candidates}
                    onOpenCandidateLogs={handleOpenLogs}
                  />
                )}

                {activeTab === "settings" && (
                  <SettingsScreen user={user} onSignOut={handleSignOut} />
                )}
              </>
            )}
          </div>
        </main>
      </div>

      <Toast toast={toast} />
    </div>
  );
}
