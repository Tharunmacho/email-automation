"use client";

import { Bell, Menu, RefreshCw, Sparkles } from "lucide-react";

import { initialsOf } from "@/lib/format";
import type { AuthUser } from "@/lib/api";

interface TopBarProps {
  user: AuthUser;
  syncing: boolean;
  onSync: () => void;
  onOpenActivity: () => void;
  onOpenSettings: () => void;
  onToggleRail: () => void;
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
  onSync,
  onOpenActivity,
  onOpenSettings,
  onToggleRail,
}: TopBarProps) {
  return (
    <header className="topbar">
      <button className="topbar-icon-btn topbar-menu-btn" onClick={onToggleRail} aria-label="Open navigation">
        <Menu size={18} />
      </button>

      <div className="topbar-brand">
        <span className="topbar-logo" aria-hidden="true">
          <Sparkles size={17} strokeWidth={2.3} />
        </span>
        <span className="topbar-title">TalentFlow AI</span>
      </div>

      <div className="topbar-actions">
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
          <Bell size={17} />
        </button>

        <button
          type="button"
          className="topbar-avatar"
          onClick={onOpenSettings}
          title={`${user.email} — open settings`}
          aria-label={`Account: ${user.name || user.email}`}
        >
          {initialsOf(user.name || user.email).charAt(0)}
        </button>
      </div>
    </header>
  );
}
