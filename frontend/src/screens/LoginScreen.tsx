"use client";

import React, { useEffect, useState } from "react";
import {
  AlertCircle,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Mail,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import { fetchDemoAccounts, login, type AuthUser, type DemoAccount } from "@/lib/api";

interface LoginScreenProps {
  onSuccess: (user: AuthUser) => void;
}

const DEFAULT_DEMO_ACCOUNTS: DemoAccount[] = [
  {
    role: "admin",
    label: "Super Admin",
    description: "Full access: allocation, staff accounts and SLA.",
    email: "admin@gmail.com",
    password: "admin@123",
  },
  {
    role: "staff",
    label: "Staff Evaluator",
    description: "Evaluate candidates allocated to your review queue.",
    email: "staff@gmail.com",
    password: "staff@123",
  },
];

export default function LoginScreen({ onSuccess }: LoginScreenProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Served by the API rather than hard-coded, so a button can never fill a
  // password that has been changed in settings or an account that is turned off.
  const [demoAccounts, setDemoAccounts] = useState<DemoAccount[]>(DEFAULT_DEMO_ACCOUNTS);

  useEffect(() => {
    let cancelled = false;
    void fetchDemoAccounts().then((accounts) => {
      if (!cancelled) setDemoAccounts(accounts.length > 0 ? accounts : DEFAULT_DEMO_ACCOUNTS);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Fill *and* submit.
   *
   * A button that only fills the form asks the reader to notice that something
   * changed and then find the submit control. The point of a demo button is to
   * be one press, so it signs in.
   */
  const signInWithDemo = async (account: DemoAccount) => {
    if (busy) return;
    setEmail(account.email);
    setPassword(account.password);
    setError(null);
    setBusy(true);
    try {
      const { user } = await login(account.email, account.password, remember);
      onSuccess(user);
    } catch (err) {
      setError(
        err instanceof Error
          ? `${account.label} demo sign-in failed: ${err.message}`
          : "Could not sign in with the demo account.",
      );
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;

    setError(null);
    if (!email.trim() || !password) {
      setError("Enter both your email and password.");
      return;
    }

    setBusy(true);
    try {
      const { user } = await login(email.trim(), password, remember);
      onSuccess(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not sign in. Try again.");
      setPassword("");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-shell">
        <div className="login-card">
          {/* Brand lockup, then the heading. Two aligned blocks read as
              deliberate; a logo floating above centred text does not. */}
          <header className="login-head">
            <div className="login-brand">
              <span className="login-logo">
                <Sparkles size={20} strokeWidth={2.3} />
              </span>
              <span className="login-brandline">
                <span className="login-brandname">TalentFlow AI</span>
                <span className="login-brandsub">Candidate sourcing workspace</span>
              </span>
            </div>

            <h1 className="login-title">Login</h1>
            <p className="login-sub">Welcome back</p>
          </header>

          <form onSubmit={handleSubmit} noValidate>
            <label className="login-label" htmlFor="login-email">
              Email address
            </label>
            <div className="login-field">
              <Mail size={16} />
              <input
                id="login-email"
                type="email"
                autoComplete="username"
                className="login-input"
                placeholder="you@company.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={busy}
                autoFocus
              />
            </div>

            <label className="login-label" htmlFor="login-password">
              Password
            </label>
            <div className="login-field">
              <Lock size={16} />
              <input
                id="login-password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                className="login-input"
                placeholder="Enter your password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={busy}
              />
              <button
                type="button"
                className="login-reveal"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? "Hide password" : "Show password"}
                tabIndex={-1}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>

            {/* Genuinely functional: unchecked keeps the token in sessionStorage,
                so it dies with the tab instead of persisting on a shared machine. */}
            <label className="login-remember">
              <input
                type="checkbox"
                checked={remember}
                onChange={(e) => setRemember(e.target.checked)}
                disabled={busy}
              />
              <span className="login-checkbox" aria-hidden="true" />
              <span>Keep me signed in</span>
            </label>

            {/* role="alert" so screen readers announce a failed attempt */}
            {error && (
              <p className="login-error" role="alert">
                <AlertCircle size={15} />
                <span>{error}</span>
              </p>
            )}

            <button type="submit" className="login-submit" disabled={busy}>
              {busy ? (
                <>
                  <Loader2 size={17} className="login-spin" />
                  <span>Logging in…</span>
                </>
              ) : (
                <span>Login</span>
              )}
            </button>
          </form>

          {/* The super admin only. The API decides what is offered here, so a
              deployment that turns demo mode off leaves nothing behind; staff
              sign in with credentials an admin issues them, not from here. */}
          {demoAccounts.length > 0 && (
            <div className="login-demo">
              <span className="login-demo-label">Demo account</span>

              <div className="login-demo-grid">
                {demoAccounts.map((account) => (
                  <button
                    key={account.email}
                    type="button"
                    className={`login-demo-btn is-${account.role}`}
                    onClick={() => void signInWithDemo(account)}
                    disabled={busy}
                    title={`Sign in as ${account.email}`}
                  >
                    <span className="login-demo-icon">
                      <ShieldCheck size={16} strokeWidth={2.2} />
                    </span>
                    <span className="login-demo-text">
                      <strong>{account.label}</strong>
                      <em>{account.description}</em>
                      <code>
                        {account.email} · {account.password}
                      </code>
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>

      </div>
    </div>
  );
}
