"use client";

import React, { useEffect, useState, useSyncExternalStore } from "react";
import {
  AlertCircle,
  ArrowRight,
  CheckCircle2,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Mail,
  Moon,
  ShieldCheck,
  Sun,
} from "lucide-react";

import BrandLogo from "@/components/BrandLogo";
import { fetchDemoAccounts, login, type AuthUser, type DemoAccount } from "@/lib/api";
import {
  getThemeServerSnapshot,
  getThemeSnapshot,
  setTheme,
  subscribeTheme,
} from "@/lib/theme";

interface LoginScreenProps {
  onSuccess: (user: AuthUser) => void;
}

const LOGIN_BENEFITS = [
  "Candidate records in one clear workspace",
  "Fair assignment across your review team",
  "Role-based access that keeps work focused",
];

export default function LoginScreen({ onSuccess }: LoginScreenProps) {
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeServerSnapshot);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [demoAccounts, setDemoAccounts] = useState<DemoAccount[]>([]);

  useEffect(() => {
    let cancelled = false;
    void fetchDemoAccounts().then((accounts) => {
      if (!cancelled) setDemoAccounts(accounts);
    });
    return () => {
      cancelled = true;
    };
  }, []);

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

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
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
    <div className="auth-page">
      <div className="auth-shell">
        <aside className="auth-story" aria-label="About ADIRA-Master CRM">
          <div className="auth-story-orb is-one" aria-hidden="true" />
          <div className="auth-story-orb is-two" aria-hidden="true" />

          <div className="auth-story-logo">
            <BrandLogo />
          </div>

          <div className="auth-story-content">
            <span className="auth-eyebrow">
              <ShieldCheck size={15} />
              ADIRA-Master CRM
            </span>
            <h1>
              Every candidate.
              <span>One clear workflow.</span>
            </h1>
            <p>
              Move from sourcing to review with a focused workspace built for recruitment teams.
            </p>

            <ul className="auth-benefits">
              {LOGIN_BENEFITS.map((benefit) => (
                <li key={benefit}>
                  <CheckCircle2 size={17} />
                  <span>{benefit}</span>
                </li>
              ))}
            </ul>
          </div>

          <p className="auth-story-foot">Recruitment operations, organised.</p>
        </aside>

        <main className="auth-access">
          <button
            type="button"
            className="auth-theme-toggle"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            {theme === "dark" ? <Sun size={17} /> : <Moon size={17} />}
          </button>

          <section className="auth-card" aria-labelledby="auth-title">
            <div className="auth-mobile-logo">
              <BrandLogo />
            </div>

            <header className="auth-head">
              <span className="auth-secure-label">
                <ShieldCheck size={14} /> Secure staff access
              </span>
              <h2 id="auth-title">Welcome back</h2>
              <p>Sign in to continue to ADIRA-Master CRM.</p>
            </header>

            <form className="auth-form" onSubmit={handleSubmit} noValidate aria-busy={busy}>
              <label className="auth-label" htmlFor="login-email">
                Email address
              </label>
              <div className="auth-field">
                <Mail size={18} aria-hidden="true" />
                <input
                  id="login-email"
                  type="email"
                  autoComplete="username"
                  placeholder="you@company.com"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  disabled={busy}
                  autoFocus
                />
              </div>

              <label className="auth-label" htmlFor="login-password">
                Password
              </label>
              <div className="auth-field">
                <Lock size={18} aria-hidden="true" />
                <input
                  id="login-password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  placeholder="Enter your password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={busy}
                />
                <button
                  type="button"
                  className="auth-reveal"
                  onClick={() => setShowPassword((visible) => !visible)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>

              <label className="auth-remember">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(event) => setRemember(event.target.checked)}
                  disabled={busy}
                />
                <span className="auth-checkbox" aria-hidden="true" />
                <span>Keep me signed in on this device</span>
              </label>

              {error && (
                <p className="auth-error" role="alert">
                  <AlertCircle size={17} />
                  <span>{error}</span>
                </p>
              )}

              <button type="submit" className="auth-submit" disabled={busy}>
                {busy ? (
                  <>
                    <Loader2 size={18} className="auth-spin" />
                    <span>Signing in...</span>
                  </>
                ) : (
                  <>
                    <span>Sign in</span>
                    <ArrowRight size={18} />
                  </>
                )}
              </button>
            </form>

            {demoAccounts.length > 0 && (
              <div className="auth-demo">
                <div className="auth-divider">
                  <span>Demo access</span>
                </div>
                <div className="auth-demo-grid">
                  {demoAccounts.map((account) => (
                    <button
                      key={account.email}
                      type="button"
                      className="auth-demo-button"
                      onClick={() => void signInWithDemo(account)}
                      disabled={busy}
                    >
                      <span className="auth-demo-icon">
                        <ShieldCheck size={17} />
                      </span>
                      <span className="auth-demo-copy">
                        <strong>{account.label}</strong>
                        <small>{account.description}</small>
                      </span>
                      <ArrowRight size={16} className="auth-demo-arrow" />
                    </button>
                  ))}
                </div>
              </div>
            )}
          </section>

          <p className="auth-access-foot">Protected workspace · Authorised access only</p>
        </main>
      </div>
    </div>
  );
}
