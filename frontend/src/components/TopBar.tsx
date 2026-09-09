"use client";

import { useEffect, useRef, useState } from "react";
import Image from "next/image";
import { AlertTriangle, Camera, CheckCircle2, ChevronDown, LoaderCircle, LogOut, Menu, ScanLine, Search, Settings, X } from "lucide-react";

import BrandLogo from "@/components/BrandLogo";
import NotificationBell from "@/components/NotificationBell";
import { initialsOf } from "@/lib/format";
import type { AuthUser } from "@/lib/api";

export interface CandidateExtractionNotice {
  status: "extracting" | "complete" | "error";
  filename: string;
  title: string;
  detail: string;
}

interface TopBarProps {
  user: AuthUser;
  /** Retained for call-site compatibility; these are no longer top-bar UI. */
  syncing?: boolean;
  realtime?: "connecting" | "live" | "offline";
  realtimeNonce?: number;
  onOpenCandidate?: (candidateId: string) => void;
  candidateExtraction?: CandidateExtractionNotice | null;
  onOpenCandidateExtraction?: () => void;
  onDismissCandidateExtraction?: () => void;
  hasRail?: boolean;
  onSync?: () => void;
  onToggleRail: () => void;
  onOpenProfile?: () => void;
  onSignOut?: () => void;
}

/** Search, notifications, and the signed-in person's profile. Operational
 * realtime/sync state remains functional without occupying the top bar. */
export default function TopBar({
  user,
  realtimeNonce = 0,
  hasRail = true,
  onOpenCandidate,
  candidateExtraction = null,
  onOpenCandidateExtraction,
  onDismissCandidateExtraction,
  onToggleRail,
  onOpenProfile,
  onSignOut,
}: TopBarProps) {
  const photoKey = `adira-profile-photo:${user.id}`;
  const [profileOpen, setProfileOpen] = useState(false);
  const [profilePhoto, setProfilePhoto] = useState<string | null>(null);
  const [photoError, setPhotoError] = useState("");
  const profileRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let active = true;
    let stored: string | null = null;
    try { stored = window.localStorage.getItem(photoKey); }
    catch { stored = null; }
    queueMicrotask(() => { if (active) setProfilePhoto(stored); });
    return () => { active = false; };
  }, [photoKey]);

  useEffect(() => {
    if (!profileOpen) return;
    const close = (event: MouseEvent) => {
      if (!profileRef.current?.contains(event.target as Node)) setProfileOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [profileOpen]);

  const uploadProfilePhoto = (file: File | undefined) => {
    setPhotoError("");
    if (!file) return;
    if (!file.type.startsWith("image/")) return setPhotoError("Choose an image file.");
    if (file.size > 2 * 1024 * 1024) return setPhotoError("Use an image smaller than 2 MB.");
    const reader = new FileReader();
    reader.onload = () => {
      const value = typeof reader.result === "string" ? reader.result : null;
      if (!value) return;
      try {
        window.localStorage.setItem(photoKey, value);
        setProfilePhoto(value);
      } catch { setPhotoError("This browser could not save the photo."); }
    };
    reader.readAsDataURL(file);
  };

  return (
    <header className="topbar">
      {hasRail && <button className="topbar-icon-btn topbar-menu-btn" onClick={onToggleRail} aria-label="Open navigation"><Menu size={20} /></button>}
      <div className="topbar-brand"><span className="topbar-logo"><BrandLogo /></span></div>
      <label className="topbar-search"><Search size={15} /><input type="search" placeholder="Search…" aria-label="Search" /><kbd className="topbar-kbd">⌘K</kbd></label>

      <div className="topbar-actions">
        {candidateExtraction && (
          <div className={`topbar-extraction is-${candidateExtraction.status}`} role="status" aria-live="polite">
            <button type="button" className="topbar-extraction-main" onClick={onOpenCandidateExtraction} disabled={!onOpenCandidateExtraction || candidateExtraction.status === "extracting"} title={candidateExtraction.detail}>
              <span className="topbar-extraction-icon" aria-hidden="true">
                {candidateExtraction.status === "extracting" ? <LoaderCircle size={16} /> : candidateExtraction.status === "complete" ? <CheckCircle2 size={16} /> : <AlertTriangle size={16} />}
              </span>
              <span className="topbar-extraction-copy"><strong>{candidateExtraction.title}</strong><small>{candidateExtraction.filename}</small></span>
              {candidateExtraction.status === "extracting" && <ScanLine className="topbar-extraction-scan" size={15} aria-hidden="true" />}
            </button>
            {candidateExtraction.status !== "extracting" && onDismissCandidateExtraction && <button type="button" className="topbar-extraction-dismiss" onClick={onDismissCandidateExtraction} aria-label="Dismiss candidate extraction notification"><X size={13} /></button>}
            {candidateExtraction.status === "extracting" && <span className="topbar-extraction-progress" aria-hidden="true" />}
          </div>
        )}

        <NotificationBell nonce={realtimeNonce} onOpenCandidate={onOpenCandidate} />
        <div className="topbar-profile" ref={profileRef}>
          <button type="button" className="topbar-profile-trigger" onClick={() => setProfileOpen((open) => !open)} aria-expanded={profileOpen} aria-haspopup="menu">
            <span className="topbar-profile-avatar" aria-hidden="true">{profilePhoto ? <Image src={profilePhoto} alt="" width={30} height={30} unoptimized /> : initialsOf(user.name || user.email)}</span>
            <span className="topbar-profile-copy"><strong>{user.name || user.email}</strong><small>{user.role}</small></span>
            <ChevronDown size={14} />
          </button>

          {profileOpen && (
            <div className="topbar-profile-menu" role="menu">
              <div className="topbar-profile-card">
                <span className="topbar-profile-avatar is-large" aria-hidden="true">{profilePhoto ? <Image src={profilePhoto} alt="" width={48} height={48} unoptimized /> : initialsOf(user.name || user.email)}</span>
                <div><strong>{user.name || "Team member"}</strong><span>{user.email}</span>{user.phone && <span>{user.phone}</span>}<small>{user.staff_code || user.role}</small></div>
              </div>
              <label className="topbar-profile-upload"><Camera size={15} /><span>{profilePhoto ? "Change profile photo" : "Add profile photo"}</span><input type="file" accept="image/*" onChange={(event) => uploadProfilePhoto(event.target.files?.[0])} /></label>
              {photoError && <p className="topbar-profile-error">{photoError}</p>}
              {onOpenProfile && <button type="button" role="menuitem" onClick={() => { setProfileOpen(false); onOpenProfile(); }}><Settings size={15} /> Profile & settings</button>}
              {onSignOut && <button type="button" role="menuitem" className="is-danger" onClick={onSignOut}><LogOut size={15} /> Sign out</button>}
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
