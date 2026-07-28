"use client";

import React from "react";
import { Binary, Database, Mail, Play, ShieldCheck } from "lucide-react";

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

  return (
    <div className="tab-content active">
      <div className="glass-card flow-visualizer-container">
        <div className="visualizer-title-desc">
          <h2
            style={{
              fontFamily: "var(--font-outfit), sans-serif",
              fontSize: "1.5rem",
              marginBottom: "0.5rem",
            }}
          >
            Pipeline Automation Flow
          </h2>
          <p>
            This flow maps how resumes are fetched from unread emails, filtered, parsed directly
            using the Veris LLM, and successfully stored in MongoDB.
          </p>
        </div>

        <div className="flow-nodes">
          {nodes.map((node, index) => {
            const Icon = node.icon;
            return (
              <React.Fragment key={node.key}>
                <div className={nodeClass(node.state)}>
                  <Icon size={32} />
                  <span className="flow-node-title">{node.title}</span>
                </div>
                {index < connectors.length && (
                  <div className={`flow-connector ${connectors[index] ? "active" : ""}`}>
                    <div className="connector-pulse"></div>
                  </div>
                )}
              </React.Fragment>
            );
          })}
        </div>

        <button
          className="btn"
          style={{ padding: "1rem 2rem", fontSize: "1.1rem", gap: "0.75rem" }}
          onClick={onTrigger}
          disabled={syncing}
        >
          <Play size={20} />
          <span>{syncing ? "Pipeline Running..." : "Trigger Live Pipeline Run"}</span>
        </button>
      </div>
    </div>
  );
}
