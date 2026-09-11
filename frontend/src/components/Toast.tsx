"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";

export type ToastType = "info" | "success" | "error";

export interface ToastState {
  message: string;
  type: ToastType;
  /** Bumped on every showToast() call so repeated messages re-trigger the animation. */
  key: number;
}

interface ToastProps {
  toast: ToastState | null;
  onDismiss?: () => void;
}

export default function Toast({ toast, onDismiss }: ToastProps) {
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    // Errors remain available until dismissed; never expire while being read
    // with a pointer or keyboard. A new message starts a fresh reading window.
    if (!toast || toast.type === "error" || paused || !onDismiss) return;
    const timer = window.setTimeout(onDismiss, 6000);
    return () => window.clearTimeout(timer);
  }, [toast, paused, onDismiss]);
  const type = toast?.type ?? "info";

  const color =
    type === "success" ? "var(--success-ink)" : type === "error" ? "var(--error-ink)" : "var(--primary)";

  const Icon = type === "success" ? CheckCircle2 : type === "error" ? AlertTriangle : Info;

  return (
    <div
      className={`toast ${toast ? "active" : ""}`}
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={() => setPaused(false)}
    >
      <div className="toast-icon" style={{ color }} aria-hidden="true">
        <Icon size={24} />
      </div>
      <span role="status" aria-live="polite" aria-atomic="true">{toast?.message ?? ""}</span>
      {toast && onDismiss && (
        <button type="button" className="toast-dismiss" aria-label="Dismiss notification" onClick={onDismiss}>
          <X size={18} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}
