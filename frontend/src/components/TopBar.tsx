"use client";

import { Menu, RefreshCw } from "lucide-react";

import NotificationBell from "@/components/NotificationBell";
import type { AuthUser } from "@/lib/api";

interface TopBarProps {
  user: AuthUser;
  syncing: boolean;
  /** State of the push connection, shown as a pulse beside the sync control. */
  realtime?: "connecting" | "live" | "offline";
  /** Bumped on every realtime event, so the bell re-reads its feed at once. */
  realtimeNonce?: number;
  onOpenCandidate?: (candidateId: string) => void;
  /** Whether this session has a navigation rail, which owns the menu toggle. */
  hasRail?: boolean;
  onSync: () => void;
  onToggleRail: () => void;
}

/**
 * The bar across the very top of the product.
 *
 * Brand on the left, status on the right, nothing in between. What is *not*
 * here is most of the design:
 *
 *   * **No account avatar and no sign-out.** Both live on the rail's account
 *     card, next to the name they apply to. Two ways to leave a session is one
 *     more than anybody needs, and the initials button did nothing the rail
 *     card was not already doing.
 *   * **No activity-log shortcut.** Activity Logs is a rail destination like
 *     every other screen; a second entrance in the chrome made it look like a
 *     mode rather than a place.
 *   * **No global search.** Every screen it could have reached already searches
 *     the records it holds, and the field was the widest object up here.
 *
 * What remains is the state of the system rather than a set of controls: is
 * push connected, is anything waiting for you, and — for an admin — is a sync
 * running. A staff member sees exactly the first two, which is the entire bar
 * for the review workspace.
 */
export default function TopBar({
  user,
  syncing,
  realtime = "offline",
  realtimeNonce = 0,
  hasRail = true,
  onOpenCandidate,
  onSync,
  onToggleRail,
}: TopBarProps) {
  return (
    <header className="topbar">
      {hasRail && (
        <button className="topbar-icon-btn topbar-menu-btn" onClick={onToggleRail} aria-label="Open navigation">
          <Menu size={20} />
        </button>
      )}

      {/* The brand is gone from here: it lives at the head of the rail now, so
          the mark sits directly above the navigation it belongs to and the
          collapsed rail can keep the mark while dropping the word. What is left
          in this bar is state, not identity — which is all it ever reported. */}
      <div className="topbar-spacer" />

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

        {/* Ingestion is the admin's. Offering it to a staff member would be
            offering them a 403, and the review workspace is deliberately down
            to the pulse and the bell. */}
        {user.role === "admin" && (
          <button type="button" className="topbar-sync" onClick={onSync} disabled={syncing}>
            <RefreshCw size={15} className={syncing ? "icon-spin" : undefined} />
            <span>{syncing ? "Syncing…" : "Sync Gmail"}</span>
          </button>
        )}
      </div>
    </header>
  );
}
