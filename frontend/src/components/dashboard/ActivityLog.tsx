"use client";

import React, { useEffect, useRef } from "react";
import { Radio } from "lucide-react";

export interface LogEntry {
  time: string;
  type: "info" | "success" | "warn" | "error";
  message: string;
}

interface ActivityLogProps {
  logs: LogEntry[];
}

const LEVEL_LABEL: Record<LogEntry["type"], string> = {
  info: "INFO",
  success: "OK",
  warn: "WARN",
  error: "FAIL",
};

export default function ActivityLog({ logs }: ActivityLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [logs]);

  return (
    <section className="dash-card dash-log-card">
      <header className="dash-card-head">
        <div>
          <h3 className="dash-card-title">
            <Radio size={17} strokeWidth={2.2} /> System activity
          </h3>
          <p className="dash-card-sub">Live trace from the ingestion engine</p>
        </div>
        <span className="dash-live">
          <i className="dash-live-dot" />
          {logs.length} event{logs.length === 1 ? "" : "s"}
        </span>
      </header>

      <div className="dash-log-scroll" ref={scrollRef}>
        {logs.length === 0 ? (
          <p className="dash-empty">Waiting for synchronization updates…</p>
        ) : (
          logs.map((log, index) => (
            <div key={`${log.time}-${index}`} className="dash-log-row">
              <span className="dash-log-time">{log.time}</span>
              <span className={`dash-log-level dash-log-${log.type}`}>{LEVEL_LABEL[log.type]}</span>
              <span className="dash-log-message">{log.message}</span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
