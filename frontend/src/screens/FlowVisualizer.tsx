"use client";

import React from "react";
import { Binary, Check, Database, Loader2, Mail, Play, ShieldCheck } from "lucide-react";

export type NodeState = "idle" | "active" | "success";

export interface FlowState {
  gmail: NodeState;
  filter: NodeState;
  veris: NodeState;
  db: NodeState;
  connGmailFilter: boolean;
  connFilterVeris: boolean;
  connVerisDb: boolean;
}

export const IDLE_FLOW: FlowState = {
  gmail: "idle",
  filter: "idle",
  veris: "idle",
  db: "idle",
  connGmailFilter: false,
  connFilterVeris: false,
  connVerisDb: false,
};

interface FlowVisualizerProps {
  flow: FlowState;
  syncing: boolean;
  onTrigger: () => void;
}

function nodeClass(state: NodeState): string {
  if (state === "active") return "flow-node node-active";
  if (state === "success") return "flow-node node-success";
  return "flow-node";
}

export default function FlowVisualizer({ flow, syncing, onTrigger }: FlowVisualizerProps) {
  const nodes = [
    { key: "gmail", icon: Mail, title: "Gmail Listener", state: flow.gmail },
    { key: "filter", icon: ShieldCheck, title: "Score Filters", state: flow.filter },
    { key: "veris", icon: Binary, title: "Veris LLM Parser", state: flow.veris },
    { key: "db", icon: Database, title: "MongoDB Atlas", state: flow.db },
  ] as const;

  const connectors = [flow.connGmailFilter, flow.connFilterVeris, flow.connVerisDb];

  // Which stage the run is on, for the compact readout that replaces the
  // diagram's left-to-right reading when it stacks.
  const activeIndex = nodes.findIndex((node) => node.state === "active");
  const doneCount = nodes.filter((node) => node.state === "success").length;
  const stageLabel = syncing
    ? activeIndex >= 0
      ? `Step ${activeIndex + 1} of ${nodes.length} — ${nodes[activeIndex].title}`
      : `${doneCount} of ${nodes.length} stages complete`
    : doneCount === nodes.length
    ? "Last run completed all four stages"
    : "Idle — waiting for a run";

  return (
    <div className="tab-content active">
      <section className="glass-card flow-card">
        <header className="flow-head">
          <h2 className="flow-title">Pipeline automation flow</h2>
          <p className="flow-sub">
            How resumes travel from an unread email, through the score filters and the Veris
            LLM parser, into MongoDB.
          </p>
        </header>

        <div className="flow-nodes">
          {nodes.map((node, index) => {
            const Icon = node.icon;
            return (
              <React.Fragment key={node.key}>
                <div className={nodeClass(node.state)}>
                  <span className="flow-node-step">
                    {node.state === "success" ? <Check size={13} strokeWidth={3} /> : index + 1}
                  </span>
                  <span className="flow-node-icon">
                    <Icon size={26} />
                  </span>
                  <span className="flow-node-title">{node.title}</span>
                </div>
                {index < connectors.length && (
                  <div
                    className={`flow-connector ${connectors[index] ? "active" : ""}`}
                    aria-hidden="true"
                  >
                    <span className="connector-pulse" />
                  </div>
                )}
              </React.Fragment>
            );
          })}
        </div>

        <p className={`flow-stage ${syncing ? "is-running" : ""}`} aria-live="polite">
          {syncing && <span className="flow-stage-dot" />}
          {stageLabel}
        </p>

        {/* One control, two labels: the phone gets a button it can actually fit
            rather than a shrunken copy of the desktop one. */}
        <button className="flow-cta" onClick={onTrigger} disabled={syncing}>
          {syncing ? <Loader2 size={18} className="icon-spin" /> : <Play size={18} />}
          <span className="flow-cta-full">
            {syncing ? "Pipeline running…" : "Trigger live pipeline run"}
          </span>
          <span className="flow-cta-short">{syncing ? "Running…" : "Run pipeline"}</span>
        </button>
      </section>
    </div>
  );
}
