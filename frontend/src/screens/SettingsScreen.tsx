"use client";

import { useEffect, useMemo, useState } from "react";
import { AtSign, LogOut, Mail, Phone, ScanText, ShieldCheck, UserRound } from "lucide-react";

import {
  fetchIngestRules,
  listUsersAPI,
  type AuthUser,
  type IngestRules,
  type ManagedUser,
} from "@/lib/api";

interface SettingsScreenProps {
  user: AuthUser;
  onSignOut: () => void;
}

interface AdminConfiguration {
  rules: IngestRules;
  users: ManagedUser[];
}

type AdminProbe =
  | { state: "loading" }
  | { state: "ready"; value: AdminConfiguration }
  | { state: "error"; message: string };

/**
 * Settings is deliberately small: everybody can confirm their own account,
 * while configuration visibility is restricted to an administrator. Secrets
 * never reach this screen; the API reports addresses, phone numbers and
 * whether the Veris key exists, never passwords or keys themselves.
 */
export default function SettingsScreen({ user, onSignOut }: SettingsScreenProps) {
  const isAdmin = user.role === "admin";
  const [adminConfig, setAdminConfig] = useState<AdminProbe>({ state: "loading" });

  useEffect(() => {
    if (!isAdmin) return;
    let active = true;
    Promise.all([fetchIngestRules(), listUsersAPI()]).then(
      ([rules, users]) => {
        if (active) setAdminConfig({ state: "ready", value: { rules, users: users.items ?? [] } });
      },
      (error: unknown) => {
        if (active) {
          setAdminConfig({
            state: "error",
            message: error instanceof Error ? error.message : "Could not load configuration.",
          });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [isAdmin]);

  const configuredEmails = useMemo(() => {
    if (adminConfig.state !== "ready") return [];
    const entries: { id: string; label: string; value: string; active: boolean }[] = [];
    const mailbox = adminConfig.value.rules.mailbox.account?.trim();
    if (mailbox) {
      entries.push({ id: `mailbox-${mailbox}`, label: "Recruitment mailbox", value: mailbox, active: true });
    }
    for (const account of adminConfig.value.users) {
      if (!account.email || entries.some((entry) => entry.value.toLowerCase() === account.email.toLowerCase())) continue;
      entries.push({
        id: account.id,
        label: account.role === "admin" ? "Admin account" : "Staff account",
        value: account.email,
        active: account.active,
      });
    }
    return entries;
  }, [adminConfig]);

  const configuredMobiles = useMemo(() => {
    if (adminConfig.state !== "ready") return [];
    return adminConfig.value.users
      .filter((account) => Boolean(account.phone?.trim()))
      .map((account) => ({
        id: account.id,
        label: account.name || account.email,
        value: account.phone!.trim(),
        active: account.active,
      }));
  }, [adminConfig]);

  return (
    <div className="settings-simple">
      <section className="ds-panel settings-account" aria-labelledby="settings-account-title">
        <div className="ds-panel-head is-split">
          <div>
            <h2 id="settings-account-title" className="ds-panel-title">Account details</h2>
            <p className="ds-panel-sub">The identity currently signed in to this workspace.</p>
          </div>
          <span className="settings-account-icon" aria-hidden="true"><UserRound size={19} /></span>
        </div>

        <dl className="settings-account-grid">
          <div>
            <dt>Name</dt>
            <dd>{user.name || "Not provided"}</dd>
          </div>
          <div>
            <dt>Email address</dt>
            <dd><AtSign size={14} /> {user.email}</dd>
          </div>
          <div>
            <dt>Mobile number</dt>
            <dd><Phone size={14} /> {user.phone || "Not configured"}</dd>
          </div>
          <div>
            <dt>Access level</dt>
            <dd><ShieldCheck size={14} /> {isAdmin ? "Administrator" : "Staff"}</dd>
          </div>
        </dl>

        <div className="settings-account-foot">
          <span>Account details are managed by an administrator.</span>
          <button type="button" className="db-btn is-danger" onClick={onSignOut}>
            <LogOut size={14} /> Sign out
          </button>
        </div>
      </section>

      {isAdmin && (
        <section className="ds-panel settings-config" aria-labelledby="settings-config-title">
          <div className="ds-panel-head is-split">
            <div>
              <h2 id="settings-config-title" className="ds-panel-title">Configured communication</h2>
              <p className="ds-panel-sub">Read-only addresses, mobile contacts, and OCR source currently in use.</p>
            </div>
            <span className="db-pill is-info">Admin only</span>
          </div>

          {adminConfig.state === "loading" ? (
            <div className="settings-config-state"><span className="app-boot-spinner" /> Loading configuration…</div>
          ) : adminConfig.state === "error" ? (
            <div className="settings-config-state is-error">{adminConfig.message}</div>
          ) : (
            <div className="settings-config-grid">
              <section className="settings-config-block">
                <div className="settings-config-head"><Mail size={16} /><h3>Configured emails</h3></div>
                <div className="settings-config-list">
                  {configuredEmails.length ? configuredEmails.map((entry) => (
                    <div className="settings-config-row" key={entry.id}>
                      <span><strong>{entry.value}</strong><small>{entry.label}</small></span>
                      <span className={`ds-status ${entry.active ? "is-ok" : "is-neutral"}`}>
                        <i aria-hidden="true" />{entry.active ? "Active" : "Inactive"}
                      </span>
                    </div>
                  )) : <p className="settings-empty">No email address is configured.</p>}
                </div>
              </section>

              <section className="settings-config-block">
                <div className="settings-config-head"><Phone size={16} /><h3>Configured mobile numbers</h3></div>
                <div className="settings-config-list">
                  {configuredMobiles.length ? configuredMobiles.map((entry) => (
                    <div className="settings-config-row" key={entry.id}>
                      <span><strong>{entry.value}</strong><small>{entry.label}</small></span>
                      <span className={`ds-status ${entry.active ? "is-ok" : "is-neutral"}`}>
                        <i aria-hidden="true" />{entry.active ? "Active" : "Inactive"}
                      </span>
                    </div>
                  )) : <p className="settings-empty">No mobile number is configured.</p>}
                </div>
              </section>

              <section className="settings-config-block is-ocr">
                <div className="settings-config-head"><ScanText size={16} /><h3>OCR source</h3></div>
                <div className="settings-ocr-source">
                  <span className="settings-ocr-mark"><ScanText size={20} /></span>
                  <span><strong>{adminConfig.value.rules.ocr.provider}</strong><small>Document OCR and scanned résumé extraction</small></span>
                  <span className={`db-pill ${adminConfig.value.rules.ocr.provider_configured ? "is-verified" : "is-pending"}`}>
                    {adminConfig.value.rules.ocr.provider_configured ? "Configured" : "API key required"}
                  </span>
                </div>
              </section>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
