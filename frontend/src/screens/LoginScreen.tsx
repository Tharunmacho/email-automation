"use client";

import React, { useState, useSyncExternalStore } from "react";
import {
  AlertCircle,
  Eye,
  EyeOff,
  Loader2,
  Lock,
  Mail,
  Moon,
  ShieldCheck,
  Sparkles,
  Sun,
} from "lucide-react";

import BrandLogo from "@/components/BrandLogo";
import { login, type AuthUser } from "@/lib/api";
import {
  getThemeServerSnapshot,
  getThemeSnapshot,
  setTheme,
  subscribeTheme,
} from "@/lib/theme";

interface LoginScreenProps {
  onSuccess: (user: AuthUser) => void;
}

export default function LoginScreen({ onSuccess }: LoginScreenProps) {
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeServerSnapshot);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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
    <main className="signin-page">
      <section className="signin-story" aria-labelledby="signin-story-title">
        <div className="signin-story-glow" aria-hidden="true" />
        <div className="signin-story-lines" aria-hidden="true">
          <span />
          <span />
          <span />
          <span />
        </div>

        <span className="signin-story-mark" aria-hidden="true">
          <Sparkles size={43} strokeWidth={1.8} />
        </span>

        <div className="signin-story-copy">
          <p className="signin-story-kicker">Adira Master CRM</p>
          <h1 id="signin-story-title">
            Hello,
            <br />
            welcome back!
          </h1>
          <p>
            Turn every application into action. Manage candidates, coordinate
            your team, and keep recruitment moving from one workspace.
          </p>
        </div>

        <p className="signin-copyright">© 2026 Adira Enterprises. All rights reserved.</p>
      </section>

      <section className="signin-access" aria-labelledby="auth-title">
        <header className="signin-access-top">
          <BrandLogo className="signin-logo" />
          <button
            type="button"
            className="signin-theme-toggle"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </header>

        <div className="signin-card">
          <header className="signin-head">
            <p className="signin-eyebrow">Secure workspace</p>
            <h2 id="auth-title">Welcome back!</h2>
            <p>Sign in with your work email to continue to Adira Master CRM.</p>
          </header>

          <form className="signin-form" onSubmit={handleSubmit} noValidate aria-busy={busy}>
            <label className="signin-label" htmlFor="login-email">Email address</label>
            <div className="signin-field">
              <Mail size={19} aria-hidden="true" />
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

            <label className="signin-label" htmlFor="login-password">Password</label>
            <div className="signin-field">
              <Lock size={19} aria-hidden="true" />
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
                className="signin-reveal"
                onClick={() => setShowPassword((visible) => !visible)}
                aria-label={showPassword ? "Hide password" : "Show password"}
              >
                {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
              </button>
            </div>

            <label className="signin-remember">
              <input
                type="checkbox"
                checked={remember}
                onChange={(event) => setRemember(event.target.checked)}
                disabled={busy}
              />
              <span className="signin-checkbox" aria-hidden="true" />
              <span>Keep me signed in on this device</span>
            </label>

            {error && (
              <p className="signin-error" role="alert">
                <AlertCircle size={17} />
                <span>{error}</span>
              </p>
            )}

            <button type="submit" className="signin-submit" disabled={busy}>
              {busy ? (
                <>
                  <Loader2 size={18} className="signin-spin" />
                  <span>Signing in...</span>
                </>
              ) : (
                <span>Sign in</span>
              )}
            </button>
          </form>

          <p className="signin-foot">
            <ShieldCheck size={16} aria-hidden="true" />
            Secure, role-based access for authorised staff
          </p>
        </div>
      </section>
    </main>
  );
}
