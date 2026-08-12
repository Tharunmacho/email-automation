"use client";

/**
 * The bell, and the feed behind it.
 *
 * A toast is a moment; this is the record. Allocation happens on a Gmail poll,
 * which lands whenever the mail lands — usually not while the person it is
 * allocated to is watching the screen. The toast fires into an empty room and
 * is gone. This panel is what that person finds when they next open the app,
 * with a count on it that is true across logouts and restarts.
 *
 * Refreshed from two directions: the `nonce` a live event bumps, so the badge
 * moves the instant a socket delivers something, and a slow poll, so a session
 * left open with a dead socket still catches up.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Bell, Check, Inbox, ShieldAlert, UserPlus } from "lucide-react";

import { timeAgo } from "@/lib/format";
import { fetchNotifications, markNotificationsRead, type NotificationRecord } from "@/lib/api";

interface NotificationBellProps {
  /** Bumped by the page whenever a realtime event arrives. */
  nonce: number;
  /** Opens the candidate a notification points at, when it points at one. */
  onOpenCandidate?: (candidateId: string) => void;
}

/** How often to re-read the feed when nothing has pushed. */
const POLL_MS = 60000;

function iconFor(type: string) {
  if (type === "sla_alert") return ShieldAlert;
  if (type === "candidate_ingested") return Inbox;
  return UserPlus;
}

export default function NotificationBell({ nonce, onOpenCandidate }: NotificationBellProps) {
  const [items, setItems] = useState<NotificationRecord[]>([]);
  const [unread, setUnread] = useState(0);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const feed = await fetchNotifications(30);
      setItems(feed.items);
      setUnread(feed.unread);
    } catch {
      // A feed that cannot be read is not worth an error banner over the whole
      // app; the badge simply does not move until the next attempt.
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, nonce]);

  useEffect(() => {
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Click-away and Escape, because the panel overlays the screen it was opened
  // from and a dropdown that can only be closed by its own button is a trap.
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (!panelRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const markAll = useCallback(async () => {
    setBusy(true);
    try {
      const result = await markNotificationsRead({ all: true });
      setUnread(result.unread);
      setItems((current) => current.map((item) => ({ ...item, read: true })));
    } catch {
      // Leave the badge as it was rather than lying about it.
    } finally {
      setBusy(false);
    }
  }, []);

  const openOne = useCallback(
    async (item: NotificationRecord) => {
      if (!item.read) {
        // Optimistic: the row is already visibly read, and a failed write only
        // means it comes back unread on the next load.
        setItems((current) =>
          current.map((row) => (row.id === item.id ? { ...row, read: true } : row)),
        );
        setUnread((count) => Math.max(0, count - 1));
        try {
          await markNotificationsRead({ ids: [item.id] });
        } catch {
          void load();
        }
      }
      if (item.candidate_id && onOpenCandidate) {
        setOpen(false);
        onOpenCandidate(item.candidate_id);
      }
    },
    [load, onOpenCandidate],
  );

  const badge = useMemo(() => (unread > 9 ? "9+" : String(unread)), [unread]);

  return (
    <div className="notif" ref={panelRef}>
      <button
        type="button"
        className={`topbar-icon-btn notif-btn ${unread > 0 ? "has-unread" : ""}`}
        onClick={() => setOpen((value) => !value)}
        title={unread > 0 ? `${unread} unread notification${unread === 1 ? "" : "s"}` : "Notifications"}
        aria-label={
          unread > 0 ? `Notifications, ${unread} unread` : "Notifications"
        }
        aria-expanded={open}
      >
        <Bell size={20} />
        {unread > 0 && <span className="notif-badge">{badge}</span>}
      </button>

      {open && (
        <div className="notif-panel" role="dialog" aria-label="Notifications">
          <header className="notif-head">
            <strong>Notifications</strong>
            {unread > 0 && (
              <button type="button" className="notif-mark" onClick={() => void markAll()} disabled={busy}>
                <Check size={13} />
                Mark all read
              </button>
            )}
          </header>

          {items.length === 0 ? (
            <div className="notif-empty">
              <Bell size={18} />
              <p>Nothing yet</p>
              <span>New allocations and SLA alerts land here.</span>
            </div>
          ) : (
            <ul className="notif-list">
              {items.map((item) => {
                const Icon = iconFor(item.type);
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      className={`notif-row ${item.read ? "" : "is-unread"}`}
                      onClick={() => void openOne(item)}
                    >
                      <span className={`notif-icon is-${item.type}`}>
                        <Icon size={14} strokeWidth={2.2} />
                      </span>
                      <span className="notif-text">
                        <strong>{item.title}</strong>
                        <em>{item.message}</em>
                        <span className="notif-when">
                          {item.created_at ? timeAgo(item.created_at) : ""}
                        </span>
                      </span>
                      {!item.read && <span className="notif-dot" aria-hidden="true" />}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
