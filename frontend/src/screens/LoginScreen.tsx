"use client";

import React, { useState, useSyncExternalStore } from "react";
import Image from "next/image";
import {
  AlertCircle,
  Eye,
  EyeOff,
  Loader2,
  Moon,
  ShieldCheck,
  Sun,
} from "lucide-react";

import BrandLogo from "@/components/BrandLogo";
import {
  loginArtworkFallback,
  loginArtworkSources,
} from "@/assets/login/artwork";
import { login, type AuthUser } from "@/lib/api";
import {
  getThemeServerSnapshot,
  getThemeSnapshot,
  setTheme,
  subscribeTheme,
} from "@/lib/theme";
import styles from "./LoginScreen.module.css";

interface LoginScreenProps {
  onSuccess: (user: AuthUser) => void;
}

export default function LoginScreen({ onSuccess }: LoginScreenProps) {
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeServerSnapshot);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [remember, setRemember] = useState(true);
  const [showPassword, setShowPassword] = useState(false);
  const [showHelp, setShowHelp] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;

    setError(null);
    setShowHelp(false);
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
    <main className={styles.page}>
      <section className={styles.story} aria-labelledby="signin-story-title">
        <picture className={styles.artworkFrame}>
          {loginArtworkSources.map((source) => (
            <source key={source.srcSet} {...source} />
          ))}
          <Image
            src={loginArtworkFallback}
            alt=""
            className={styles.artwork}
            unoptimized
            loading="eager"
            fetchPriority="high"
          />
        </picture>
        <p className={styles.kicker}>A world of possibilities<span aria-hidden="true" /></p>
        <div className={styles.storyCopy}>
          <h1 id="signin-story-title">
            <span>Find talent.</span>
            <span>Build teams.</span>
            <span>Create futures.</span>
          </h1>
          <p>
            Great opportunities begin with the right people.
            Bring them together with Adira.
          </p>
        </div>
      </section>

      <section className={styles.access} aria-labelledby="auth-title">
        <header className={styles.brandHeader}>
          <BrandLogo className={styles.logo} />
          <button
            type="button"
            className={styles.themeToggle}
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
            title={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </header>

        <div className={styles.card}>
          <header className={styles.heading}>
            <h2 id="auth-title">Welcome Back</h2>
            <p>Enter your email and password to access your workspace.</p>
          </header>

          <form className={styles.form} onSubmit={handleSubmit} noValidate aria-busy={busy}>
            <div className={styles.formGroup}>
              <label className={styles.label} htmlFor="login-email">Email address</label>
              <div className={styles.field}>
                <input
                  id="login-email"
                  type="email"
                  autoComplete="username"
                  placeholder="Enter your email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  disabled={busy}
                  required
                />
              </div>
            </div>

            <div className={styles.formGroup}>
              <label className={styles.label} htmlFor="login-password">Password</label>
              <div className={styles.field}>
                <input
                  id="login-password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  placeholder="Enter your password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  disabled={busy}
                  required
                />
                <button
                  type="button"
                  className={styles.reveal}
                  disabled={busy}
                  onClick={() => setShowPassword((visible) => !visible)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
            </div>

            <div className={styles.options}>
              <label className={styles.remember}>
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(event) => setRemember(event.target.checked)}
                  disabled={busy}
                />
                <span>Remember me</span>
              </label>
              <button
                type="button"
                className={styles.helpButton}
                disabled={busy}
                onClick={() => {
                  setError(null);
                  setShowHelp((visible) => !visible);
                }}
                aria-expanded={showHelp}
                aria-controls="signin-help"
              >
                Forgot password?
              </button>
            </div>

            {showHelp && <p id="signin-help" className={styles.help} role="status">Contact your Adira administrator to reset your password or request access.</p>}

            {error && (
              <p className={styles.error} role="alert">
                <AlertCircle size={17} />
                <span>{error}</span>
              </p>
            )}

            <button type="submit" className={styles.submit} disabled={busy}>
              {busy ? (
                <>
                  <Loader2 size={18} className={styles.spin} />
                  <span>Signing in...</span>
                </>
              ) : (
                <span>Sign in</span>
              )}
            </button>
          </form>

          <p className={styles.secure}>
            <ShieldCheck size={16} aria-hidden="true" />
            Secure access to Adira Master CRM
          </p>
        </div>
        <footer className={styles.footer}>
          <p>Need an account? <span>Contact your administrator</span></p>
          <small>© 2026 Adira Enterprises</small>
        </footer>
      </section>
    </main>
  );
}
