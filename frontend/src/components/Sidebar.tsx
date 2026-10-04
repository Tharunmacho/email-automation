"use client";

import { useMemo, useSyncExternalStore } from "react";
import { Check, ChevronsUpDown, Menu, Moon, Search, Sun, X } from "lucide-react";
import Link from "next/link";
import BrandLogo from "@/components/BrandLogo";
import { useModalFocus } from "@/components/ui/useModalFocus";
import { navGroupsFor, navPath, type NavId } from "@/lib/nav";
import { getPaletteServerSnapshot, getPaletteSnapshot, getThemeServerSnapshot, getThemeSnapshot, PALETTES, setPalette, setTheme, subscribeTheme } from "@/lib/theme";
import type { AuthUser } from "@/lib/api";

interface SidebarProps {
  activeId: NavId;
  collapsed: boolean;
  mobileOpen: boolean;
  user: AuthUser;
  onNavigate: (id: NavId) => void;
  onToggleCollapse: () => void;
  onCloseMobile: () => void;
}

const SECTIONS: { label: string; ids: NavId[] }[] = [
  { label: "Workspace", ids: ["overview", "assigned-candidates", "candidates", "candidate-entry", "job-orders", "sourcing", "b2b-enquiries"] },
  { label: "Team & operations", ids: ["staff", "attendance", "payroll"] },
  { label: "Quick access", ids: ["data-management", "users", "settings"] },
];

export default function Sidebar({ activeId, collapsed, mobileOpen, user, onNavigate, onToggleCollapse, onCloseMobile }: SidebarProps) {
  const theme = useSyncExternalStore(subscribeTheme, getThemeSnapshot, getThemeServerSnapshot);
  const palette = useSyncExternalStore(subscribeTheme, getPaletteSnapshot, getPaletteServerSnapshot);
  const railRef = useModalFocus<HTMLElement>(mobileOpen, onCloseMobile);
  const sections = useMemo(() => {
    const items = navGroupsFor(user.role, user.pages).flatMap((group) => group.items);
    return SECTIONS.map((section) => ({ ...section, items: section.ids.flatMap((id) => items.filter((item) => item.id === id)) })).filter((section) => section.items.length);
  }, [user.role, user.pages]);

  return (
    <>
      <div className={`rail-backdrop ${mobileOpen ? "is-open" : ""}`} onClick={onCloseMobile} aria-hidden="true" />
      <nav ref={railRef} className={`rail ${collapsed ? "is-collapsed" : ""} ${mobileOpen ? "is-open" : ""}`} aria-label="Main navigation" role={mobileOpen ? "dialog" : undefined} aria-modal={mobileOpen || undefined} tabIndex={mobileOpen ? -1 : undefined}>
        <div className="rail-brand">
          <Link href={navPath(sections[0]?.items[0]?.id || "settings")} className="rail-logo" aria-label="Adira workspace" onClick={(event) => {
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
            event.preventDefault(); onNavigate(sections[0]?.items[0]?.id || "settings"); onCloseMobile();
          }}><BrandLogo /></Link>
          <button type="button" className="rail-toggle rail-desktop-toggle" onClick={onToggleCollapse} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} aria-expanded={!collapsed}>{collapsed ? <Menu size={15} /> : <ChevronsUpDown size={14} />}</button>
          <button type="button" className="rail-toggle rail-mobile-close" aria-label="Close navigation" onClick={onCloseMobile}><X size={17} /></button>
        </div>
        <button type="button" className="rail-search" aria-label="Search workspace" onClick={() => { onCloseMobile(); window.dispatchEvent(new Event("adira-open-search")); }}>
          <Search size={14} strokeWidth={1.5} /><span>Search</span><kbd>⌘ K</kbd>
        </button>
        <div className="rail-scroll">
          {sections.map((section, sectionIndex) => (
            <div key={section.label} className={`rail-group ${sectionIndex === 0 ? "is-main" : ""}`} role="group" aria-label={section.label}>
              {sectionIndex > 0 && <p className="rail-section-label">{section.label}</p>}
              {section.items.map((item) => {
                const Icon = item.icon;
                return <Link key={item.id} href={navPath(item.id)} className={`rail-item ${activeId === item.id ? "is-active" : ""}`} aria-current={activeId === item.id ? "page" : undefined} title={collapsed ? item.label : undefined} onClick={(event) => {
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault(); onNavigate(item.id); onCloseMobile();
                }}><span className={`rail-nav-icon is-${item.id}`}><Icon size={15} strokeWidth={1.5} /></span><span className="rail-item-label">{item.label}</span></Link>;
              })}
            </div>
          ))}
        </div>
        <div className="rail-foot">
          <div className="rail-workspace-note"><span className="rail-workspace-dot" /><span>Adira Master CRM</span><small>Workspace</small></div>
          <div className="palette-picker" role="group" aria-label="Colour palette">
            <div className="palette-picker-label"><span>Palette</span><strong>{PALETTES.find((option) => option.id === palette)?.label}</strong></div>
            <div className="palette-swatches">
              {PALETTES.map(({ id, label }) => <button key={id} type="button" className={`palette-swatch is-${id}`} onClick={() => setPalette(id)} aria-label={`${label} palette`} aria-pressed={palette === id} title={label}>{palette === id && <Check size={14} strokeWidth={2.5} />}</button>)}
            </div>
          </div>
          <div className="theme-switch" role="group" aria-label="Colour theme">
            {[{ id: "light" as const, label: "Light", icon: Sun }, { id: "dark" as const, label: "Dark", icon: Moon }].map(({ id, label, icon: Icon }) => <button key={id} type="button" className={`theme-switch-btn ${theme === id ? "is-on" : ""}`} onClick={() => setTheme(id)} aria-pressed={theme === id} title={`${label} theme`}><Icon size={12} /><span className="rail-item-label">{label}</span></button>)}
          </div>
          <button type="button" className="rail-theme-mini" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} aria-label="Switch colour theme">{theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}</button>
        </div>
      </nav>
    </>
  );
}
