"use client";

import { useMemo, useState, useSyncExternalStore } from "react";
import { ChevronDown, ChevronLeft, Menu, Moon, Sun, X } from "lucide-react";
import Link from "next/link";

import BrandLogo from "@/components/BrandLogo";
import { useModalFocus } from "@/components/ui/useModalFocus";
import { navGroupsFor, navPath, type NavId } from "@/lib/nav";
import {
  getThemeServerSnapshot,
  getThemeSnapshot,
  setTheme,
  subscribeTheme,
  type Theme,
} from "@/lib/theme";
import type { AuthUser } from "@/lib/api";

interface SidebarProps {
  activeId: NavId;
  collapsed: boolean;
  mobileOpen: boolean;
  user: AuthUser;
  onNavigate: (id: NavId) => void;
  onToggleCollapse: () => void;
  onCloseMobile: () => void;
}

const THEMES: { id: Theme; label: string; icon: typeof Sun }[] = [
  { id: "light", label: "Light", icon: Sun },
  { id: "dark", label: "Dark", icon: Moon },
];

/**
 * The left rail: destinations are separated into quiet visual clusters without
 * adding category copy above the links. The collapse control lives in its own
 * compact row and the account card stays anchored at the bottom.
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
}: SidebarProps) {
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeServerSnapshot);
  const railRef = useModalFocus<HTMLElement>(mobileOpen, onCloseMobile);

  // A staff member gets a shorter rail: the destinations they cannot use are
  // refused by the API anyway, and offering them is offering a dead end.
  const groups = useMemo(() => navGroupsFor(user.role, user.pages), [user.role, user.pages]);
  const canOpenAccount = groups.some((group) => group.items.some((item) => item.id === "settings"));
  // Each new signed-in workspace starts with its navigation groups closed.
  // The active parent still highlights the current destination, without forcing
  // its children open or preventing the user from collapsing that group.
  const [openGroups, setOpenGroups] = useState<Set<string>>(() => new Set());

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
        ref={railRef}
        className={`rail ${collapsed ? "is-collapsed" : ""} ${mobileOpen ? "is-open" : ""}`}
        aria-label="Main navigation"
        role={mobileOpen ? "dialog" : undefined}
        aria-modal={mobileOpen || undefined}
        tabIndex={mobileOpen ? -1 : undefined}
      >
        {/* The brand, at the top of the navigation it names. It is held to the
            header bar's own height so the hairline under it continues the one
            the header draws, and the two read as a single rule across the
            shell. The collapse control still rides the first group's heading
            rather than taking a band of its own. */}
        <div className="rail-brand">
          <span className="rail-logo">
            <BrandLogo />
          </span>
        </div>

        <div className="rail-scroll">
          <div className="rail-collapse-row">
            <span className="rail-menu-label">Menu</span>
            <button type="button" className="rail-toggle rail-mobile-close" aria-label="Close navigation" onClick={onCloseMobile}>
              <X size={18} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="rail-toggle"
              onClick={onToggleCollapse}
              title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              aria-expanded={!collapsed}
            >
              {collapsed ? <Menu size={18} /> : <ChevronLeft size={18} />}
            </button>
          </div>

          {groups.map((group) => {
            const GroupIcon = group.icon;
            const groupActive = group.items.some((item) => item.id === activeId);
            const isOpen = openGroups.has(group.label);
            return (
            <div
              key={group.label}
              className={`rail-group ${group.collapsible ? "is-nested" : ""}`}
              role="group"
              aria-label={group.label}
            >
              {group.collapsible && (
                <button
                  type="button"
                  className={`rail-section-toggle ${groupActive ? "is-current" : ""}`}
                  onClick={() => {
                    if (collapsed) {
                      onToggleCollapse();
                      setOpenGroups((current) => new Set([...current, group.label]));
                      return;
                    }
                    setOpenGroups((current) => {
                      const next = new Set(current);
                      if (next.has(group.label)) next.delete(group.label);
                      else next.add(group.label);
                      return next;
                    });
                  }}
                  aria-expanded={isOpen}
                  title={collapsed ? group.label : undefined}
                >
                  {GroupIcon && <GroupIcon size={18} strokeWidth={2} />}
                  <span className="rail-item-label">{group.label}</span>
                  <ChevronDown className="rail-section-chevron" size={15} />
                </button>
              )}
              <div className={`rail-group-items ${group.collapsible && !isOpen ? "is-closed" : ""}`}>
              {group.items.map((item) => {
                const Icon = item.icon;
                const isActive = activeId === item.id;
                return (
                  <Link
                    key={item.id}
                    href={navPath(item.id)}
                    className={`rail-item ${isActive ? "is-active" : ""}`}
                    onClick={(event) => {
                      // Keep modified clicks native so a destination can open
                      // in a new tab. Plain clicks stay inside the mounted CRM
                      // workspace and preserve its loaded data.
                      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                      event.preventDefault();
                      go(item.id);
                    }}
                    aria-current={isActive ? "page" : undefined}
                    title={collapsed ? item.label : undefined}
                  >
                    <Icon size={18} strokeWidth={2} />
                    <span className="rail-item-label">{item.label}</span>
                  </Link>
                );
              })}
              </div>
            </div>
          );})}
        </div>

        <div className="rail-foot">
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
          {canOpenAccount && (
            <button type="button" className="rail-account-summary" onClick={() => go("settings")} aria-label={`Account settings for ${user.name || user.email}`} title={collapsed ? `${user.name || user.email} · Account settings` : undefined}>
              <span className="rail-account-monogram" aria-hidden="true">{(user.name || user.email).split(/\s+/).slice(0, 2).map((part) => part[0]).join("").toUpperCase()}</span>
              <span className="rail-account-copy"><strong>{user.name || user.email}</strong><small>{user.role}</small></span>
            </button>
          )}
        </div>
      </nav>
    </>
  );
}
