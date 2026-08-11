"use client";

import { useEffect, useState } from "react";
import { Inbox, RefreshCw } from "lucide-react";

import FlowVisualizer, { type FlowState } from "@/screens/FlowVisualizer";
import ActivityLog, { type LogEntry } from "@/components/dashboard/ActivityLog";
import { fetchIngestRules, type IngestRules, type PollSummary } from "@/lib/api";
import { formatInt, timeAgo } from "@/lib/format";

interface PipelineScreenProps {
  flow: FlowState;
  logs: LogEntry[];
  syncing: boolean;
  lastSyncAt: string | null;
  lastSummary: PollSummary | null;
  workersOnline: boolean | null;
  onSync: () => void;
}

/**
 * Gmail → AI extraction → MongoDB, as a diagram and as a result.
 *
 * Gmail Sync lost its own rail entry, and this is where it went. The two were
 * always halves of the same question — the diagram shows the run in flight, the
 * cards under it show what that run actually did — and splitting them meant
 * watching a sync on one screen and finding its counts on another.
 */
export default function PipelineScreen({
  flow,
  logs,
  syncing,
  lastSyncAt,
  lastSummary,
  workersOnline,
  onSync,
}: PipelineScreenProps) {
  const [rules, setRules] = useState<IngestRules | null>(null);

  useEffect(() => {
    let active = true;
    fetchIngestRules().then(
      (data) => {
        if (active) setRules(data);
      },
      () => {
        // The screen still works without it — the connection card reports what
        // it could not read rather than blocking the trigger.
      },
    );
    return () => {
      active = false;
    };
  }, []);

  const counts = lastSummary
    ? [
        { label: "Fetched", value: lastSummary.fetched },
        { label: "Ingested", value: lastSummary.ingested_candidates },
        { label: "Skipped", value: lastSummary.skipped },
        { label: "Errors", value: lastSummary.errors },
      ]
    : [];

  return (
    <>
      <FlowVisualizer flow={flow} syncing={syncing} onTrigger={onSync} />

      <div className="db-split">
        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Source connection</h3>
              <p className="db-card-sub">The mailbox the first stage draws from.</p>
            </div>
            <span className={`db-pill ${rules?.mailbox.configured ? "is-verified" : "is-pending"}`}>
              {rules?.mailbox.configured ? "Connected" : "Not configured"}
            </span>
          </header>
          <div className="db-card-body">
            <div className="db-kv">
              <div className="db-kv-key">Account</div>
              <div className="db-kv-val">{rules?.mailbox.account || "—"}</div>
              <div className="db-kv-key">Protocol</div>
              <div className="db-kv-val">
                {rules ? (rules.provider === "gmail" ? "Gmail API" : "IMAP") : "—"}
              </div>
              <div className="db-kv-key">Watched folder</div>
              <div className="db-kv-val">{rules?.mailbox.inbox_folder || "—"}</div>
              <div className="db-kv-key">Filed after ingest</div>
              <div className="db-kv-val">{rules?.mailbox.processed_folder || "—"}</div>
              <div className="db-kv-key">Background worker</div>
              <div className="db-kv-val">
                {workersOnline === null
                  ? "Unknown"
                  : workersOnline
                    ? "Online — cycles run off the request path"
                    : "Offline — cycles run inline"}
              </div>
              <div className="db-kv-key">Last run</div>
              <div className="db-kv-val">
                {lastSyncAt ? timeAgo(lastSyncAt) : "Not yet this session"}
              </div>
            </div>

            <button
              type="button"
              className="db-btn is-primary"
              style={{ marginTop: "1.1rem" }}
              onClick={onSync}
              disabled={syncing}
            >
              <RefreshCw size={15} className={syncing ? "icon-spin" : undefined} />
              {syncing ? "Running…" : "Run pipeline now"}
            </button>
          </div>
        </section>

        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Last cycle</h3>
              <p className="db-card-sub">
                {lastSummary
                  ? "What the most recent run did."
                  : "No cycle has run in this session yet."}
              </p>
            </div>
          </header>
          <div className="db-card-body">
            {lastSummary ? (
              <>
                <div className="ov-kpis" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
                  {counts.map((count) => (
                    <article key={count.label} className="ov-kpi">
                      <p className="ov-kpi-label">{count.label}</p>
                      <p className="ov-kpi-value" style={{ fontSize: "1.5rem" }}>
                        {formatInt(count.value)}
                      </p>
                    </article>
                  ))}
                </div>
                {lastSummary.skipped_reason && (
                  <p className="db-card-sub" style={{ marginTop: "0.9rem" }}>
                    {lastSummary.skipped_reason}
                  </p>
                )}
              </>
            ) : (
              <div className="db-empty" style={{ border: "none", padding: "1.5rem 0" }}>
                <Inbox size={26} strokeWidth={1.5} />
                <span className="db-empty-title">Nothing run yet</span>
                <span className="db-empty-sub">
                  Trigger the pipeline and the counts from that cycle appear here.
                </span>
              </div>
            )}
          </div>
        </section>
      </div>

      <ActivityLog logs={logs} />
    </>
  );
}
