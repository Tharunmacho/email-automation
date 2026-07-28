"use client";

import React, { useEffect, useRef } from "react";
import { Terminal } from "lucide-react";

export interface LogEntry {
  time: string;
  type: "info" | "success" | "warn" | "error";
  message: string;
}

interface LogsConsoleProps {
  logs: LogEntry[];
}

export default function LogsConsole({ logs }: LogsConsoleProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [logs]);

  const getTypeClass = (type: LogEntry["type"]) => {
    switch (type) {
      case "success": return "log-success";
      case "warn": return "log-warn";
      case "error": return "log-error";
      default: return "log-info";
    }
  };

  return (
    <div className="glass-card">
      <h2 className="section-title">
        <Terminal size={20} /> System Activity Logs
      </h2>
      <div className="terminal-container" ref={containerRef}>
        <div className="terminal-header">
          <div className="terminal-dots">
            <span className="terminal-dot" style={{ backgroundColor: "#ef4444" }}></span>
            <span className="terminal-dot" style={{ backgroundColor: "#eab308" }}></span>
            <span className="terminal-dot" style={{ backgroundColor: "#22c55e" }}></span>
          </div>
          <span>ingestion_engine.log</span>
        </div>
        <div className="terminal-output" id="terminal-logs">
          {logs.map((log, index) => (
            <div key={index} className="log-entry">
              <span className="log-time">[{log.time}]</span>
              <span className={getTypeClass(log.type)}>
                [{log.type.toUpperCase()}]
              </span>{" "}
              {log.message}
            </div>
          ))}
          {logs.length === 0 && (
            <div className="log-entry" style={{ color: "var(--text-muted)" }}>
              Ready to listen for synchronization updates...
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
