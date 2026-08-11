"use client";

import React, { useState } from "react";
import { AlertCircle, Eye, EyeOff, Loader2, Lock, Mail, Sparkles } from "lucide-react";

import { login, type AuthUser } from "@/lib/api";

interface LoginScreenProps {
  onSuccess: (user: AuthUser) => void;
}

export default function LoginScreen({ onSuccess }: LoginScreenProps) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

          {/* Credentials shown outright, as requested. Clicking the block
              fills the form so they never have to be typed by hand. */}
          <button
            type="button"
            className="login-creds"
            onClick={() => {
              setEmail("admin@gmail.com");
              setPassword("admin@123");
              setError(null);
            }}
            disabled={busy}
            title="Fill the form with these credentials"
          >
            <span className="login-creds-label">Demo credentials</span>
            <span className="login-creds-row">
              <span>Email</span>
              <code>admin@gmail.com</code>
            </span>
            <span className="login-creds-row">
              <span>Password</span>
              <code>admin@123</code>
            </span>
          </button>
        </div>

      </div>
    </div>
  );
}
