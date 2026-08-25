"use client";

import { useMemo, useState } from "react";
import { History, Radio, Users } from "lucide-react";

import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { type CandidateLog } from "@/lib/candidateLog";
import { candidateNameOf, formatDateFull, formatInt, initialsOf, timeAgo } from "@/lib/format";
import type { CandidateRecord } from "@/lib/api";

interface ActivityLogsScreenProps {
  systemLogs: LogEntry[];
  candidateLogs: CandidateLog[];
  candidates: CandidateRecord[];
  onOpenCandidateLogs: (candidate: CandidateRecord) => void;
}

const LEVEL_LABEL: Record<LogEntry["type"], string> = {
  info: "INFO",
  success: "OK",
  warn: "WARN",
  error: "FAIL",
};

type Tab = "system" | "records";

/** Build 24 hourly buckets from log timestamps (system logs) */
function buildHourlyBuckets(logs: LogEntry[]): number[] {
  const buckets = new Array(24).fill(0);
  const now = new Date();
  for (const log of logs) {
    if (!log.time) continue;
    // log.time is a display string like "12:34:56" — use today's date
    const parts = log.time.split(":");
    if (parts.length < 2) continue;
    const hour = parseInt(parts[0], 10);
    if (!Number.isNaN(hour) && hour >= 0 && hour < 24) {
      buckets[hour] += 1;
    }
  }
  return buckets;
}

function HourlyBar({ buckets }: { buckets: number[] }) {
  const max = Math.max(...buckets, 1);
  const hours = Array.from({ length: 24 }, (_, i) => i);
  return (
    <div className="act-bar-chart">
      {hours.map((h) => {
        const val = buckets[h] ?? 0;
        const pct = (val / max) * 100;
        return (
          <div key={h} className="act-bar-col" title={`${String(h).padStart(2, "0")}:00 — ${val} event${val === 1 ? "" : "s"}`}>
            <div className="act-bar-fill" style={{ height: `${Math.max(pct, val > 0 ? 4 : 0)}%` }} />
            {h % 6 === 0 && <span className="act-bar-label">{String(h).padStart(2, "0")}</span>}
          </div>
        );
      })}
    </div>
  );
}

export default function ActivityLogsScreen({
  systemLogs,
  candidateLogs,
  candidates,
  onOpenCandidateLogs,
}: ActivityLogsScreenProps) {
  const [tab, setTab] = useState<Tab>("system");

  const touched = useMemo(() => {
    const latest = new Map<string, { count: number; at: string }>();
    for (const entry of candidateLogs) {
      const current = latest.get(entry.candidateId);
      latest.set(entry.candidateId, {
        count: (current?.count ?? 0) + 1,
        at: !current || entry.at > current.at ? entry.at : current.at,
      });
    }
    return candidates
      .filter((candidate) => latest.has(candidate.id))
      .map((candidate) => ({ candidate, ...latest.get(candidate.id)! }))
      .sort((a, b) => b.at.localeCompare(a.at));
  }, [candidateLogs, candidates]);

  /* ── KPI counts from live logs ── */
  const errorCount   = systemLogs.filter((l) => l.type === "error").length;
  const warnCount    = systemLogs.filter((l) => l.type === "warn").length;
  const hourlyBuckets = useMemo(() => buildHourlyBuckets(systemLogs), [systemLogs]);

  return (
    <>
      {/* ── Hourly event bar chart ── */}
      {systemLogs.length > 0 && (
        <section className="db-card">
          <div className="db-card-head">
            <div>
              <h2 className="db-card-title">Events by Hour</h2>
              <p className="db-card-sub">
                Pipeline activity distribution (today, by hour)
              </p>
            </div>
          </div>
          <div className="db-card-body" style={{ paddingBottom: "1.5rem" }}>
            <HourlyBar buckets={hourlyBuckets} />
          </div>
        </section>
      )}

      {/* ── Tab switcher ── */}
      <div className="ds-head is-compact">
        <div className="ds-seg" role="tablist" aria-label="Log source">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "system"}
            className={`ds-seg-btn ${tab === "system" ? "is-on" : ""}`}
            onClick={() => setTab("system")}
          >
            <Radio size={13} /> Pipeline trace
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "records"}
            className={`ds-seg-btn ${tab === "records" ? "is-on" : ""}`}
            onClick={() => setTab("records")}
          >
            <Users size={13} /> Record history
          </button>
        </div>

        {/* Only the states that have something in them. A row of zeros is four
            findings the reader has to check to learn nothing happened. */}
        <p className="ds-head-sub">
          {systemLogs.length === 0
            ? "No pipeline events this session."
            : [
                `${formatInt(systemLogs.length)} event${systemLogs.length === 1 ? "" : "s"}`,
                warnCount > 0 ? `${formatInt(warnCount)} warning${warnCount === 1 ? "" : "s"}` : null,
                errorCount > 0 ? `${formatInt(errorCount)} error${errorCount === 1 ? "" : "s"}` : null,
              ]
                .filter(Boolean)
                .join(" · ")}
        </p>
      </div>

      {tab === "system" ? (
        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Pipeline trace</h3>
              <p className="db-card-sub">
                Ingestion events from this session. Resets when the page is reloaded.
              </p>
            </div>
            <span className="db-pill is-info">{formatInt(systemLogs.length)} events</span>
          </header>
          <div className="db-card-body">
            {systemLogs.length === 0 ? (
              <div className="db-empty" style={{ border: "none", padding: "2rem 0" }}>
                <Radio size={26} strokeWidth={1.5} />
                <span className="db-empty-title">Nothing traced yet</span>
                <span className="db-empty-sub">Run a Gmail sync to see the pipeline report in.</span>
              </div>
            ) : (
              <div className="dash-log-scroll" style={{ maxHeight: "none" }}>
                {[...systemLogs].reverse().map((log, index) => (
                  <div key={`${log.time}-${index}`} className="dash-log-row">
                    <span className="dash-log-time">{log.time}</span>
                    <span className={`dash-log-level dash-log-${log.type}`}>
                      {LEVEL_LABEL[log.type]}
                    </span>
                    <span className="dash-log-message">{log.message}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>
      ) : (
        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Record history</h3>
              <p className="db-card-sub">
                Candidates that have been viewed, edited or verified. Open one for its full trail.
              </p>
            </div>
            <span className="db-pill is-info">{formatInt(candidateLogs.length)} events</span>
          </header>
          <div className="db-card-body">
            {touched.length === 0 ? (
              <div className="db-empty" style={{ border: "none", padding: "2rem 0" }}>
                <History size={26} strokeWidth={1.5} />
                <span className="db-empty-title">No record changes yet</span>
                <span className="db-empty-sub">
                  Viewing, editing, verifying or deleting a profile is recorded against it.
                </span>
              </div>
            ) : (
              <div className="clog-days">
                {touched.map(({ candidate, count, at }) => (
                  <button
                    key={candidate.id}
                    type="button"
                    className="ov-action"
                    style={{ width: "100%" }}
                    onClick={() => onOpenCandidateLogs(candidate)}
                  >
                    <span className="cprof-monogram" style={{ width: 38, height: 38, fontSize: "0.78rem" }}>
                      {initialsOf(candidateNameOf(candidate))}
                    </span>
                    <span className="ov-action-text" style={{ flex: 1 }}>
                      <span className="ov-action-title">{candidateNameOf(candidate)}</span>
                      <span className="ov-action-sub">
                        {formatInt(count)} event{count === 1 ? "" : "s"} · last {timeAgo(at)} ·{" "}
                        {formatDateFull(new Date(at))}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </section>
      )}
    </>
  );
}
