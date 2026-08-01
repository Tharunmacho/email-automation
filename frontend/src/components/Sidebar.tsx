"use client";

import React from "react";
import { LayoutDashboard, Users, GitMerge, Building2, FileText, ChevronLeft, ChefHat } from "lucide-react";

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
    { id: "sourcing", label: "Sourcing Hub", icon: Building2 },
    { id: "job-orders", label: "Job Orders", icon: FileText },
  ];

  const handleTabClick = (tabId: string) => {
    onTabChange(tabId);
    onCloseMobile();
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
              <ChefHat size={20} strokeWidth={2.4} />
            </div>
            <span className="brand-text">
              <span className="brand-title">Ingrechef AI</span>
              <span className="brand-caption">Recruitment workspace</span>
            </span>
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

        <p className="nav-section-label">Workspace</p>

        <ul className="nav-menu">
          {menuItems.map((item) => {
            const Icon = item.icon;
            const isActive = activeTab === item.id;
            return (
              <li key={item.id}>
                <button
                  type="button"
                  className={`nav-item ${isActive ? "active" : ""}`}
                  onClick={() => handleTabClick(item.id)}
                  aria-current={isActive ? "page" : undefined}
                  title={collapsed ? item.label : undefined}
                >
                  <Icon size={18} strokeWidth={2} />
                  <span>{item.label}</span>
                </button>
              </li>
            );
          })}
        </ul>

        <div className="sidebar-footer">
          <div className="status-dot"></div>
          <span>Pipeline active</span>
        </div>
      </div>
    </>
  );
}
