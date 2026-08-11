"use client";

/**
 * Rendered on the server and for the first client paint.
 *
 * Everything below the action row is derived from "today", which the server and
 * the browser can disagree about (different timezone, and with
 * `output: export` the HTML is baked at build time) — so the real Overview
 * mounts only once the client owns the clock. The shapes and counts here match
 * the live layout exactly, so nothing shifts when it swaps in.
 */
export default function DashboardSkeleton() {
  return (
    <div aria-hidden="true">
      <section className="ov-actions">
        {[0, 1, 2, 3].map((index) => (
          <div key={index} className="ov-action" style={{ cursor: "default" }}>
            <span className="dash-skel" style={{ width: 38, height: 38, borderRadius: "var(--radius-md)" }} />
            <span className="ov-action-text" style={{ flex: 1 }}>
              <span className="dash-skel dash-skel-line" style={{ width: "55%" }} />
              <span className="dash-skel dash-skel-line" style={{ width: "88%", marginTop: 8 }} />
            </span>
          </div>
        ))}
      </section>

      <section className="ov-kpis" style={{ marginTop: "1.25rem" }}>
        {[0, 1, 2, 3].map((index) => (
          <div key={index} className="ov-kpi">
            <span className="dash-skel dash-skel-line" style={{ width: "48%" }} />
            <span className="dash-skel dash-skel-value" style={{ marginTop: 12 }} />
            <span className="dash-skel dash-skel-line" style={{ width: "76%", marginTop: 10 }} />
          </div>
        ))}
      </section>

      <section className="db-card" style={{ marginTop: "1.25rem" }}>
        <div className="db-card-head">
          <span className="dash-skel dash-skel-line" style={{ width: "180px" }} />
          <span className="dash-skel dash-skel-line" style={{ width: "104px" }} />
        </div>
        <div className="db-card-body">
          <span className="dash-skel dash-skel-plot" />
        </div>
      </section>

      <section className="db-card" style={{ marginTop: "1.25rem" }}>
        <div className="db-card-head">
          <span className="dash-skel dash-skel-line" style={{ width: "150px" }} />
        </div>
        <div className="db-card-body">
          <span className="dash-skel dash-skel-plot" style={{ height: 180 }} />
        </div>
      </section>
    </div>
  );
}
