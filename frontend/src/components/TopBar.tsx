"use client";

import { LogOut, Menu, RefreshCw, ScrollText, Sparkles } from "lucide-react";

import NotificationBell from "@/components/NotificationBell";
import { initialsOf } from "@/lib/format";
import type { AuthUser } from "@/lib/api";

interface TopBarProps {
  user: AuthUser;
  syncing: boolean;
  /** State of the push connection, shown as a pulse beside the sync control. */
  realtime?: "connecting" | "live" | "offline";
  /** Bumped on every realtime event, so the bell re-reads its feed at once. */
  realtimeNonce?: number;
  onOpenCandidate?: (candidateId: string) => void;
  /**
   * Whether this session has a navigation rail.
   *
   * The staff workspace is a single screen and renders without one, which
   * moves identity and sign-out into this bar — they live on the rail's
   * account card for everyone else, and a staff member with no rail would
   * otherwise have no way out of the session.
   */
  hasRail?: boolean;
  onSync: () => void;
  onOpenActivity: () => void;
  onOpenSettings: () => void;
  onToggleRail: () => void;
  onSignOut: () => void;
}

/**
 * The bar across the very top of the product.
 *
 * Brand on the left, three controls on the right, nothing in between. The
 * search field that used to sit here is gone: every screen it could reach
 * already has its own search over the records it holds, so a global field was
 * a second way to do the same job and the widest object in the chrome.
 *
 * Sign-out is not here either — it lives on the rail's account card, next to
 * the name it applies to. The avatar opens Settings, which is where the rest
 * of the account lives.
 */
export default function TopBar({
  user,
  syncing,
  realtime = "offline",
  realtimeNonce = 0,
  hasRail = true,
  onOpenCandidate,
  onSync,
  onOpenActivity,
  onOpenSettings,
  onToggleRail,
  onSignOut,
}: TopBarProps) {
  return (
    <header className="topbar">
      {hasRail && (
        <button className="topbar-icon-btn topbar-menu-btn" onClick={onToggleRail} aria-label="Open navigation">
          <Menu size={20} />
        </button>
      )}

      {/* Mark and name, one flex row, centred on each other — see `.topbar-brand`
          for why the line-height is part of that and not decoration. */}
      <div className="topbar-brand">
        <span className="topbar-logo" aria-hidden="true">
          <Sparkles size={20} strokeWidth={2.1} />
        </span>
        <span className="topbar-title">TalentFlow AI</span>
      </div>

      <div className="topbar-actions">
        {/* The dot reports the real socket state. A badge that always reads
            "live" would be worse than none — the one thing it has to be able
            to say is that push has stopped working. */}
        <span
          className={`ws-pulse is-${realtime}`}
          title={
            realtime === "live"
              ? "Live updates connected"
              : realtime === "connecting"
                ? "Connecting to live updates…"
                : "Live updates offline — reconnecting"
          }
        >
          <span className="ws-pulse-dot" aria-hidden="true" />
          <span className="ws-pulse-label">
            {realtime === "live" ? "Live" : realtime === "connecting" ? "…" : "Offline"}
          </span>
        </span>

        {/* Both roles get the bell: being told what has been allocated to you
            is the staff member's half of the feature, and the reason it exists
            at all. What is *in* the feed is already scoped server-side. */}
        <NotificationBell nonce={realtimeNonce} onOpenCandidate={onOpenCandidate} />

        {/* Ingestion and the audit log are for super admin role only.
            For staff members, they are omitted completely to keep the topbar clean. */}
        {user.role === "admin" && (
          <>
            <button type="button" className="topbar-sync" onClick={onSync} disabled={syncing}>
              <RefreshCw size={15} className={syncing ? "icon-spin" : undefined} />
              <span>{syncing ? "Syncing…" : "Sync Gmail"}</span>
            </button>

            <button
              type="button"
              className="topbar-icon-btn"
              onClick={onOpenActivity}
              title="Activity logs"
              aria-label="Activity logs"
            >
              <ScrollText size={20} />
            </button>
          </>
        )}

        <button
          type="button"
          className="topbar-avatar"
          onClick={onOpenSettings}
          title={`${user.email} — open settings`}
          aria-label={`Account: ${user.name || user.email}`}
        >
          {initialsOf(user.name || user.email).charAt(0)}
        </button>

        <button
          type="button"
          className="topbar-icon-btn"
          onClick={onSignOut}
          title={`Sign out of ${user.email}`}
          aria-label="Sign out"
        >
          <LogOut size={18} />
        </button>
      </div>
    </header>
  );
}
