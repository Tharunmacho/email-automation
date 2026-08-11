"use client";

import { useEffect, useState } from "react";
import { AlertCircle, Filter, Lock } from "lucide-react";

import { fetchIngestRules, type IngestRules } from "@/lib/api";

/** One labelled value inside a rules card. */
function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <div className="db-kv-key">{label}</div>
      <div className="db-kv-val">{children}</div>
    </>
  );
}

function Yes({ on, yes = "Enabled", no = "Disabled" }: { on: boolean; yes?: string; no?: string }) {
  return <span className={`db-pill ${on ? "is-verified" : "is-neutral"}`}>{on ? yes : no}</span>;
}

/**
 * The gates an inbound email passes before it becomes a candidate.
 *
 * Read from `/ingest/rules`, which reports the configuration the pipeline is
 * actually running — not a copy of it maintained in the frontend. It is
 * read-only because the values are environment configuration: changing them
 * from a web page would mean the running process and the `.env` that will be
 * used on the next restart disagree.
 *
 * No longer a destination of its own. It renders as a block inside Settings,
 * which is where you go to ask what this deployment is configured to do.
 */
export default function EmailRules() {
  const [rules, setRules] = useState<IngestRules | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    fetchIngestRules().then(
      (data) => {
        if (active) setRules(data);
      },
      (err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : "Could not read the rules.");
      },
    );
    return () => {
      active = false;
    };
  }, []);

  if (error) {
    return (
      <div className="db-empty">
        <AlertCircle size={28} strokeWidth={1.5} />
        <span className="db-empty-title">Could not load the ingestion rules</span>
        <span className="db-empty-sub">{error}</span>
      </div>
    );
  }

  if (!rules) {
    return (
      <div className="db-empty">
        <Filter size={28} strokeWidth={1.5} />
        <span className="db-empty-title">Reading pipeline configuration…</span>
      </div>
    );
  }

  return (
    <>
      <div className="db-split">
        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Source mailbox</h3>
              <p className="db-card-sub">Where candidate email is collected from.</p>
            </div>
            <span className="db-pill is-neutral">
              <Lock size={11} /> Read-only
            </span>
          </header>
          <div className="db-card-body">
            <div className="db-kv">
              <Row label="Provider">{rules.provider === "gmail" ? "Gmail API" : "SMTP / IMAP"}</Row>
              <Row label="Account">{rules.mailbox.account || "Not configured"}</Row>
              <Row label="Credentials">
                <Yes on={rules.mailbox.configured} yes="Configured" no="Missing" />
              </Row>
              <Row label="Inbox folder">{rules.mailbox.inbox_folder}</Row>
              <Row label="Processed folder">{rules.mailbox.processed_folder}</Row>
              <Row label="Deleted folder">{rules.mailbox.deleted_folder}</Row>
              {rules.provider === "gmail" && (
                <Row label="Search query">
                  <span className="db-chip is-mono">{rules.mailbox.gmail_query}</span>
                </Row>
              )}
              <Row label="Auto-reply">
                <Yes on={rules.auto_reply.enabled} />
              </Row>
            </div>
          </div>
        </section>

        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Acceptance gates</h3>
              <p className="db-card-sub">
                Two checks: one before anything is downloaded, one after parsing.
              </p>
            </div>
          </header>
          <div className="db-card-body">
            <div className="db-kv">
              <Row label="Minimum detector score">
                {rules.gates.detector_min_score.toFixed(2)}
              </Row>
              <Row label="Inspect every document">
                <Yes on={rules.gates.inspect_all_documents} yes="On" no="Off" />
              </Row>
              <Row label="Minimum image size">
                {Math.round(rules.gates.min_image_attachment_bytes / 1024)} KB
              </Row>
              <Row label="Minimum ingest confidence">
                {rules.gates.min_ingest_confidence.toFixed(2)}
              </Row>
              <Row label="Extraction model">
                <span className="db-chip is-mono">{rules.extraction.model}</span>
              </Row>
              <Row label="Extraction key">
                <Yes on={rules.extraction.configured} yes="Configured" no="Missing" />
              </Row>
            </div>
          </div>
        </section>
      </div>

      <div className="db-split">
        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Accepted attachments</h3>
              <p className="db-card-sub">
                Filenames are never trusted on their own — these are the types opened and read.
              </p>
            </div>
          </header>
          <div className="db-card-body">
            <div className="db-chips">
              {rules.attachments.accepted_extensions.map((extension) => (
                <span key={extension} className="db-chip is-mono">
                  {extension}
                </span>
              ))}
            </div>
          </div>
        </section>

        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Ignored senders</h3>
              <p className="db-card-sub">
                Mail matching any of these fragments is never treated as a candidate résumé.
              </p>
            </div>
          </header>
          <div className="db-card-body">
            <div className="db-chips">
              {rules.ignored_senders.map((fragment) => (
                <span key={fragment} className="db-chip is-mono">
                  {fragment}
                </span>
              ))}
            </div>
          </div>
        </section>
      </div>

      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">OCR</h3>
            <p className="db-card-sub">
              Applied only to pages with no readable text layer, and only to the pages that hold
              the résumé.
            </p>
          </div>
          <span className={`db-pill ${rules.ocr.provider_configured ? "is-verified" : "is-pending"}`}>
            {rules.ocr.provider_configured ? "Provider configured" : "No provider key"}
          </span>
        </header>
        <div className="db-card-body">
          <div className="db-kv">
            <Row label="Text-layer threshold">{rules.ocr.min_text_chars} characters</Row>
            <Row label="Render resolution">{rules.ocr.dpi} DPI</Row>
            <Row label="Pages per call">{rules.ocr.chunk_pages}</Row>
            <Row label="Maximum pages">{rules.ocr.max_pages}</Row>
            <Row label="Give up after">{rules.ocr.give_up_pages} pages with nothing relevant</Row>
            <Row label="Languages">{rules.ocr.languages}</Row>
          </div>
        </div>
      </section>
    </>
  );
}
