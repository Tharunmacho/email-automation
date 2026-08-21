"use client";

import Image from "next/image";
import { useMemo, useState, useSyncExternalStore } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, ChevronsUpDown, LogOut, Moon, Sun } from "lucide-react";

import { navGroupsFor, type NavId } from "@/lib/nav";
import {
  getThemeServerSnapshot,
  getThemeSnapshot,
  setTheme,
  subscribeTheme,
  type Theme,
} from "@/lib/theme";
import { initialsOf } from "@/lib/format";
import type { AuthUser } from "@/lib/api";

interface SidebarProps {
  activeId: NavId;
  collapsed: boolean;
  mobileOpen: boolean;
  user: AuthUser;
  onNavigate: (id: NavId) => void;
  onToggleCollapse: () => void;
  onCloseMobile: () => void;
  onSignOut: () => void;
}

const THEMES: { id: Theme; label: string; icon: typeof Sun }[] = [
  { id: "light", label: "Light", icon: Sun },
  { id: "dark", label: "Dark", icon: Moon },
];

/**
 * The left rail: three labelled groups of destinations with the collapse control
 * on the first heading's row, and the account card at the bottom.
 *
 * Collapsed it keeps only the icons — same list, same order, same active
 * marker, so the muscle memory built at full width still works at 68px. Nothing
 * is removed on collapse that you cannot get back by hovering: every control
 * keeps its `title`, which is the tooltip a labelless icon needs.
 */
export default function Sidebar({
  activeId,
  collapsed,
  mobileOpen,
  user,
  onNavigate,
  onToggleCollapse,
  onCloseMobile,
  onSignOut,
}: SidebarProps) {
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeServerSnapshot);

  // A staff member gets a shorter rail: the destinations they cannot use are
  // refused by the API anyway, and offering them is offering a dead end.
  const groups = useMemo(() => navGroupsFor(user.role, user.pages), [user.role, user.pages]);

  // Which section headings are folded shut. Empty means every group is open,
  // which is the state the rail should start in: a first-run sidebar that hides
  // its own destinations is a sidebar nobody finds the rest of the product in.
  const [shutGroups, setShutGroups] = useState<Set<string>>(new Set());

  const toggleGroup = (label: string) =>
    setShutGroups((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });

  const go = (id: NavId) => {
    onNavigate(id);
    onCloseMobile();
  };

  return (
    <>
      <div
        className={`rail-backdrop ${mobileOpen ? "is-open" : ""}`}
        onClick={onCloseMobile}
        aria-hidden="true"
      />

      <nav
        className={`rail ${collapsed ? "is-collapsed" : ""} ${mobileOpen ? "is-open" : ""}`}
        aria-label="Main navigation"
      >
        {/* The brand owns the head of the rail. Expanded it is the mark and the
            word; collapsed the word is dropped and the mark alone holds the
            slot, which is why the mark is a square badge rather than a wordmark
            with a glyph in it — it has to survive on its own at 68px. */}
        <button
          type="button"
          className="rail-toggle"
          onClick={onToggleCollapse}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
        >
          {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
        </button>

        <div className="rail-brand">
          <span className="rail-brand-mark" aria-hidden="true">
            <Image src="/adira-logo@4x.png" alt="" width={224} height={200} preload />
          </span>
          <span className="rail-brand-name">Adira</span>
        </div>

        <div className="rail-scroll">
          {groups.map((group, index) => {
            const isShut = shutGroups.has(group.label);
            return (
              <div key={group.label} className={`rail-group ${isShut ? "is-shut" : ""}`}>
                <div className="rail-group-head">
                  {/* The heading is the control. Collapsed to icons the label is
                      gone, so folding a group you cannot read would be folding a
                      group you cannot name — the caret goes with it. */}
                  <button
                    type="button"
                    className="rail-group-label"
                    onClick={() => toggleGroup(group.label)}
                    aria-expanded={!isShut}
                    disabled={collapsed}
                  >
                    <span className="rail-group-word">{group.label}</span>
                    <ChevronDown size={13} className="rail-group-caret" />
                  </button>
                </div>

                {/* The items are wrapped so the connector spine has something to
                    run the height of. It is drawn on this element, not on each
                    row, so it is one continuous line rather than a stack of
                    segments with seams between them. */}
                <div className="rail-group-items" hidden={isShut && !collapsed}>
                  {group.items.map((item) => {
                    const Icon = item.icon;
                    const isActive = activeId === item.id;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        className={`rail-item ${isActive ? "is-active" : ""}`}
                        onClick={() => go(item.id)}
                        aria-current={isActive ? "page" : undefined}
                        title={collapsed ? item.label : undefined}
                      >
                        <Icon size={18} strokeWidth={2} />
                        <span className="rail-item-label">{item.label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>

        <div className="rail-foot">
          {/* The account card. "Recruitment Team" is the workspace this session
              belongs to; the signed-in address sits under it, so the card says
              both which team you are in and who you are in it as. */}
          <div className="rail-account">
            <span className="rail-avatar" aria-hidden="true">
              {initialsOf(user.name || user.email)}
            </span>
            <span className="rail-account-text">
              <span className="rail-account-name">Recruitment Team</span>
              <span className="rail-account-mail" title={user.email}>
                {user.email}
              </span>
            </span>
            {/* The chevrons are the affordance; the exit icon is what the
                control does, and it swaps in on hover so the button cannot be
                mistaken for a menu that merely expands. */}
            <button
              type="button"
              className="rail-signout"
              onClick={onSignOut}
              title={`Sign out of ${user.email}`}
              aria-label="Sign out"
            >
              <ChevronsUpDown size={14} className="rail-account-chevrons" />
              <LogOut size={14} className="rail-account-exit" />
            </button>
          </div>

          {/* Collapsed, the pill becomes one button that flips the theme — a
              two-up segmented control does not fit 52px, and hiding the control
              entirely would strand anyone who works with the rail closed. */}
          <div className="theme-switch" role="group" aria-label="Colour theme">
            {THEMES.map(({ id, label, icon: Icon }) => (
              <button
                key={id}
                type="button"
                className={`theme-switch-btn ${theme === id ? "is-on" : ""}`}
                onClick={() => setTheme(id)}
                aria-pressed={theme === id}
                title={`${label} theme`}
              >
                <Icon size={13} /> <span className="rail-item-label">{label}</span>
              </button>
            ))}
          </div>

          <button
            type="button"
            className="rail-theme-mini"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            title={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
            aria-label={theme === "dark" ? "Switch to light theme" : "Switch to dark theme"}
          >
            {theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </button>
        </div>
      </nav>
    </>
  );
}
