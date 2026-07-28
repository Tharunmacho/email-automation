"use client";

import React from "react";
import { Users, CheckCircle2, Eye, Cpu } from "lucide-react";

interface StatsGridProps {
  total: number;
  verified: number;
  review: number;
}

export default function StatsGrid({ total, verified, review }: StatsGridProps) {
  return (
    <div className="stats-grid">
      <div className="glass-card stat-card">
        <div className="stat-icon">
          <Users size={24} />
        </div>
        <div>
          <div className="stat-value" id="stat-total">{total}</div>
          <div className="stat-label">Total Candidates</div>
        </div>
      </div>
      
      <div className="glass-card stat-card">
        <div className="stat-icon">
          <CheckCircle2 size={24} />
        </div>
        <div>
          <div className="stat-value" id="stat-verified">{verified}</div>
          <div className="stat-label">Verified Candidates</div>
        </div>
      </div>
      
      <div className="glass-card stat-card">
        <div className="stat-icon">
          <Eye size={24} />
        </div>
        <div>
          <div className="stat-value" id="stat-review">{review}</div>
          <div className="stat-label">Pending Review</div>
        </div>
      </div>
      
      <div className="glass-card stat-card">
        <div className="stat-icon">
          <Cpu size={24} />
        </div>
        <div>
          <div className="stat-value">100%</div>
          <div className="stat-label">Veris LLM Acc</div>
        </div>
      </div>
    </div>
  );
}
