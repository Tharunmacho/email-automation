"use client";

import { useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { createPortal } from "react-dom";
import Image from "next/image";
import { AlertTriangle, Camera, CheckCircle2, ChevronDown, LoaderCircle, LogOut, Menu, Move, ScanLine, Search, Settings, X, ZoomIn } from "lucide-react";

import BrandLogo from "@/components/BrandLogo";
import NotificationBell from "@/components/NotificationBell";
import { useModalFocus } from "@/components/ui/useModalFocus";
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
  const [editorSource, setEditorSource] = useState<string | null>(null);
  const [cropZoom, setCropZoom] = useState(1);
  const [cropOffset, setCropOffset] = useState({ x: 0, y: 0 });
  const [cropImageSize, setCropImageSize] = useState({ width: 0, height: 0 });
  const [outputSize, setOutputSize] = useState<256 | 512 | 1024>(512);
  const profileRef = useRef<HTMLDivElement>(null);
  const profileTriggerRef = useRef<HTMLButtonElement>(null);
  const editorRef = useModalFocus<HTMLDivElement>(Boolean(editorSource), () => setEditorSource(null));
  const cropDragRef = useRef<{ pointerId: number; x: number; y: number; offsetX: number; offsetY: number } | null>(null);
  const cropPreviewSize = 280;

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

  const chooseProfilePhoto = (file: File | undefined) => {
    setPhotoError("");
    if (!file) return;
    if (!file.type.startsWith("image/")) return setPhotoError("Choose an image file.");
    if (file.size > 10 * 1024 * 1024) return setPhotoError("Use an image smaller than 10 MB.");
    const reader = new FileReader();
    reader.onload = () => {
      const value = typeof reader.result === "string" ? reader.result : null;
      if (!value) return;
      setCropZoom(1);
      setCropOffset({ x: 0, y: 0 });
      setCropImageSize({ width: 0, height: 0 });
      // The file input disappears with the menu. Restore focus to the lasting
      // profile button before the dialog captures its return-focus target.
      profileTriggerRef.current?.focus();
      setEditorSource(value);
      setProfileOpen(false);
    };
    reader.readAsDataURL(file);
  };

  const clampCropOffset = (x: number, y: number, zoom = cropZoom) => {
    if (!cropImageSize.width || !cropImageSize.height) return { x: 0, y: 0 };
    const coverScale = Math.max(cropPreviewSize / cropImageSize.width, cropPreviewSize / cropImageSize.height);
    const maxX = Math.max(0, (cropImageSize.width * coverScale * zoom - cropPreviewSize) / 2);
    const maxY = Math.max(0, (cropImageSize.height * coverScale * zoom - cropPreviewSize) / 2);
    return {
      x: Math.max(-maxX, Math.min(maxX, x)),
      y: Math.max(-maxY, Math.min(maxY, y)),
    };
  };

  const startCropDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    cropDragRef.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      offsetX: cropOffset.x,
      offsetY: cropOffset.y,
    };
  };

  const moveCrop = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = cropDragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setCropOffset(clampCropOffset(
      drag.offsetX + event.clientX - drag.x,
      drag.offsetY + event.clientY - drag.y,
    ));
  };

  const stopCropDrag = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (cropDragRef.current?.pointerId === event.pointerId) cropDragRef.current = null;
  };

  const saveCroppedPhoto = () => {
    if (!editorSource) return;
    const image = new window.Image();
    image.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = outputSize;
      canvas.height = outputSize;
      const context = canvas.getContext("2d");
      if (!context) return setPhotoError("This browser could not crop the photo.");

      const coverScale = Math.max(outputSize / image.naturalWidth, outputSize / image.naturalHeight) * cropZoom;
      const width = image.naturalWidth * coverScale;
      const height = image.naturalHeight * coverScale;
      const offsetScale = outputSize / cropPreviewSize;
      context.drawImage(
        image,
        (outputSize - width) / 2 + cropOffset.x * offsetScale,
        (outputSize - height) / 2 + cropOffset.y * offsetScale,
        width,
        height,
      );

      const value = canvas.toDataURL("image/jpeg", 0.9);
      try {
        window.localStorage.setItem(photoKey, value);
        setProfilePhoto(value);
        setEditorSource(null);
        setPhotoError("");
      } catch { setPhotoError("This browser could not save the photo."); }
    };
    image.onerror = () => setPhotoError("This image could not be opened.");
    image.src = editorSource;
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
          <button ref={profileTriggerRef} type="button" className="topbar-profile-trigger" onClick={() => setProfileOpen((open) => !open)} aria-expanded={profileOpen} aria-haspopup="menu">
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
              <label className="topbar-profile-upload"><Camera size={15} /><span>{profilePhoto ? "Change profile photo" : "Add profile photo"}</span><input type="file" accept="image/*" onChange={(event) => { chooseProfilePhoto(event.target.files?.[0]); event.currentTarget.value = ""; }} /></label>
              {photoError && <p className="topbar-profile-error">{photoError}</p>}
              {onOpenProfile && <button type="button" role="menuitem" onClick={() => { setProfileOpen(false); onOpenProfile(); }}><Settings size={15} /> Profile & settings</button>}
              {onSignOut && <button type="button" role="menuitem" className="is-danger" onClick={onSignOut}><LogOut size={15} /> Sign out</button>}
            </div>
          )}
        </div>
      </div>

      {/* The top bar's backdrop-filter creates a containing block for fixed
          descendants. Portal the editor so its overlay fills the viewport. */}
      {editorSource && createPortal(
        <div className="profile-photo-editor-overlay">
          <div ref={editorRef} className="profile-photo-editor" role="dialog" aria-modal="true" aria-labelledby="profile-photo-editor-title" tabIndex={-1}>
            <div className="profile-photo-editor-head">
              <div>
                <h2 id="profile-photo-editor-title">Adjust profile photo</h2>
                <p>Drag to reposition, then zoom and choose the saved size.</p>
              </div>
              <button type="button" onClick={() => setEditorSource(null)} aria-label="Close photo editor"><X size={18} /></button>
            </div>

            <div className="profile-photo-editor-body">
              <div
                className="profile-photo-crop"
                onPointerDown={startCropDrag}
                onPointerMove={moveCrop}
                onPointerUp={stopCropDrag}
                onPointerCancel={stopCropDrag}
              >
                <Image
                  src={editorSource}
                  alt="Profile photo crop preview"
                  fill
                  sizes="280px"
                  unoptimized
                  draggable={false}
                  onLoad={(event) => setCropImageSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })}
                  style={{ transform: `translate(${cropOffset.x}px, ${cropOffset.y}px) scale(${cropZoom})` }}
                />
                <span className="profile-photo-crop-ring" aria-hidden="true" />
                <span className="profile-photo-drag-hint"><Move size={14} /> Drag photo</span>
              </div>

              <div className="profile-photo-controls">
                <label>
                  <span><ZoomIn size={15} /> Zoom <strong>{Math.round(cropZoom * 100)}%</strong></span>
                  <input
                    type="range"
                    min="1"
                    max="3"
                    step="0.05"
                    value={cropZoom}
                    onChange={(event) => {
                      const nextZoom = Number(event.target.value);
                      setCropZoom(nextZoom);
                      setCropOffset((current) => clampCropOffset(current.x, current.y, nextZoom));
                    }}
                  />
                </label>

                <fieldset>
                  <legend>Saved image size</legend>
                  <div className="profile-photo-size-options">
                    {([256, 512, 1024] as const).map((size) => (
                      <button key={size} type="button" className={outputSize === size ? "is-on" : ""} onClick={() => setOutputSize(size)}>
                        {size}px
                      </button>
                    ))}
                  </div>
                </fieldset>

                <button type="button" className="profile-photo-reset" onClick={() => { setCropZoom(1); setCropOffset({ x: 0, y: 0 }); }}>
                  Reset position and zoom
                </button>
                {photoError && <p className="profile-photo-editor-error">{photoError}</p>}
              </div>
            </div>

            <div className="profile-photo-editor-foot">
              <button type="button" className="ds-ghost-btn" onClick={() => setEditorSource(null)}>Cancel</button>
              <button type="button" className="ds-primary-btn" onClick={saveCroppedPhoto}><Camera size={15} /> Save photo</button>
            </div>
          </div>
        </div>,
        document.body,
      )}
    </header>
  );
}
