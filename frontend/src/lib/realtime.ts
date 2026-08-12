/**
 * The live connection to `/ws`.
 *
 * One socket per signed-in session, opened by `useRealtime` and shared by
 * whatever is on screen. The server decides what reaches it: a staff member is
 * only ever sent their own allocations, an admin only the SLA alerts, so this
 * module does no filtering of its own and never asks for a channel.
 *
 * Reconnection is the whole reason this is a hook and not a bare `new
 * WebSocket`. A dashboard is left open all day; a laptop sleeps, a proxy times
 * an idle socket out, the API restarts on deploy. Any of those silently ends
 * the connection, and a pulse badge that says "live" over a dead socket is
 * worse than no badge — so the socket is reopened with a backoff and the badge
 * reflects the real state.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE, getToken } from "@/lib/api";
import type { SlaAlert } from "@/types";

export type RealtimeStatus = "connecting" | "live" | "offline";

export interface CandidateAssignedEvent {
  type: "candidate_assigned";
  channel: "staff";
  staff_id: string;
  title: string;
  message: string;
  candidate: {
    id: string;
    full_name?: string | null;
    email?: string | null;
    current_designation?: string | null;
    matched_keywords?: string[];
  };
}

/** The admin's copy of the same moment: something was ingested and placed. */
export interface CandidateIngestedEvent {
  type: "candidate_ingested";
  channel: "admin";
  title: string;
  message: string;
  staff_name?: string | null;
  candidate: {
    id: string;
    full_name?: string | null;
    email?: string | null;
  };
}

export interface SlaAlertEvent {
  type: "sla_alert";
  channel: "admin";
  title: string;
  message: string;
  count: number;
  threshold_hours: number;
  alerts: SlaAlert[];
}

export interface ConnectedEvent {
  type: "connected";
  user_id: string;
  role: string;
  channel: string;
}

export type RealtimeEvent =
  | CandidateAssignedEvent
  | CandidateIngestedEvent
  | SlaAlertEvent
  | ConnectedEvent
  | { type: "pong" };

/** Backoff between reconnection attempts, in milliseconds. */
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;
/** Idle keepalive. Proxies commonly close a socket that has been quiet ~60s. */
const PING_INTERVAL_MS = 25000;

function socketUrl(token: string): string {
  // API_BASE is "" when the frontend is served by the same FastAPI process, so
  // the origin has to come from the page in that case.
  const base = API_BASE || (typeof window !== "undefined" ? window.location.origin : "");
  const url = new URL("/ws", base);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", token);
  return url.toString();
}

/**
 * Open the push channel and hand every event to `onEvent`.
 *
 * `enabled` is what gates it on being signed in — a socket opened without a
 * token is closed by the server immediately and would reconnect in a loop.
 */
export function useRealtime(
  enabled: boolean,
  onEvent: (event: RealtimeEvent) => void,
): { status: RealtimeStatus } {
  // "connecting" is the honest initial value for an enabled hook: the socket is
  // opened on mount. When disabled, the returned status is overridden below
  // rather than stored, so the effect never has to write state synchronously.
  const [status, setStatus] = useState<RealtimeStatus>("connecting");

  // The callback is read through a ref so a caller that passes an inline
  // function does not tear the socket down and rebuild it on every render.
  const handlerRef = useRef(onEvent);
  useEffect(() => {
    handlerRef.current = onEvent;
  }, [onEvent]);

  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(RECONNECT_MIN_MS);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Set on unmount so an in-flight close handler does not schedule a reconnect
  // to a socket nobody is listening to any more.
  const closedRef = useRef(false);

  const cleanup = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    if (pingRef.current) clearInterval(pingRef.current);
    timerRef.current = null;
    pingRef.current = null;
    const socket = socketRef.current;
    socketRef.current = null;
    if (socket) {
      socket.onclose = null;
      socket.onerror = null;
      socket.onmessage = null;
      socket.onopen = null;
      try {
        socket.close();
      } catch {
        // Already closing — nothing to do.
      }
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      cleanup();
      return;
    }

    closedRef.current = false;

    const connect = () => {
      const token = getToken();
      if (!token || closedRef.current) {
        setStatus("offline");
        return;
      }

      let socket: WebSocket;
      try {
        socket = new WebSocket(socketUrl(token));
      } catch {
        scheduleReconnect();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        setStatus("live");
        retryRef.current = RECONNECT_MIN_MS;
        pingRef.current = setInterval(() => {
          if (socket.readyState === WebSocket.OPEN) socket.send("ping");
        }, PING_INTERVAL_MS);
      };

      socket.onmessage = (message) => {
        let event: RealtimeEvent;
        try {
          event = JSON.parse(message.data as string);
        } catch {
          return; // Not ours to interpret.
        }
        if (event.type === "pong") return;
        handlerRef.current(event);
      };

      socket.onerror = () => {
        // `onclose` always follows, and that is where the retry lives.
      };

      socket.onclose = (closeEvent) => {
        if (pingRef.current) clearInterval(pingRef.current);
        pingRef.current = null;
        socketRef.current = null;
        setStatus("offline");
        // 4401 is this API's "your token is not valid". Retrying it would spin
        // against a rejection that cannot resolve without a fresh login.
        if (closeEvent.code === 4401 || closedRef.current) return;
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (closedRef.current) return;
      const delay = retryRef.current;
      retryRef.current = Math.min(delay * 2, RECONNECT_MAX_MS);
      setStatus("connecting");
      timerRef.current = setTimeout(connect, delay);
    };

    // Deferred by a tick so the effect body writes no state of its own — the
    // socket's own callbacks are what report status from here on.
    timerRef.current = setTimeout(connect, 0);

    return () => {
      closedRef.current = true;
      cleanup();
    };
  }, [enabled, cleanup]);

  // A disabled hook is offline by definition, so that is derived rather than
  // stored — storing it would mean writing state from the effect that disables it.
  return { status: enabled ? status : "offline" };
}
