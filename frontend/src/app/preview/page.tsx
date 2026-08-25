"use client";

/* TEMPORARY design harness — not part of the product.
   Renders the real shell around any one screen, so the redesign can be looked
   at without clicking through the app. Query parameters:

     ?screen=candidates   which screen to mount (default: overview)
     ?screen=profile      one candidate, with the job and document sections
     ?theme=dark          stamps the theme the rail's pill would
     ?token=…             seeds the API session, so the screens that fetch
                          their own data get real rows back

   Delete this route once the redesign is signed off. */

import { useEffect, useMemo, useState } from "react";

import Sidebar from "@/components/Sidebar";
import TopBar from "@/components/TopBar";
import OverviewScreen from "@/screens/OverviewScreen";
import CandidatesView from "@/screens/CandidatesView";
import JobOrders from "@/screens/JobOrders";
import SourcingHub from "@/screens/SourcingHub";
import DataManagementScreen from "@/screens/DataManagementScreen";
import UserManagementScreen from "@/screens/UserManagementScreen";
import ActivityLogsScreen from "@/screens/ActivityLogsScreen";
import SettingsScreen from "@/screens/SettingsScreen";
import AdminStaffManagement from "@/screens/AdminStaffManagement";
import CandidateProfileScreen from "@/screens/CandidateProfileScreen";
import { NAV_META, type NavId } from "@/lib/nav";
import { setTheme } from "@/lib/theme";
import { setToken, type AuthUser, type CandidateRecord } from "@/lib/api";

const USER: AuthUser = {
  id: "preview",
  email: "recruiter@adira.co",
  name: "Aarthi Menon",
  role: "admin",
};

const FIRST = ["Arun", "Priya", "Rahul", "Meera", "Vikram", "Sana", "Karthik", "Divya", "Imran", "Nisha", "Joseph", "Lakshmi"];
const LAST = ["Kumar", "Nair", "Sharma", "Iyer", "Reddy", "Fernandes", "Bose", "Menon", "Khan", "Pillai", "Thomas", "Rao"];
const ROLES = [
  "Senior Site Engineer",
  "MEP Draughtsman",
  "Welder (6G)",
  "Warehouse Supervisor",
  "HVAC Technician",
  "QA/QC Inspector",
  "Heavy Vehicle Driver",
  "Electrical Foreman",
];
const PLACES = ["Chennai, TN", "Kochi, KL", "Dubai, UAE", "Coimbatore, TN", "Doha, QA", "Madurai, TN"];
const SKILLS = ["Welding", "AutoCAD", "Safety", "QA/QC", "Rigging", "Revit", "Forklift"];

/** Deterministic PRNG, so the harness renders the same page every reload. */
function rng(seed: number) {
  let s = seed;
  return () => {
    s = (s * 1664525 + 1013904223) % 4294967296;
    return s / 4294967296;
  };
}

function buildCandidates(count: number): CandidateRecord[] {
  const rand = rng(20260824);
  const out: CandidateRecord[] = [];

  for (let i = 0; i < count; i += 1) {
    // Weight arrivals toward mid-week and toward the recent past, so the plot
    // and the weekday chart have a shape rather than noise.
    const daysAgo = Math.floor(rand() ** 1.6 * 58);
    const created = new Date(Date.now() - daysAgo * 86400000 - Math.floor(rand() * 86400000));
    const confidence = 0.55 + rand() ** 0.6 * 0.44;
    const verified = confidence > 0.85 && rand() > 0.45;

    out.push({
      id: `c${(1000 + i).toString(16)}${Math.floor(rand() * 1e6).toString(16)}`,
      status: verified ? "verified" : "parsed",
      created_at: created.toISOString(),
      updated_at: created.toISOString(),
      profile: {
        confidence,
        full_name: `${FIRST[Math.floor(rand() * FIRST.length)]} ${LAST[Math.floor(rand() * LAST.length)]}`,
        email: `candidate${i}@example.com`,
        phone: `+91 90${(100000 + Math.floor(rand() * 899999)).toString()}`,
        location: PLACES[Math.floor(rand() * PLACES.length)],
        current_designation: ROLES[Math.floor(rand() * ROLES.length)],
        total_experience_years: Math.floor(rand() * 18) + 1,
        skills: SKILLS.slice(0, 2 + Math.floor(rand() * 4)),
      },
    } as CandidateRecord);
  }

  return out;
}

/** One fully-populated record — every section the profile screen can draw. */
const PROFILE_CANDIDATE = {
  id: "preview-candidate",
  source: "whatsapp",
  status: "parsed",
  cv_required: false,
  created_at: "2026-08-13T09:30:00.000Z",
  updated_at: "2026-08-13T09:30:00.000Z",
  profile: {
    confidence: 0.92,
    full_name: "NASIM SHAH",
    current_designation: "Electrician",
    email: "nasimshah096@example.com",
    phone: "+91-6205611280",
    location: "Vill – Chaturbuhjwa, Po-Roari, Ps - Shikarpur, Dist- West Champaran, Bihar –845453, India",
    country: "India",
    destination_country: "Singapore",
    job_title: "Electrician",
    job_category: "electrician",
    job_preference: "Site electrician on a commercial project",
    course_or_trade: "ITI Electrician (NCVT), 2019",
    state_preference: "Jurong / Tuas",
    available_from: "After 2 months — serving notice",
    passport_number: "Z1234567",
    passport_expiry: "2031-03-14",
    trade_skills: ["Panel wiring", "Cable tray erection", "Motor termination", "Megger testing"],
    skills: ["Electrical maintenance", "Wiring", "Troubleshooting", "Safety compliance"],
    languages: ["Hindi", "Bhojpuri", "English"],
    total_experience_years: 6,
    resume_summary: "Six years as a site electrician across residential and light-industrial projects.",
    job_answers: [
      { question_id: "q1", question: "How many years have you worked as an electrician?", answer: "6 years", kind: "text" },
      { question_id: "q2", question: "Do you hold a valid wireman licence?", answer: "Yes — Bihar state licence, valid to 2028", kind: "choice" },
      { question_id: "q3", question: "Have you worked overseas before?", answer: "No, this would be my first placement abroad", kind: "choice" },
    ],
    work_experience: [
      {
        company: "Shree Constructions",
        designation: "Site Electrician",
        start_date: "Mar 2021",
        end_date: "Present",
        location: "Patna, Bihar",
        description: "Panel wiring and terminations for a 9-tower residential site.\nMegger testing and handover documentation for each block.",
      },
    ],
    education: [{ degree: "ITI — Electrician", institution: "Govt. ITI West Champaran", end_date: "2019", grade: "First class" }],
    certifications: ["Wireman licence (Bihar)", "Basic fire safety"],
    additional_info: { industry: "Construction", highest_qualification: "ITI Electrician" },
  },
} as unknown as CandidateRecord;

const noop = () => {};

export default function PreviewPage() {
  const [collapsed, setCollapsed] = useState(false);
  const [ready, setReady] = useState(false);

  const params = useMemo(
    () => (typeof window === "undefined" ? null : new URLSearchParams(window.location.search)),
    [],
  );

  useEffect(() => {
    const theme = params?.get("theme");
    if (theme === "dark" || theme === "light") setTheme(theme);
    const token = params?.get("token");
    if (token) setToken(token);
    setReady(true);
  }, [params]);

  const candidates = useMemo(() => buildCandidates(240), []);
  // The raw parameter, because the harness offers one view that is not a nav
  // destination: a single candidate's profile.
  const screenParam = params?.get("screen") ?? "overview";
  const screen = screenParam as NavId;
  const meta = NAV_META[screen] ?? NAV_META.overview;

  // The screens that fetch for themselves must not mount before the token is in
  // storage, or their first request goes out unauthenticated.
  if (!ready) return null;

  const go = (id: NavId) => {
    const next = new URLSearchParams(params ?? undefined);
    next.set("screen", id);
    window.location.search = next.toString();
  };

  return (
    <div className={`app-shell ${collapsed ? "is-collapsed" : ""}`}>
      <TopBar
        user={USER}
        syncing={false}
        realtime="live"
        hasRail
        onSync={noop}
        onToggleRail={noop}
      />

      <div className="app-body">
        <Sidebar
          activeId={screen}
          collapsed={collapsed}
          mobileOpen={false}
          user={USER}
          onNavigate={go}
          onToggleCollapse={() => setCollapsed((c) => !c)}
          onCloseMobile={noop}
          onSignOut={noop}
        />

        <main className="workspace">
          <div className="db-page">
            {screen !== "overview" && screen !== "candidates" && screenParam !== "profile" && (
              <header className="db-page-head">
                <div>
                  <span className="db-eyebrow">{meta.eyebrow}</span>
                  <h1 className="db-title">{meta.title}</h1>
                  <p className="db-subtitle">{meta.subtitle}</p>
                </div>
              </header>
            )}

            {screen === "overview" && (
              <OverviewScreen
                total={candidates.length}
                candidates={candidates}
                logs={[]}
                onNavigate={go}
                onOpenCandidate={noop}
              />
            )}

            {screen === "candidates" && (
              <CandidatesView
                candidates={candidates}
                logCounts={{}}
                onOpenCandidate={noop}
                onEditCandidate={noop}
                onOpenLogs={noop}
                onDeleteCandidate={noop}
              />
            )}

            {screenParam === "profile" && (
              <CandidateProfileScreen candidate={PROFILE_CANDIDATE} onBack={noop} onVerify={noop} />
            )}

            {screen === "job-orders" && <JobOrders candidates={candidates} />}
            {screen === "sourcing" && <SourcingHub />}
            {screen === "data-management" && <DataManagementScreen />}
            {screen === "users" && <UserManagementScreen currentUserId={USER.id} />}
            {screen === "staff" && (
              <AdminStaffManagement
                candidates={candidates}
                refreshNonce={0}
                onToast={noop}
                onCandidatesChanged={noop}
                onOpenCandidate={noop}
              />
            )}
            {screen === "activity" && (
              <ActivityLogsScreen
                systemLogs={[]}
                candidateLogs={[]}
                candidates={candidates}
                onOpenCandidateLogs={noop}
              />
            )}
            {screen === "settings" && <SettingsScreen user={USER} onSignOut={noop} />}
          </div>
        </main>
      </div>
    </div>
  );
}
