"use client";

/**
 * The call icon beside the bell.
 *
 * A "callback" verdict means staff rang the candidate and nobody picked up.
 * Twenty-four hours later the candidate is due another call, and this icon is
 * where that reminder lives: it rings, carries a count, and opens a list of
 * who to call. Reading the list also raises one bell notification per due
 * callback on the server, so the reminder survives a closed tab.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Phone, PhoneCall } from "lucide-react";

import { timeAgo } from "@/lib/format";
import { fetchDueCallbacks, type DueCallback } from "@/lib/api";

interface CallbackReminderProps {
  /** Bumped by the page whenever a realtime event arrives. */
  nonce: number;
  onOpenCandidate?: (candidateId: string) => void;
}

const POLL_MS = 60000;

export default function CallbackReminder({ nonce, onOpenCandidate }: CallbackReminderProps) {
  const [items, setItems] = useState<DueCallback[]>([]);
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement | null>(null);

  const load = useCallback(async () => {
    try {
      const due = await fetchDueCallbacks();
      setItems(due.items);
    } catch {
      // Same as the bell: a failed read leaves the icon as it was.
    }
  }, []);

  useEffect(() => {
    let live = true;
    void (async () => {
      if (live) await load();
    })();
    return () => {
      live = false;
    };
  }, [load, nonce]);

  useEffect(() => {
    const timer = setInterval(() => void load(), POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

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

  const count = items.length;
  const badge = count > 9 ? "9+" : String(count);

  return (
    <div className="notif" ref={panelRef}>
      <button
        type="button"
        className={`topbar-icon-btn notif-btn callback-btn ${count > 0 ? "has-due" : ""}`}
        onClick={() => {
          setOpen((value) => !value);
          void load();
        }}
        title={count > 0 ? `${count} candidate${count === 1 ? "" : "s"} to call again` : "Callbacks"}
        aria-label={count > 0 ? `Callbacks, ${count} due` : "Callbacks"}
        aria-expanded={open}
      >
        {count > 0 ? <PhoneCall size={19} /> : <Phone size={19} />}
        {count > 0 && <span className="notif-badge">{badge}</span>}
      </button>

      {open && (
        <div className="notif-panel" role="dialog" aria-label="Callbacks due">
          <header className="notif-head">
            <strong>Call again</strong>
          </header>

          {count === 0 ? (
            <div className="notif-empty">
              <Phone size={18} />
              <p>No callbacks due</p>
              <span>Candidates marked “Callback” appear here 24 hours later.</span>
            </div>
          ) : (
            <ul className="notif-list">
              {items.map((item) => (
                <li key={item.candidate_id}>
                  <button
                    type="button"
                    className="notif-row is-unread"
                    onClick={() => {
                      setOpen(false);
                      onOpenCandidate?.(item.candidate_id);
                    }}
                  >
                    <span className="notif-icon is-callback_due">
                      <PhoneCall size={14} strokeWidth={2.2} />
                    </span>
                    <span className="notif-text">
                      <strong>{item.candidate_name}</strong>
                      <em>
                        Did not pick up
                        {item.callback_marked_at ? ` ${timeAgo(item.callback_marked_at)}` : ""}
                        {item.assigned_staff_name ? ` · ${item.assigned_staff_name}` : ""}
                      </em>
                      {item.phone && (
                        <span className="callback-phone">
                          <Phone size={11} aria-hidden="true" /> {item.phone}
                        </span>
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
