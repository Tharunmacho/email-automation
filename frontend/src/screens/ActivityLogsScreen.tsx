"use client";

import { useMemo, useState } from "react";
import { History, Radio, Users } from "lucide-react";

import type { LogEntry } from "@/components/dashboard/ActivityLog";
import { type CandidateLog } from "@/lib/candidateLog";
import { candidateNameOf, formatDateFull, formatInt, initialsOf, timeAgo } from "@/lib/format";
import type { CandidateRecord } from "@/lib/api";

interface ActivityLogsScreenProps {
  /** The pipeline trace — sync cycles, connection events, record counts. */
  systemLogs: LogEntry[];
  /** Every per-candidate event, across all candidates. */
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

/**
 * The System destination for both logs the app keeps.
 *
 * They are genuinely different things and stay on separate tabs: the pipeline
 * trace is this session's run history and resets with the process, while the
 * record trail is per-candidate, persisted, and survives a reload. Merging them
 * would produce one stream where half the entries vanish on refresh.
 */
export default function ActivityLogsScreen({
  systemLogs,
  candidateLogs,
  candidates,
  onOpenCandidateLogs,
}: ActivityLogsScreenProps) {
  const [tab, setTab] = useState<Tab>("system");

  /** Candidates that have history, most recently touched first. */
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

  return (
    <>
      <div className="db-card" style={{ padding: "0.35rem", display: "inline-flex", gap: "0.2rem", width: "fit-content" }}>
        <button
          type="button"
          className={`theme-switch-btn ${tab === "system" ? "is-on" : ""}`}
          style={{ padding: "0.45rem 0.9rem", borderRadius: "var(--radius-sm)" }}
          onClick={() => setTab("system")}
        >
          <Radio size={13} /> Pipeline trace
        </button>
        <button
          type="button"
          className={`theme-switch-btn ${tab === "records" ? "is-on" : ""}`}
          style={{ padding: "0.45rem 0.9rem", borderRadius: "var(--radius-sm)" }}
          onClick={() => setTab("records")}
        >
          <Users size={13} /> Record history
        </button>
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
