"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { LogOut, Moon, Sun } from "lucide-react";

import {
  API_BASE,
  fetchHealth,
  fetchIngestRules,
  fetchWorkerStatus,
  type AuthUser,
  type IngestRules,
} from "@/lib/api";
import {
  getThemeServerSnapshot,
  getThemeSnapshot,
  setTheme,
  subscribeTheme,
  type Theme,
} from "@/lib/theme";
import { formatInt } from "@/lib/format";
import EmailRules from "@/screens/EmailRulesScreen";

interface SettingsScreenProps {
  user: AuthUser;
  onSignOut: () => void;
}

type Probe<T> = { state: "loading" } | { state: "ok"; value: T } | { state: "error"; message: string };

function StatusPill({ probe, okLabel }: { probe: Probe<unknown>; okLabel: string }) {
  if (probe.state === "loading") return <span className="db-pill is-neutral">Checking…</span>;
  if (probe.state === "error") return <span className="db-pill is-failed">Unreachable</span>;
  return <span className="db-pill is-verified">{okLabel}</span>;
}

/**
 * Session, appearance and the state of the services this workspace depends on.
 *
 * The service rows are live probes rather than stored values: the question this
 * screen answers is "is it working right now", which a cached answer cannot
 * report.
 */
export default function SettingsScreen({ user, onSignOut }: SettingsScreenProps) {
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeServerSnapshot);

  const [health, setHealth] = useState<Probe<{ status: string; candidates: number }>>({
    state: "loading",
  });
  const [workers, setWorkers] = useState<Probe<{ available: boolean }>>({ state: "loading" });
  const [rules, setRules] = useState<Probe<IngestRules>>({ state: "loading" });

  useEffect(() => {
    let active = true;
    const settle = <T,>(setter: (p: Probe<T>) => void) => [
      (value: T) => {
        if (active) setter({ state: "ok", value });
      },
      (err: unknown) => {
        if (active) {
          setter({ state: "error", message: err instanceof Error ? err.message : "Failed" });
        }
      },
    ] as const;

    const [okHealth, failHealth] = settle(setHealth);
    fetchHealth().then(okHealth, failHealth);
    const [okWorkers, failWorkers] = settle(setWorkers);
    fetchWorkerStatus().then(okWorkers, failWorkers);
    const [okRules, failRules] = settle(setRules);
    fetchIngestRules().then(okRules, failRules);

    return () => {
      active = false;
    };
  }, []);

  const THEMES: { id: Theme; label: string; icon: typeof Sun }[] = [
    { id: "light", label: "Light", icon: Sun },
    { id: "dark", label: "Dark", icon: Moon },
  ];

  return (
    <>
      <div className="db-split">
        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Account</h3>
              <p className="db-card-sub">The session this browser is signed in with.</p>
            </div>
          </header>
          <div className="db-card-body">
            <div className="db-kv">
              <div className="db-kv-key">Name</div>
              <div className="db-kv-val">{user.name || "—"}</div>
              <div className="db-kv-key">Email</div>
              <div className="db-kv-val">{user.email}</div>
              <div className="db-kv-key">Role</div>
              <div className="db-kv-val">
                <span className="db-pill is-info">{user.role || "user"}</span>
              </div>
            </div>
            <button
              type="button"
              className="db-btn is-danger"
              style={{ marginTop: "1.1rem" }}
              onClick={onSignOut}
            >
              <LogOut size={15} /> Sign out
            </button>
          </div>
        </section>

        <section className="db-card">
          <header className="db-card-head">
            <div>
              <h3 className="db-card-title">Appearance</h3>
              <p className="db-card-sub">Applies to this browser only, and is remembered.</p>
            </div>
          </header>
          <div className="db-card-body">
            <div className="theme-switch" style={{ maxWidth: "260px" }} role="group" aria-label="Colour theme">
              {THEMES.map(({ id, label, icon: Icon }) => (
                <button
                  key={id}
                  type="button"
                  className={`theme-switch-btn ${theme === id ? "is-on" : ""}`}
                  onClick={() => setTheme(id)}
                  aria-pressed={theme === id}
                >
                  <Icon size={13} /> {label}
                </button>
              ))}
            </div>
          </div>
        </section>
      </div>

      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">Services</h3>
            <p className="db-card-sub">Checked live, each time this screen is opened.</p>
          </div>
        </header>
        <div className="db-card-body">
          <div className="db-kv">
            <div className="db-kv-key">API</div>
            <div className="db-kv-val">
              <StatusPill probe={health} okLabel="Reachable" />{" "}
              <span style={{ fontWeight: 400, color: "var(--text-muted)" }}>
                {API_BASE || "same origin"}
              </span>
            </div>

            <div className="db-kv-key">Records in database</div>
            <div className="db-kv-val">
              {health.state === "ok" ? formatInt(health.value.candidates) : "—"}
            </div>

            <div className="db-kv-key">Background worker</div>
            <div className="db-kv-val">
              {workers.state === "ok" ? (
                <span className={`db-pill ${workers.value.available ? "is-verified" : "is-pending"}`}>
                  {workers.value.available ? "Online" : "Offline — syncs run inline"}
                </span>
              ) : (
                <StatusPill probe={workers} okLabel="Online" />
              )}
            </div>

            <div className="db-kv-key">Mailbox</div>
            <div className="db-kv-val">
              {rules.state === "ok" ? (
                <span className={`db-pill ${rules.value.mailbox.configured ? "is-verified" : "is-pending"}`}>
                  {rules.value.mailbox.configured ? rules.value.mailbox.account : "Not configured"}
                </span>
              ) : (
                <StatusPill probe={rules} okLabel="Configured" />
              )}
            </div>

            <div className="db-kv-key">Extraction model</div>
            <div className="db-kv-val">
              {rules.state === "ok" ? (
                <span className="db-chip is-mono">{rules.value.extraction.model}</span>
              ) : (
                "—"
              )}
            </div>

            <div className="db-kv-key">OCR provider</div>
            <div className="db-kv-val">
              {rules.state === "ok" ? (
                <span
                  className={`db-pill ${rules.value.ocr.provider_configured ? "is-verified" : "is-pending"}`}
                >
                  {rules.value.ocr.provider_configured ? "Configured" : "No key"}
                </span>
              ) : (
                "—"
              )}
            </div>
          </div>

          {/* Configuration is environment-driven. Saying so here stops this
              screen reading as a settings form that has lost its Save button. */}
          <p className="db-card-sub" style={{ marginTop: "1.1rem" }}>
            Pipeline configuration is set from the server environment and is read-only here. The
            values currently in force are below.
          </p>
        </div>
      </section>

      <div>
        <h2 className="db-label" style={{ display: "block", marginBottom: "0.7rem" }}>
          Ingestion rules
        </h2>
        <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
          <EmailRules />
        </div>
      </div>
    </>
  );
}
