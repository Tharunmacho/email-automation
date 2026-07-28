"use client";

import React from "react";
import { LayoutDashboard, Users, GitMerge, ChevronLeft, ChefHat } from "lucide-react";

interface SidebarProps {
  collapsed: boolean;
  onToggleCollapse: () => void;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  activeTab: string;
  onTabChange: (tab: string) => void;
}

export default function Sidebar({
  collapsed,
  onToggleCollapse,
  mobileOpen,
  onCloseMobile,
  activeTab,
  onTabChange,
}: SidebarProps) {
  const menuItems = [
    { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
    { id: "candidates", label: "Candidates", icon: Users },
    { id: "visualizer", label: "Flow Visualizer", icon: GitMerge },
  ];

  const handleTabClick = (tabId: string) => {
    onTabChange(tabId);
    onCloseMobile(); // auto-close drawer on mobile
  };

  return (
    <>
      {/* Mobile Backdrop Overlay */}
      <div
        className={`sidebar-overlay ${mobileOpen ? "active" : ""}`}
        onClick={onCloseMobile}
      ></div>

      {/* Sidebar Navigation */}
      <div className={`sidebar ${collapsed ? "collapsed" : ""} ${mobileOpen ? "mobile-open" : ""}`}>
        <div className="brand">
          <div className="brand-logo-wrapper">
            <div className="brand-logo">
              <ChefHat size={22} strokeWidth={2.5} />
            </div>
            <span className="brand-title">Ingrechef AI</span>
          </div>
          <button
            className="sidebar-toggle"
            id="sidebar-toggle-btn"
            onClick={onToggleCollapse}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <ChevronLeft size={16} />
          </button>
        </div>

        <ul className="nav-menu">
          {menuItems.map((item) => {
            const Icon = item.icon;
            const isActive = activeTab === item.id;
            return (
              <li key={item.id}>
                <button
                  className={`nav-item ${isActive ? "active" : ""}`}
                  style={{ width: "100%", background: "none", border: "none", textAlign: "left", cursor: "pointer" }}
                  onClick={() => handleTabClick(item.id)}
                >
                  <Icon size={20} />
                  <span>{item.label}</span>
                </button>
              </li>
            );
          })}
        </ul>

        <div className="sidebar-footer">
          <div className="status-dot"></div>
          <span>Pipeline Active (Veris LLM)</span>
        </div>
      </div>
    </>
  );
}
