"use client";

import React from "react";

/**
 * Rendered on the server and for the first client paint. Everything below the
 * tiles is derived from "today", which the server and the browser can disagree
 * about (different timezone, and with `output: export` the HTML is baked at
 * build time) — so the real cards mount only once the client owns the clock.
 * Shapes and heights match the live layout so nothing shifts when it swaps in.
 */
export default function DashboardSkeleton() {
  return (
    <div className="tab-content active dash-root" aria-hidden="true">
      <div className="metric-tiles">
        {/* Same tones, in the same order, as the live tiles — otherwise the
            accent edges change colour as the real cards swap in. */}
        {(["blue", "green", "yellow", "navy"] as const).map((tone, index) => (
          <div key={index} className={`metric-tile tone-${tone}`}>
            <div className="metric-tile-head">
              <span className="dash-skel dash-skel-chip" />
              <span className="dash-skel dash-skel-spark" />
            </div>
            <div className="metric-tile-body">
              <span className="dash-skel dash-skel-line" style={{ width: "58%" }} />
              <span className="dash-skel dash-skel-value" />
            </div>
            <div className="metric-tile-foot">
              <span className="dash-skel dash-skel-line" style={{ width: "42%" }} />
            </div>
          </div>
        ))}
      </div>

      <div className="dash-row dash-row-primary">
        <section className="dash-card">
          <div className="dash-card-head">
            <span className="dash-skel dash-skel-line" style={{ width: "180px" }} />
            <span className="dash-skel dash-skel-line" style={{ width: "104px" }} />
          </div>
          <span className="dash-skel dash-skel-plot" />
        </section>
        <section className="dash-card">
          <div className="dash-card-head">
            <span className="dash-skel dash-skel-line" style={{ width: "140px" }} />
          </div>
          {[0, 1, 2, 3, 4, 5].map((index) => (
            <span key={index} className="dash-skel dash-skel-person" />
          ))}
        </section>
      </div>
    </div>
  );
}
