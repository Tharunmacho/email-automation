/**
 * The rail's structure, in one place.
 *
 * `page.tsx` switches on these ids and the rail renders from the same list, so
 * a destination cannot exist in the navigation without a screen behind it — or
 * the other way round.
 *
 * Three groups under small uppercase headings: where you work, what you work
 * with, and what the system is doing. Flat inside each group — the grouping
 * carries the structure, so no item needs to be indented under another.
 *
 * No counts and no tags. A rail is for getting somewhere; a number on it is a
 * fact you did not ask for, and it competes with the one thing the rail has to
 * make obvious, which is where you currently are.
 */

import {
  Briefcase,
  Building2,
  FileText,
  Inbox,
  LayoutDashboard,
  ScrollText,
  Settings as SettingsIcon,
  ShieldCheck,
  Users,
  Zap,
  type LucideIcon,
} from "lucide-react";

export type NavId =
  | "overview"
  | "candidates"
  | "my-queue"
  | "staff"
  | "job-orders"
  | "resume-parser"
  | "sourcing"
  | "visualizer"
  | "activity"
  | "settings";

export interface NavItem {
  id: NavId;
  label: string;
  icon: LucideIcon;
  /**
   * Which roles may reach this destination. Omitted means everyone.
   *
   * This is presentation only — every one of these screens is also guarded by
   * the API, which is what actually enforces the boundary. Hiding a rail item
   * a staff member cannot use keeps the rail honest; it is not the security.
   */
  roles?: string[];
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

/**
 * Two products behind one sign-in.
 *
 * A staff account reaches exactly one destination: the candidates allocated to
 * them. Everything else in this list is the Super Admin's. That is not a
 * simplification of the rail — it is the whole difference between the two
 * roles, so it is expressed here as data rather than as conditionals scattered
 * through the shell.
 *
 * Sign-out and the theme switch live on the rail's account card, not in this
 * list, so a one-item rail still gives a staff member everything they need.
 */
export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Workspace",
    items: [{ id: "my-queue", label: "My Candidates", icon: Inbox, roles: ["staff"] }],
  },
  {
    label: "General",
    items: [
      { id: "staff", label: "Staff", icon: ShieldCheck, roles: ["admin"] },
      { id: "overview", label: "Overview", icon: LayoutDashboard, roles: ["admin"] },
      { id: "candidates", label: "Candidates", icon: Users, roles: ["admin"] },
    ],
  },
  {
    label: "Tools",
    items: [
      { id: "job-orders", label: "Job Orders", icon: Briefcase, roles: ["admin"] },
      { id: "sourcing", label: "Sourcing Hub", icon: Building2, roles: ["admin"] },
      { id: "visualizer", label: "Flow Visualizer", icon: Zap, roles: ["admin"] },
      { id: "resume-parser", label: "Resume Parser", icon: FileText, roles: ["admin"] },
    ],
  },
  {
    label: "Support",
    items: [
      { id: "activity", label: "Activity Logs", icon: ScrollText, roles: ["admin"] },
      { id: "settings", label: "Settings", icon: SettingsIcon, roles: ["admin"] },
    ],
  },
];

/** The rail as one role sees it, with any group that empties out dropped. */
export function navGroupsFor(role: string | undefined): NavGroup[] {
  return NAV_GROUPS.map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.roles || item.roles.includes(role ?? "")),
  })).filter((group) => group.items.length > 0);
}

/**
 * Where a role lands on sign-in.
 *
 * The admin opens on the staff console rather than the ingestion Overview:
 * their first question is who is holding what and whether anyone has stalled,
 * and that screen is also where the SLA breaches are listed.
 */
export function defaultNavFor(role: string | undefined): NavId {
  return role === "staff" ? "my-queue" : "staff";
}

/** Header copy per destination — the eyebrow/title/subtitle every screen opens with. */
export const NAV_META: Record<NavId, { eyebrow: string; title: string; subtitle: string }> = {
  overview: {
    eyebrow: "Dashboard",
    title: "Overview",
    subtitle: "Candidate sourcing, email extraction, and processing pipeline in one view.",
  },
  candidates: {
    eyebrow: "General",
    title: "Candidates",
    subtitle: "Every parsed profile in the database.",
  },
  "my-queue": {
    eyebrow: "Workspace",
    title: "My Candidates",
    subtitle: "The profiles allocated to you — read the résumé, record your evaluation.",
  },
  staff: {
    eyebrow: "General",
    title: "Staff & Allocation",
    subtitle: "Accounts, expertise keywords, workload balance, and the 10-hour SLA.",
  },
  "job-orders": {
    eyebrow: "Tools",
    title: "Job Orders",
    subtitle: "Client requisitions and the candidate matching pipeline.",
  },
  "resume-parser": {
    eyebrow: "Tools",
    title: "Resume Parser",
    subtitle: "How résumés reach the parser, what it scored them at, and the fields it extracts.",
  },
  sourcing: {
    eyebrow: "Tools",
    title: "Sourcing Hub",
    subtitle: "Business clients and association members who submit talent requirements.",
  },
  visualizer: {
    eyebrow: "Tools",
    title: "Flow Visualizer",
    subtitle: "Gmail → AI extraction → MongoDB, stage by stage, as the pipeline runs it.",
  },
  activity: {
    eyebrow: "Support",
    title: "Activity Logs",
    subtitle: "Full pipeline audit history and system events.",
  },
  settings: {
    eyebrow: "Support",
    title: "Settings",
    subtitle: "Preferences, system health, and the rules the ingestion pipeline applies.",
  },
};
