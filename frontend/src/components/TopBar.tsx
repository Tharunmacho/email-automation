"use client";

import { useEffect, useRef, useState } from "react";
import { CalendarDays, LogOut, Menu, RefreshCw, Search } from "lucide-react";

import NotificationBell from "@/components/NotificationBell";
import { initialsOf } from "@/lib/format";
import { useIsMounted } from "@/lib/useIsMounted";
import type { AuthUser } from "@/lib/api";

interface TopBarProps {
  user: AuthUser;
  syncing: boolean;
  /** Bumped on every realtime event, so the bell re-reads its feed at once. */
  realtimeNonce?: number;
  onOpenCandidate?: (candidateId: string) => void;
  /** Whether this session has a navigation rail, which owns the menu toggle. */
  hasRail?: boolean;
  onSync: () => void;
  onToggleRail: () => void;
  /** Runs a search from the bar: opens the candidate pool on the term. */
  onSearch?: (term: string) => void;
  onSignOut: () => void;
}

/** "12:37 PM, Wed" — the reference's format, in the viewer's own locale. */
function stamp(now: Date): string {
  const time = now.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
  });
  const day = now.toLocaleDateString(undefined, { weekday: "short" });
  return `${time}, ${day}`;
}

/**
 * The bar across the top of the workspace.
 *
 * One row of pills: the sync action, the clock, the search field, the bell and
 * the account. Everything is a rounded control on the bar rather than a bare
 * icon, so the row reads as a set of objects at one weight instead of a mix of
 * buttons and glyphs.
 *
 * What is *not* here: the brand, which lives at the head of the rail; and the
 * live-socket pulse, which was a badge reporting a state that is only worth
 * interrupting for when it is bad.
 */
export default function TopBar({
  user,
  syncing,
  realtimeNonce = 0,
  hasRail = true,
  onOpenCandidate,
  onSync,
  onToggleRail,
  onSearch,
  onSignOut,
}: TopBarProps) {
  const mounted = useIsMounted();
  const [now, setNow] = useState(() => new Date());
  const [term, setTerm] = useState("");
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  // Ticks on the minute boundary rather than every 60s from mount, so the
  // displayed minute changes when the clock does and not up to 59s late.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const schedule = () => {
      const ms = 60000 - (Date.now() % 60000);
      timer = setTimeout(() => {
        setNow(new Date());
        schedule();
      }, ms);
    };
    schedule();
    return () => clearTimeout(timer);
  }, []);

  // Close the account menu on an outside click or on Escape.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (event: MouseEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) setMenuOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    onSearch?.(term.trim());
  };

  return (
    <header className="topbar">
      {hasRail && (
        <button
          className="topbar-icon-btn topbar-menu-btn"
          onClick={onToggleRail}
          aria-label="Open navigation"
        >
          <Menu size={20} />
        </button>
      )}

      <div className="topbar-spacer" />

      <div className="topbar-actions">
        {/* Ingestion is the admin's. Offering it to a staff member would be
            offering them a 403. */}
        {user.role === "admin" && (
          <button type="button" className="topbar-sync" onClick={onSync} disabled={syncing}>
            <RefreshCw size={15} className={syncing ? "icon-spin" : undefined} />
            <span>{syncing ? "Syncing…" : "Sync Gmail"}</span>
          </button>
        )}

        {/* Held back until mounted: the server prerenders this page once, at
            build time, in whatever timezone the build ran in. Rendering a clock
            there would ship a wrong time baked into the HTML and then hydrate
            over it. */}
        <div className="topbar-clock" suppressHydrationWarning>
          <CalendarDays size={15} />
          <span>{mounted ? stamp(now) : "—"}</span>
        </div>

        <form className="topbar-search" onSubmit={submit} role="search">
          <Search size={15} className="topbar-search-icon" />
          <input
            type="search"
            className="topbar-search-input"
            placeholder="Search candidates by name, role, skill…"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            aria-label="Search candidates"
          />
        </form>

        <NotificationBell nonce={realtimeNonce} onOpenCandidate={onOpenCandidate} />

        {/* The account. The rail carries the same identity, but the rail is
            collapsible and absent for a staff reviewer, so the bar keeps a copy
            that is always reachable. */}
        <div className="topbar-account" ref={menuRef}>
          <button
            type="button"
            className="topbar-avatar"
            onClick={() => setMenuOpen((open) => !open)}
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            title={user.email}
          >
            {initialsOf(user.name || user.email)}
          </button>

          {menuOpen && (
            <div className="topbar-menu" role="menu">
              <div className="topbar-menu-head">
                <span className="topbar-menu-name">{user.name || "Account"}</span>
                <span className="topbar-menu-mail">{user.email}</span>
                <span className="topbar-menu-role">
                  {user.role === "admin" ? "Super Admin" : "Staff"}
                </span>
              </div>
              <button type="button" className="topbar-menu-item" onClick={onSignOut} role="menuitem">
                <LogOut size={14} /> Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
