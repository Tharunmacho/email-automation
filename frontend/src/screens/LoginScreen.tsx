"use client";

import React, { useState } from "react";
import { AlertCircle, ChefHat, Eye, EyeOff, Loader2, Lock, Mail } from "lucide-react";

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
          <header className="login-head">
            <span className="login-logo">
              <ChefHat size={22} strokeWidth={2.3} />
            </span>
            <h1 className="login-title">Sign in</h1>
            <p className="login-sub">
              Welcome back. Enter your details to access the workspace.
            </p>
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
                  <span>Signing in…</span>
                </>
              ) : (
                <span>Sign in</span>
              )}
            </button>
          </form>

          {/* Deliberately understated — credentials shown in a bordered panel
              make a product look like a prototype. */}
          <p className="login-hint">
            Demo access ·{" "}
            <button
              type="button"
              onClick={() => {
                setEmail("admin@gmail.com");
                setPassword("admin@123");
                setError(null);
              }}
              disabled={busy}
            >
              use sample credentials
            </button>
          </p>
        </div>

        <p className="login-foot">Ingrechef AI · Recruitment workspace</p>
      </div>
    </div>
  );
}
