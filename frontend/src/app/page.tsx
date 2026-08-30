"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname } from "next/navigation";

import Sidebar from "@/components/Sidebar";
import TopBar from "@/components/TopBar";
import OverviewScreen from "@/screens/OverviewScreen";
import SourcingHub from "@/screens/SourcingHub";
import B2BEnquiries from "@/screens/B2BEnquiries";
import DataManagementScreen from "@/screens/DataManagementScreen";
import UserManagementScreen from "@/screens/UserManagementScreen";
import CandidatesView from "@/screens/CandidatesView";
import JobOrders from "@/screens/JobOrders";
import SettingsScreen from "@/screens/SettingsScreen";
import CandidateProfileScreen from "@/screens/CandidateProfileScreen";
import CandidateEditScreen from "@/screens/CandidateEditScreen";
import CandidateUploadScreen, { type CandidateUploadFiles } from "@/screens/CandidateUploadScreen";
import LoginScreen from "@/screens/LoginScreen";
import AdminStaffManagement from "@/screens/AdminStaffManagement";
import StaffDashboard from "@/screens/StaffDashboard";
import Toast, { type ToastState, type ToastType } from "@/components/Toast";
import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { candidateNameOf } from "@/lib/format";
import { summariseProfileChange } from "@/lib/candidateProfile";
import { useRealtime, type RealtimeEvent } from "@/lib/realtime";
import {
  NAV_META,
  defaultNavFor,
  navGroupsFor,
  navIdFromPath,
  navPath,
  type NavId,
} from "@/lib/nav";
import {
  appendCandidateLog,
  dropCandidateLogs,
} from "@/lib/candidateLog";
import {
  uploadCandidateDocuments,
  evaluateCandidate,
  fetchMe,
  getCandidate,
  getToken,
  logout as clearSession,
  deleteCandidateAPI,
  listCandidates,
  markCandidateViewed,
  runPollCycle,
  updateCandidateProfile,
  verifyCandidate,
  type AuthUser,
  type CandidateProfile,
  type CandidateRecord,
  type SlaAlert,
} from "@/lib/api";
import type { Verdict } from "@/screens/CandidateProfileScreen";

/**
 * A candidate-scoped screen that takes over the content column.
 *
 * These are screens, not overlays, and each one does exactly one job: read the
 * profile, change the profile, or read the profile's history. Keeping them
 * apart is what lets the executive view carry no edit control at all and the
 * editor carry nothing but controls.
 *
 * The candidate is held by id rather than by record: the list holds projections
 * with the heavy fields left out, so opening one of these fetches the whole
 * record for that candidate alone.
 */
interface CandidateScreen {
  mode: "profile" | "edit";
  candidateId: string;
}

interface CandidateExtractionState {
  status: "extracting" | "complete" | "error";
  filename: string;
  title: string;
  detail: string;
  candidateId?: string;
}

/**
 * How often the directory refreshes itself with nothing else going on.
 *
 * This is a backstop for a tab left open, not a live feed. It used to be five
 * seconds, which meant a page of 200 candidate documents out of Atlas twelve
 * times a minute per open tab, forever — the API's single heaviest load, and
 * almost all of it re-fetching rows nobody was looking at. Everything that
 * actually changes the data — a sync, a save, a verify, a delete — refreshes
 * the list itself, so this only has to catch what another user did.
 */
const BACKGROUND_REFRESH_MS = 45000;

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
};

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

const logEntry = (message: string, type: LogEntry["type"] = "info"): LogEntry => ({
  time: new Date().toLocaleTimeString(),
  type,
  message,
});

export default function Home() {
  const pathname = usePathname();
  // null = signed out. `checking` covers the first paint, where a stored token
  // exists but has not been validated yet — without it the login screen would
  // flash on every refresh for an already-signed-in user.
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checking, setChecking] = useState(true);

  const [railCollapsed, setRailCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);

  // Bumped whenever a push event says the allocation data moved, so the staff
  // and admin screens reload without polling on a timer.
  const [realtimeNonce, setRealtimeNonce] = useState(0);
  // The admin's SLA modal. Held here rather than in the staff screen because it
  // has to appear over whatever the admin is currently looking at.
  const [slaPopup, setSlaPopup] = useState<{ message: string; alerts: SlaAlert[] } | null>(null);
  // Candidates that arrived over the socket during this session, so the queue
  // can still mark them "new" long after the toast has faded.
  const [arrivedIds, setArrivedIds] = useState<Set<string>>(() => new Set());
  // A profile the bell asked the staff workspace to open.
  const [queueFocusId, setQueueFocusId] = useState<string | null>(null);
  // Set when "Add staff" sends the admin to User Management, so that screen
  // opens on its create form rather than on the accounts matrix.
  const [usersOpenCreate, setUsersOpenCreate] = useState(false);

  const [candidates, setCandidates] = useState<CandidateRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [toast, setToast] = useState<ToastState | null>(null);

  const [screen, setScreen] = useState<CandidateScreen | null>(null);
  // The whole record for whichever candidate a screen is open on. Fetched per
  // candidate because the list no longer carries the full profile — and it has
  // to be the whole record before the editor may open, or a save would write
  // back the projection and erase every field the list left out.
  const [detail, setDetail] = useState<CandidateRecord | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailNonce, setDetailNonce] = useState(0);
  const [saving, setSaving] = useState(false);
  const [creationError, setCreationError] = useState<string | null>(null);
  const [candidateExtraction, setCandidateExtraction] = useState<CandidateExtractionState | null>(null);
  const [candidateEntryReset, setCandidateEntryReset] = useState(0);
  // Identifies the upload owned by the current signed-in session. Signing out
  // invalidates the id so a slow VeriIS response cannot notify the next user.
  const candidateExtractionRunRef = useRef(0);
  const [verifying, setVerifying] = useState(false);
  /** A staff verdict in flight, which locks the review screen's own buttons. */
  const [evaluating, setEvaluating] = useState(false);
  // Ids with a DELETE in flight — a second click must not fire a second request.
  const deletingRef = useRef<Set<string>>(new Set());

  const [syncing, setSyncing] = useState(false);
  // Reported by the Overview and Gmail Sync screens. Null means "not this
  // session" rather than "never" — the backend keeps no run history.
  const syncingRef = useRef(false);
  const bootLoggedRef = useRef(false);
  // When the list was last known to be current, so returning to the tab can
  // refresh a stale one without re-fetching a list that is seconds old.
  const lastRefreshRef = useRef(0);

  /** The only destinations this account is allowed to discover or open. */
  const reachableTabs = useMemo(
    () =>
      user
        ? new Set(
            navGroupsFor(user.role, user.pages).flatMap((group) =>
              group.items.map((item) => item.id),
            ),
          )
        : null,
    [user],
  );
  const canReadCandidates = reachableTabs?.has("candidates") ?? false;

  // The path is the page state. Native history updates keep this large client
  // workspace mounted, while Back/Forward changes `pathname` and therefore the
  // rendered destination without maintaining a second copy in React state.
  const routeTab = navIdFromPath(pathname) ?? "overview";

  useEffect(() => {
    const closeTransientScreen = () => {
      setScreen(null);
      setUsersOpenCreate(false);
    };
    window.addEventListener("popstate", closeTransientScreen);
    return () => window.removeEventListener("popstate", closeTransientScreen);
  }, []);

  // A bookmarked route may be valid application-wide but unavailable to the
  // signed-in account. Render the role's safe landing page and correct the URL
  // without leaving the forbidden destination in browser history.
  useEffect(() => {
    if (!user || !reachableTabs) return;
    if (reachableTabs.has(routeTab)) return;
    const fallback = defaultNavFor(user.role, user.pages);
    window.history.replaceState(null, "", navPath(fallback));
  }, [reachableTabs, routeTab, user]);

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
  const showToast = useCallback((message: string, type: ToastType = "info") => {
    setToast({ message, type, key: Date.now() });
  }, []);

  /**
   * Record a user action in the dashboard trace and acknowledge it immediately.
   * Screens that already describe their create, update, delete and failure
   * outcomes through `onActivity` can therefore share the same popup feedback
   * without maintaining a second set of messages. A warning is an expected
   * destructive outcome (for example, "job order deleted"), not a failed
   * operation, so it uses the neutral toast treatment.
   */
  const announceActivity = useCallback(
    (message: string, type: LogEntry["type"] = "info") => {
      log(message, type);
      showToast(message, type === "warn" ? "info" : type);
    },
    [log, showToast],
  );

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
      lastRefreshRef.current = Date.now();
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

  // Protected deep links never leave a signed-out visitor sitting on a CRM
  // URL. Collapse them to the single login address; successful authentication
  // then sends the account to its own permitted landing page.
  useEffect(() => {
    if (checking || user || pathname === "/") return;
    window.history.replaceState(null, "", "/");
  }, [checking, pathname, user]);

  const handleSignOut = useCallback(() => {
    candidateExtractionRunRef.current += 1;
    clearSession();
    setUser(null);
    setCandidates([]);
    setTotal(0);
    setLogs([]);
    setCandidateExtraction(null);
    setCreationError(null);
    // Candidate history is deliberately NOT cleared: it is the record's audit
    // trail, not this session's scratch state, and the next sign-in should find
    // it intact.
    // So the next session opens with its own connect banner.
    bootLoggedRef.current = false;
    setScreen(null);
    window.history.replaceState(null, "", "/");
  }, []);

  // ---- bootstrap -------------------------------------------------------- //
  useEffect(() => {
    if (!user || !canReadCandidates) return;
    let active = true;

    listCandidates().then(
      (data) => {
        if (!active) return;
        lastRefreshRef.current = Date.now();
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

    return () => {
      active = false;
    };
  }, [canReadCandidates, user]);

  /**
   * The backstop refresh: slow, and only when someone is actually looking.
   *
   * A hidden tab is refreshed for nobody, so it is skipped entirely and caught
   * up the moment it comes back — but only if what it is holding has gone stale,
   * so flicking between tabs does not fire a request each time. A sync is
   * skipped too: it refreshes the list itself when it finishes, with a summary
   * of what it ingested.
   */
  useEffect(() => {
    if (!user || !canReadCandidates) return;

    const refreshIfIdle = () => {
      if (syncingRef.current) return;
      void refreshCandidates();
    };

    const interval = setInterval(() => {
      if (document.hidden) return;
      refreshIfIdle();
    }, BACKGROUND_REFRESH_MS);

    const onVisible = () => {
      if (document.hidden) return;
      if (Date.now() - lastRefreshRef.current < BACKGROUND_REFRESH_MS) return;
      refreshIfIdle();
    };
    document.addEventListener("visibilitychange", onVisible);

    return () => {
      clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [canReadCandidates, refreshCandidates, user]);

  /**
   * The destination actually shown, which is not always the one in state.
   *
   * A staff member signing in — or restoring a session last left on Overview —
   * holds a tab their rail no longer offers and the API refuses. Correcting it
   * is a derivation, not an effect: computing it here renders the right screen
   * on the first pass, where writing it back would render the wrong one first
   * and then correct itself a frame later.
   */
  const currentTab = useMemo(() => {
    if (!user || !reachableTabs) return routeTab;
    return reachableTabs.has(routeTab) ? routeTab : defaultNavFor(user.role, user.pages);
  }, [user, reachableTabs, routeTab]);

  /**
   * Whether this session gets a navigation rail.
   *
   * Derived from the rail's own contents rather than from the role, so a rail
   * can never appear with one item in it — if the destinations ever change,
   * this follows automatically.
   */
  const hasRail = useMemo(() => {
    const destinations = navGroupsFor(user?.role, user?.pages).flatMap((group) => group.items);
    return destinations.length >= 1;
  }, [user]);

  /**
   * Push events from `/ws`.
   *
   * The server has already decided what this user is allowed to be told — a
   * staff member only receives their own allocations, an admin only the SLA
   * alerts — so this switches on the event and does not re-check the role.
   *
   * An assignment refreshes the list because the new profile is not in it yet;
   * an SLA alert does not, because nothing about the candidate data changed.
   */
  const handleRealtime = useCallback(
    (event: RealtimeEvent) => {
      switch (event.type) {
        case "candidate_assigned": {
          showToast(event.message, "info");
          setRealtimeNonce((n) => n + 1);
          // Mark it as new so the queue can point at what just arrived; the
          // toast is gone in four seconds and the row has to still say so.
          setArrivedIds((current) => {
            const next = new Set(current);
            if (event.candidate?.id) next.add(event.candidate.id);
            return next;
          });
          if (canReadCandidates) void refreshCandidates();
          break;
        }
        // The admin's copy of the same moment. Without this, the person who
        // pressed Sync watched a poll run and was told nothing at all.
        case "candidate_ingested": {
          showToast(event.message, "success");
          setRealtimeNonce((n) => n + 1);
          if (canReadCandidates) void refreshCandidates();
          break;
        }
        case "sla_alert": {
          setSlaPopup({
            message: `${event.count} profile${event.count === 1 ? " is" : "s are"} beyond the ${
              event.threshold_hours
            }-hour review window.`,
            alerts: event.alerts,
          });
          setRealtimeNonce((n) => n + 1);
          break;
        }
        default:
          break;
      }
    },
    [canReadCandidates, refreshCandidates, showToast],
  );

  const { status: realtimeStatus } = useRealtime(Boolean(user), handleRealtime);

  // ---- the open candidate ------------------------------------------------ //
  const openCandidateId = screen?.candidateId ?? null;

  /**
   * Fetch the whole record for whichever candidate is open.
   *
   * Re-runs on `detailNonce`, which a save or a verify bumps: the id has not
   * changed, but what is stored under it has.
   */
  // Clearing the last screen's error — and the record itself when nothing is
  // open — is an adjustment to the id changing, not work to do afterwards.
  // Done from the effect it cost a committed render still showing the previous
  // state; done here the corrected values are what gets painted. The two
  // clearings differ deliberately: closing a candidate empties the record,
  // while opening a different one keeps the last one on screen until the new
  // record arrives, which is what it did before.
  const [detailFor, setDetailFor] = useState({ id: openCandidateId, nonce: detailNonce });
  if (detailFor.id !== openCandidateId || detailFor.nonce !== detailNonce) {
    setDetailFor({ id: openCandidateId, nonce: detailNonce });
    setDetailError(null);
    if (!openCandidateId) setDetail(null);
  }

  useEffect(() => {
    if (!openCandidateId) return;

    let active = true;

    getCandidate(openCandidateId).then(
      (record) => active && setDetail(record),
      (err: unknown) =>
        active &&
        setDetailError(
          err instanceof Error ? err.message : "Could not load this candidate.",
        ),
    );

    return () => {
      active = false;
    };
  }, [openCandidateId, detailNonce]);

  const handleNavigate = useCallback(
    (next: NavId) => {
      // Internal dashboard cards may suggest another destination. Treat them
      // exactly like the rail: if it was not granted, the click learns nothing
      // and opens nothing.
      if (!reachableTabs?.has(next)) return;
      const nextPath = navPath(next);
      if (pathname !== nextPath) window.history.pushState(null, "", nextPath);
      // Leaving for another destination closes whichever candidate screen was
      // open, so coming back lands on the list rather than mid-edit.
      setScreen(null);
      // Any ordinary navigation clears the create-user request below, so only
      // the click that made it opens User Management on its form.
      setUsersOpenCreate(false);
      if (next === "candidates") void refreshCandidates();
    },
    [pathname, reachableTabs, refreshCandidates],
  );

  /**
   * "Add staff" on the staff console.
   *
   * Creating a person is account work, so it happens where accounts live: this
   * carries the intent over to User Management and opens its create form there,
   * rather than duplicating a second, thinner create path on the staff screen.
   */
  const handleCreateStaff = useCallback(() => {
    handleNavigate("users");
    setUsersOpenCreate(true);
  }, [handleNavigate]);

  // ---- pipeline run ----------------------------------------------------- //
  const runPipeline = useCallback(async () => {
    if (syncingRef.current) return;
    syncingRef.current = true;
    setSyncing(true);

    log("Step 1: Connecting to Gmail API & searching for unread candidate emails...", "info");
    await sleep(600);

    try {
      log("Step 2: Applying resume detection filter & score validation...", "info");
      await sleep(600);

      const summary = await runPollCycle();

      log(`Step 3: Fetched ${summary.fetched} email(s); reading attachments...`, "info");
      const didWork = summary.ingested_candidates > 0 || summary.processed > 0;

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
          if ((msgRes.reason ?? "").startsWith("already processed")) alreadyHandled += 1;
          else {
            ignoredEmails += 1;
            // log(`[NOTICE] Email ignored — ${msgRes.reason || "no reason given"}.`, "warn");
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
              // log(`[NOTICE] '${att.filename}': skipped${why || " — already ingested"}.`, "warn");
              break;
            case "suppressed":
              // log(`[NOTICE] '${att.filename}': skipped — previously deleted by a user.`, "warn");
              break;
            case "not_resume":
              // log(`[NOTICE] '${att.filename}': not usable as a resume${why}.`, "warn");
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
        log("Step 4: Writing structured Candidate Profile to MongoDB Atlas collection 'candidates'...", "info");
        await sleep(700);
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
      const message = err instanceof Error ? err.message : "Unknown error";
      log(`[ERROR] Inbound pipeline execution failed: ${message}`, "error");
      showToast("Failed to poll Gmail inbox.", "error");
    }

    await sleep(4000);
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
      // confirmed by the record itself rather than by a toast alone — re-read
      // from the API, because what is stored under this id has just changed.
      setDetailNonce((n) => n + 1);
      setScreen({ mode: "profile", candidateId });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Connection error saving profile.";
      showToast("Failed to save profile changes.", "error");
      appendCandidateLog(candidateId, "Failed", `Save failed — ${message}`, "error", user?.email);
    } finally {
      setSaving(false);
    }
  };

  const handleCreateCandidate = async (files: CandidateUploadFiles) => {
    if (candidateExtraction?.status === "extracting") return;
    const runId = candidateExtractionRunRef.current + 1;
    const displayName = files.resume?.name || files.full_name || "Manual candidate";
    const hasDocuments = Boolean(files.resume || files.aadhaar?.length || files.passport?.length);
    candidateExtractionRunRef.current = runId;
    setCreationError(null);
    setCandidateExtraction({
      status: "extracting",
      filename: displayName,
      title: hasDocuments ? "VeriIS extracting" : "Creating candidate",
      detail: hasDocuments
        ? "The documents are queued or being extracted. You can continue working."
        : "The candidate is being added. You can continue working.",
    });
    showToast(hasDocuments ? "Candidate extraction started." : "Candidate creation started.", "info");
    try {
      const result = await uploadCandidateDocuments(files);
      if (candidateExtractionRunRef.current !== runId) return;
      const created = result.candidate;
      setCandidateExtraction({
        status: "complete",
        filename: displayName,
        title: `${candidateNameOf(created)} is ready`,
        detail: hasDocuments
          ? "Extraction finished. Open the completed candidate profile."
          : "Candidate created. Open the completed profile.",
        candidateId: created.id,
      });
      setCandidateEntryReset((nonce) => nonce + 1);
      setRealtimeNonce((nonce) => nonce + 1);
      void refreshCandidates();
      const creationDetail = result.processed.length
        ? `Candidate created from ${result.processed.join(", ")} using VeriIS.`
        : `Candidate created manually${files.job_title ? ` for ${files.job_title}` : ""}.`;
      appendCandidateLog(created.id, "Created", creationDetail, "success", user?.email);
      log(`Added candidate: ${candidateNameOf(created)}.`, "success");
      const identityNames = result.processed.filter((item) => item !== "resume");
      const identityCopy = identityNames.length
        ? ` ${identityNames.map((item) => item === "aadhaar" ? "Aadhaar" : "passport").join(" and ")} extracted.`
        : "";
      showToast(`${hasDocuments ? "Candidate extracted" : "Candidate created"} successfully.${identityCopy}`, "success");
    } catch (err) {
      if (candidateExtractionRunRef.current !== runId) return;
      const message = err instanceof Error ? err.message : "Could not add the candidate.";
      setCreationError(message);
      setCandidateExtraction({
        status: "error",
        filename: displayName,
        title: hasDocuments ? "Extraction needs attention" : "Candidate was not created",
        detail: message,
      });
      showToast(message, "error");
    }
  };

  const handleVerify = async (candidateId: string) => {
    setVerifying(true);
    const who = nameOf(candidateId);
    try {
      await verifyCandidate(candidateId);
      showToast("Candidate marked as verified.", "success");
      appendCandidateLog(candidateId, "Verified", `Profile marked as verified.`, "success", user?.email);
      // The open screen stays open; re-reading the record is what flips the
      // status badge to Verified in place.
      setDetailNonce((n) => n + 1);
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
   * Where "Back to candidates" lands, which is not the same place for the two
   * roles: an admin's list of candidates is the directory, a staff member's is
   * their queue. Sending a reviewer back to a screen the API refuses them is
   * how the back button used to end up somewhere they could not be.
   */
  const listTab: NavId = "candidates";

  const openScreen = (mode: CandidateScreen["mode"], candidateId: string) => {
    const listPath = navPath(listTab);
    if (pathname !== listPath) window.history.pushState(null, "", listPath);
    setScreen({ mode, candidateId });
  };

  /**
   * Open a candidate's full screen — profile and, for staff, the verdict form.
   *
   * The single entry point for both roles and every route in: a queue row, the
   * eye icon, "Review next", a notification. A staff member's first open also
   * stops that profile's SLA clock, which is why the stamp lives here rather
   * than in the queue — this is the moment the résumé is actually on screen.
   */
  const handleOpenCandidate = (candidate: CandidateRecord) => {
    openScreen("profile", candidate.id);
    appendCandidateLog(candidate.id, "Viewed", `Executive profile opened.`, "info", user?.email);

    if (user?.role !== "staff") return;
    void markCandidateViewed(candidate.id)
      // A first view changes the queue's counts and its SLA colouring, so the
      // list behind the screen has to be re-read. A repeat open changes
      // nothing and is not worth a request's worth of refresh.
      .then((result) => result.first_view && void refreshCandidates())
      .catch(() => {
        // Losing the stamp costs an SLA row its stop-clock; it must not stop
        // the reviewer reading the profile that is already on screen.
      });
  };

  const handleEditCandidate = (candidate: CandidateRecord) => {
    openScreen("edit", candidate.id);
    appendCandidateLog(candidate.id, "Opened editor", `Edit screen opened.`, "info", user?.email);
  };

  const handleAddCandidate = () => {
    setCreationError(null);
    handleNavigate("candidate-entry");
  };

  const closeScreen = () => setScreen(null);

  /**
   * The profile a reviewer should see next: the unopened one closest to its
   * deadline, excluding whatever is on screen.
   *
   * Read from the same list the queue sorts, so "Save & next" and the queue's
   * own "Review next" always agree about what is most urgent.
   */
  const nextUnviewed = useMemo(() => {
    if (user?.role !== "staff") return null;
    const waiting = candidates.filter(
      (candidate) => !candidate.viewed_at && candidate.id !== screen?.candidateId,
    );
    if (waiting.length === 0) return null;
    // Oldest allocation first — that is the one nearest the SLA window.
    return waiting.reduce((oldest, candidate) => {
      const a = new Date(candidate.assigned_at ?? candidate.created_at).getTime();
      const b = new Date(oldest.assigned_at ?? oldest.created_at).getTime();
      return a < b ? candidate : oldest;
    });
  }, [candidates, screen?.candidateId, user?.role]);

  /**
   * Record a verdict from the review screen, and optionally move straight on.
   *
   * `advance` swaps the candidate under a mounted screen rather than closing
   * and reopening one, so a reviewer working a queue never watches the shell
   * unmount between profiles. When there is nothing left to advance to it
   * closes, which is the honest end of the run.
   */
  const handleSaveEvaluation = async (
    candidateId: string,
    verdict: Verdict,
    advance: boolean,
  ) => {
    setEvaluating(true);
    // Read before the save: `nextUnviewed` is derived from a list this save is
    // about to change, and after the refresh the profile just judged is no
    // longer unviewed — so the "next" computed afterwards could be a different
    // one from the button the reviewer actually pressed.
    const following = advance ? nextUnviewed : null;
    try {
      await evaluateCandidate(candidateId, {
        status: verdict.status,
        score: verdict.score,
        notes: verdict.notes,
      });
      appendCandidateLog(
        candidateId,
        "Evaluated",
        `Recorded as ${verdict.status}${verdict.score ? ` · ${verdict.score} of 5` : ""}.`,
        "success",
        user?.email,
      );
      await refreshCandidates();

      if (following) {
        showToast(`Saved. Opening ${candidateNameOf(following)}.`, "success");
        handleOpenCandidate(following);
      } else {
        showToast(advance ? "Saved — nothing else is waiting." : "Evaluation saved.", "success");
        if (advance) closeScreen();
        else setDetailNonce((n) => n + 1);
      }
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not save the evaluation.";
      showToast(message, "error");
      appendCandidateLog(candidateId, "Failed", `Evaluation failed — ${message}`, "error", user?.email);
    } finally {
      setEvaluating(false);
    }
  };

  /**
   * Open whatever a notification points at.
   *
   * The two roles reach a candidate by different routes — an admin through the
   * directory's profile screen, a staff member through the evaluation drawer in
   * their queue — so the bell hands over an id and this decides.
   */
  const handleOpenCandidateById = (candidateId: string) => {
    if (!reachableTabs?.has("candidates")) return;
    if (user?.role === "staff") {
      handleNavigate("candidates");
      setQueueFocusId(candidateId);
      return;
    }
    openScreen("profile", candidateId);
  };

  /**
   * The record the open screen is showing, and only once it is the *whole*
   * record for that candidate — a detail still in flight, or one left over from
   * the previously open candidate, resolves to null and the screen waits.
   */
  const screenCandidate =
    screen && detail && detail.id === screen.candidateId
      ? detail
      : null;

  const meta = screen ? CANDIDATE_SCREEN_META[screen.mode] : NAV_META[currentTab];

  /** A staff member reading a candidate: the one screen that gets no page head. */
  const isStaffReview = user?.role === "staff" && screen?.mode === "profile";

  // Screens that draw their own title row. The dashboard's header carries the
  // window it is showing and the controls that change it, and the candidates
  // pool carries its own title, subtitle and the date range, add and export
  // controls — so for both, the generic eyebrow/title/subtitle block above
  // would be a second heading for the same page, and the one without the
  // controls.
  const ownsItsHeader =
    !screen &&
    ["overview", "candidates", "candidate-entry", "staff", "users", "data-management"].includes(
      currentTab,
    );

  if (checking && pathname !== "/") {
    return (
      <div className="app-boot">
        <span className="app-boot-spinner" />
      </div>
    );
  }

  // `/` is always the sign-in screen, even when a previous session token is
  // still valid. The CRM itself begins at `/overview` after authentication.
  if (pathname === "/" || !user) {
    return (
      <LoginScreen
        onSuccess={(u) => {
          setUser(u);
          const landing = defaultNavFor(u.role, u.pages);
          window.history.replaceState(null, "", navPath(landing));
        }}
      />
    );
  }

  return (
    <div
      className={`app-shell ${railCollapsed ? "is-collapsed" : ""} ${hasRail ? "" : "is-railless"}`}
    >
      <TopBar
        user={user}
        syncing={syncing}
        realtime={realtimeStatus}
        realtimeNonce={realtimeNonce}
        hasRail={hasRail}
        onOpenCandidate={canReadCandidates ? handleOpenCandidateById : undefined}
        candidateExtraction={candidateExtraction}
        onOpenCandidateExtraction={
          candidateExtraction?.status === "complete" && candidateExtraction.candidateId
            ? () => handleOpenCandidateById(candidateExtraction.candidateId as string)
            : candidateExtraction?.status === "error"
              ? () => handleNavigate("candidate-entry")
              : undefined
        }
        onDismissCandidateExtraction={() => setCandidateExtraction(null)}
        onSync={runPipeline}
        onToggleRail={() => setMobileOpen((open) => !open)}
      />

      <div className="app-body">
        {/* Accounts with more than one reachable destination keep the rail;
            a tightly restricted account can still use the compact top bar. */}
        {hasRail && (
          <Sidebar
            activeId={currentTab}
            collapsed={railCollapsed}
            mobileOpen={mobileOpen}
            user={user}
            onNavigate={handleNavigate}
            onToggleCollapse={() => setRailCollapsed((open) => !open)}
            onCloseMobile={() => setMobileOpen(false)}
            onSignOut={handleSignOut}
          />
        )}

        <main className="workspace">
          <div className="db-page">
            {/* Every destination opens the same way, the staff workspace
                included. It used to be excepted here on the grounds that it
                carried its own header; it does not, so the screen began at a
                row of buttons with nothing naming it.

                The one exception is the reviewer's own candidate screen. That
                screen is a candidate — their name, their photo-sized monogram,
                their résumé — and a page title above it saying "Executive
                Profile" was a second heading competing with the one that
                matters. The chrome there is the bar's pulse and bell, and
                nothing else. */}
            {!isStaffReview && !ownsItsHeader && (
              <header className="db-page-head">
                <div>
                  <span className="db-eyebrow">{meta.eyebrow}</span>
                  <h1 className="db-title">{meta.title}</h1>
                  <p className="db-subtitle">{meta.subtitle}</p>
                </div>
              </header>
            )}

            {/* A candidate screen owns the whole workspace while it is open.
                Nothing is layered over anything else — the profile, the editor
                and the activity log are three destinations, not three modes of
                one. */}
            {/* One screen, two uses. An admin gets the read-only executive
                profile with a Verify control; a staff member gets the same
                profile with the verdict suite live beside it. The evidence and
                the judgement are never in two places. */}
            {screenCandidate && screen?.mode === "profile" && (
              <CandidateProfileScreen
                // Identity, not just data: "Save & next" swaps the candidate
                // without unmounting the screen, and the verdict controls hold
                // draft state seeded from the record. Keying on the id makes a
                // different candidate a different component, so no reviewer
                // ever sees the previous one's notes in the box.
                key={screenCandidate.id}
                candidate={screenCandidate}
                verifying={verifying}
                onBack={closeScreen}
                onVerify={user?.role === "admin" ? handleVerify : undefined}
                evaluation={
                  user?.role === "staff"
                    ? {
                        saving: evaluating,
                        nextName: nextUnviewed ? candidateNameOf(nextUnviewed) : null,
                        onSave: (verdict, advance) =>
                          void handleSaveEvaluation(screenCandidate.id, verdict, advance),
                      }
                    : undefined
                }
              />
            )}

            {screenCandidate && screen?.mode === "edit" && (
              <CandidateEditScreen
                candidate={screenCandidate}
                saving={saving}
                verifying={verifying}
                onBack={closeScreen}
                onSave={handleSave}
                onVerify={user?.role === "admin" ? handleVerify : undefined}
              />
            )}

            {/* The record is fetched when the screen opens, so there is a
                moment with nothing to show — and, if it cannot be fetched, no
                screen to show at all. Neither may fall through to the
                directory: that would read as "the candidate is gone". */}
            {screen && !screenCandidate && (
              <section className="db-card">
                {detailError ? (
                  <>
                    <h3 className="db-card-title">Could not open this candidate</h3>
                    <p className="db-card-sub">{detailError}</p>
                    <button type="button" className="db-btn" onClick={closeScreen}>
                      Back to candidates
                    </button>
                  </>
                ) : (
                  <span className="app-boot-spinner" />
                )}
              </section>
            )}

            {!screen && (
              <>
                {currentTab === "overview" && (
                  <OverviewScreen
                    total={total}
                    candidates={candidates}
                    logs={logs}
                    onNavigate={handleNavigate}
                    onOpenCandidate={handleOpenCandidate}
                  />
                )}

                {currentTab === "sourcing" && <SourcingHub onActivity={announceActivity} />}

                {currentTab === "b2b-enquiries" && <B2BEnquiries onActivity={announceActivity} />}

                {currentTab === "data-management" && <DataManagementScreen onActivity={announceActivity} />}

                {currentTab === "candidate-entry" && (
                  <CandidateUploadScreen
                    key={candidateEntryReset}
                    saving={candidateExtraction?.status === "extracting"}
                    error={creationError}
                    onSubmit={(files) => void handleCreateCandidate(files)}
                  />
                )}

                {currentTab === "users" && (
                  <UserManagementScreen
                    onActivity={announceActivity}
                    currentUserId={user?.id}
                    openCreate={usersOpenCreate}
                  />
                )}

                {currentTab === "candidates" && (
                  user?.role === "staff" ? (
                    <StaffDashboard
                      candidates={candidates}
                      arrivedIds={arrivedIds}
                      focusCandidateId={queueFocusId}
                      onFocusHandled={() => setQueueFocusId(null)}
                      onToast={showToast}
                      onOpenCandidate={handleOpenCandidate}
                    />
                  ) : (
                    <CandidatesView
                      candidates={candidates}
                      onAddCandidate={handleAddCandidate}
                      onOpenCandidate={handleOpenCandidate}
                      onEditCandidate={handleEditCandidate}
                      onDeleteCandidate={handleDeleteCandidate}
                      onAssignmentChanged={() => void refreshCandidates()}
                      onToast={showToast}
                    />
                  )
                )}

                {currentTab === "staff" && (
                  <AdminStaffManagement
                    candidates={candidates}
                    refreshNonce={realtimeNonce}
                    onToast={showToast}
                    onCandidatesChanged={() => void refreshCandidates()}
                    onOpenCandidate={handleOpenCandidate}
                    onCreateStaff={
                      reachableTabs?.has("users") ? handleCreateStaff : undefined
                    }
                  />
                )}

                {currentTab === "job-orders" && (
                  <JobOrders candidates={candidates} onActivity={announceActivity} />
                )}

                {currentTab === "settings" && (
                  <SettingsScreen user={user} onSignOut={handleSignOut} />
                )}
              </>
            )}
          </div>
        </main>
      </div>

      {/* The SLA alert is a modal rather than a toast on purpose: it reports
          that someone has stopped working on something, which is not a message
          that should be allowed to time out unread. */}
      {slaPopup && (
        <div className="modal-overlay active" onClick={() => setSlaPopup(null)}>
          <div
            className="modal-container is-narrow"
            role="alertdialog"
            aria-modal="true"
            aria-label="Overdue reviews"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="modal-header">
              <div>
                <h3 className="modal-title">Profiles past the review window</h3>
                <p className="modal-subtitle">{slaPopup.message}</p>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setSlaPopup(null)}
                aria-label="Dismiss"
              >
                ×
              </button>
            </div>
            <div className="modal-body">
              <div className="sla-list">
                {slaPopup.alerts.slice(0, 8).map((alert) => (
                  <div key={alert.candidate_id} className="sla-row">
                    <span className="sla-hours">{Math.round(alert.hours_overdue)}h</span>
                    <div className="sla-body">
                      <span className="sla-name">{alert.full_name ?? alert.candidate_name}</span>
                      <span className="sla-meta">
                        {alert.assigned_staff_name} ·{" "}
                        {alert.reason === "unviewed" ? "never opened" : "opened, not evaluated"}
                      </span>
                    </div>
                  </div>
                ))}
                {slaPopup.alerts.length > 8 && (
                  <p className="db-card-sub">and {slaPopup.alerts.length - 8} more.</p>
                )}
              </div>
            </div>
            <div className="modal-footer">
              <button
                type="button"
                className="modal-cancel-btn"
                onClick={() => setSlaPopup(null)}
              >
                Dismiss
              </button>
              <button
                type="button"
                className="modal-submit-btn"
                onClick={() => {
                  setSlaPopup(null);
                  handleNavigate("staff");
                }}
              >
                Review workload
              </button>
            </div>
          </div>
        </div>
      )}

      <Toast toast={toast} />
    </div>
  );
}
