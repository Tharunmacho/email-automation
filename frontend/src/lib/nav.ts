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
  LayoutDashboard,
  ScrollText,
  Settings as SettingsIcon,
  Users,
  Zap,
  type LucideIcon,
} from "lucide-react";

export type NavId =
  | "overview"
  | "candidates"
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
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "General",
    items: [
      { id: "overview", label: "Overview", icon: LayoutDashboard },
      { id: "candidates", label: "Candidates", icon: Users },
    ],
  },
  {
    label: "Tools",
    items: [
      { id: "job-orders", label: "Job Orders", icon: Briefcase },
      { id: "sourcing", label: "Sourcing Hub", icon: Building2 },
      { id: "visualizer", label: "Flow Visualizer", icon: Zap },
      { id: "resume-parser", label: "Resume Parser", icon: FileText },
    ],
  },
  {
    label: "Support",
    items: [
      { id: "activity", label: "Activity Logs", icon: ScrollText },
      { id: "settings", label: "Settings", icon: SettingsIcon },
    ],
  },
];

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
