"use client";

import React from "react";
import { AlertTriangle, CheckCircle2, Info } from "lucide-react";

export type ToastType = "info" | "success" | "error";

export interface ToastState {
  message: string;
  type: ToastType;
  /** Bumped on every showToast() call so repeated messages re-trigger the animation. */
  key: number;
}

interface ToastProps {
  toast: ToastState | null;
}

export default function Toast({ toast }: ToastProps) {
  const type = toast?.type ?? "info";

  const color =
    type === "success" ? "var(--success)" : type === "error" ? "var(--error)" : "var(--primary)";

  const Icon = type === "success" ? CheckCircle2 : type === "error" ? AlertTriangle : Info;

  return (
    <div className={`toast ${toast ? "active" : ""}`} role="status" aria-live="polite">
      <div className="toast-icon" style={{ color }}>
        <Icon size={24} />
      </div>
      <span>{toast?.message ?? ""}</span>
    </div>
  );
}
