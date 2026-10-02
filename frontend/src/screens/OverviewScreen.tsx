"use client";

import { useMemo, useState, type ReactNode } from "react";
import { ArrowDown, ArrowRight, ArrowUp, Plus, Search } from "lucide-react";
import Select from "@/components/ui/Select";
import DashboardSkeleton from "@/components/dashboard/DashboardSkeleton";
import { isVerified, needsReview } from "@/lib/dashboardMetrics";
import { candidateNameOf, formatDateFull, formatInt, initialsOf } from "@/lib/format";
import { useIsMounted } from "@/lib/useIsMounted";
import type { NavId } from "@/lib/nav";
import type { CandidateRecord } from "@/lib/api";
import type { LogEntry } from "@/components/dashboard/ActivityLog";
import styles from "./OverviewScreen.module.css";

interface OverviewScreenProps {
  candidates: CandidateRecord[];
  logs: LogEntry[];
  onNavigate: (id: NavId) => void;
  onOpenCandidate: (candidate: CandidateRecord) => void;
}

type Period = "year" | "quarter" | "month" | "all";
interface Point { label: string; value: number }
const DAY = 86400000;
const ORANGE = "#f58b32";
const COLORS = ["#62c9f5", "#fa9250", "#f5ce4f", "#a999df"];
const PIPELINE_STATES = new Set(["submitted", "interviewing", "interview_completed", "offered", "placed"]);

function createdTime(candidate: CandidateRecord) {
  const at = Date.parse(candidate.created_at || "");
  return Number.isFinite(at) ? at : null;
}

function periodStart(period: Period, anchor: Date, candidates: CandidateRecord[]) {
  if (period === "year") return new Date(anchor.getFullYear(), 0, 1).getTime();
  if (period === "all") return Math.min(anchor.getTime() - 90 * DAY, ...candidates.flatMap((candidate) => { const at = createdTime(candidate); return at === null ? [] : [at]; }));
  return anchor.getTime() - (period === "quarter" ? 90 : 30) * DAY;
}

function seriesFor(candidates: CandidateRecord[], start: number, end: number, predicate: (candidate: CandidateRecord) => boolean = () => true, cumulative = false): Point[] {
  const count = 7;
  const width = Math.max(DAY, end - start + 1) / count;
  let running = 0;
  return Array.from({ length: count }, (_, index) => {
    const from = start + index * width;
    const to = start + (index + 1) * width;
    const value = candidates.filter((candidate) => { const at = createdTime(candidate); return at !== null && at >= from && at < to && predicate(candidate); }).length;
    running += value;
    return { label: new Date(Math.min(end, to - 1)).toLocaleDateString("en", { month: "short", ...(end - start < 100 * DAY ? { day: "numeric" } : {}) }), value: cumulative ? running : value };
  });
}

function Trend({ percent }: { percent: number | null }) {
  if (percent === null) return <span className={styles.metricHint}>No earlier data</span>;
  return <span className={`${styles.trend} ${percent < 0 ? styles.down : ""}`}>{Math.abs(percent).toFixed(1)}%{percent < 0 ? <ArrowDown size={10} /> : <ArrowUp size={10} />}</span>;
}

function MiniChart({ points, label, kind = "line", max }: { points: Point[]; label: string; kind?: "line" | "area" | "step" | "bars"; max?: number }) {
  const [hovered, setHovered] = useState<number | null>(null);
  const ceiling = max || Math.max(4, Math.ceil(Math.max(...points.map((point) => point.value), 0) / 4) * 4);
  const x = (index: number) => 32 + index * (198 / Math.max(1, points.length - 1));
  const y = (value: number) => 126 - value / ceiling * 104;
  const line = points.map((point, index) => `${index ? "L" : "M"}${x(index)},${y(point.value)}`).join(" ");
  const step = points.map((point, index) => index ? `H${x(index)} V${y(point.value)}` : `M${x(index)},${y(point.value)}`).join(" ");
  const hasData = points.some((point) => point.value > 0);
  const selected = hovered === null ? (hasData ? points.length - 2 : null) : hovered;
  return <div className={styles.chart}>
    <svg viewBox="0 0 242 156" role="img" aria-label={`${label}. ${points.map((point) => `${point.label}: ${point.value}`).join(", ")}`}>
      {[0, 1, 2, 3, 4].map((tick) => <g key={tick}><line x1="32" x2="230" y1={126 - tick * 26} y2={126 - tick * 26} className={styles.gridline} /><text x="24" y={130 - tick * 26} textAnchor="end" className={styles.axis}>{Math.round(ceiling * tick / 4)}{max === 100 ? "%" : ""}</text></g>)}
      {points.map((point, index) => <g key={index}><line x1={x(index)} x2={x(index)} y1="22" y2="126" className={styles.gridline} />{(index % 2 === 0 || index === points.length - 1) && <text x={x(index)} y="147" textAnchor="middle" className={styles.axis}>{point.label}</text>}</g>)}
      {kind === "area" && <path d={`${line} L230,126 L32,126 Z`} fill={ORANGE} fillOpacity=".16" />}
      {kind === "bars" ? points.map((point, index) => <rect key={index} x={x(index) - 9} y={y(point.value)} width="18" height={126 - y(point.value)} rx="3" fill={index % 2 ? "#f7bc8a" : ORANGE} />) : <path d={kind === "step" ? step : line} fill="none" stroke={ORANGE} strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />}
      {selected !== null && hasData && <g><line x1={x(selected)} x2={x(selected)} y1="18" y2="126" stroke={ORANGE} strokeOpacity=".4" /><circle cx={x(selected)} cy={y(points[selected].value)} r="4.5" fill={ORANGE} stroke="var(--bg-main)" strokeWidth="2" />{hovered !== null && <g><rect x={Math.min(190, Math.max(32, x(selected) - 20))} y={Math.max(2, y(points[selected].value) - 28)} width="40" height="21" rx="5" fill={ORANGE} /><text x={Math.min(210, Math.max(52, x(selected)))} y={Math.max(16, y(points[selected].value) - 14)} textAnchor="middle" fill="white" fontSize="10">{points[selected].value}{max === 100 ? "%" : ""}</text></g>}</g>}
      {points.map((point, index) => <rect key={`hit-${index}`} x={x(index) - 13} y="16" width="26" height="114" fill="transparent" onMouseEnter={() => setHovered(index)} onMouseLeave={() => setHovered(null)}><title>{point.label}: {point.value}{max === 100 ? "%" : ""}</title></rect>)}
      {!hasData && <text x="132" y="79" textAnchor="middle" className={styles.noData}>No activity in this period</text>}
    </svg>
  </div>;
}

function Metric({ label, value, hint, children, title, description, onClick }: { label: string; value: string; hint?: ReactNode; children: ReactNode; title: string; description: string; onClick: () => void }) {
  return <article className={styles.metric}>
    <div className={styles.metricHeader}><p className={styles.metricLabel}>{label}</p><div className={styles.metricReading}><strong>{value}</strong>{hint}</div></div>
    <div className={styles.visual}>{children}</div>
    <button type="button" className={styles.metricCaption} onClick={onClick}><span>{title}</span><ArrowRight size={13} /></button>
    <p className={styles.metricDescription}>{description}</p>
  </article>;
}

function Distribution({ items }: { items: { label: string; value: number }[] }) {
  const total = items.reduce((sum, item) => sum + item.value, 0);
  return <div className={styles.distribution}>
    <div className={styles.distributionBlocks} aria-label={items.map((item) => `${item.label}: ${item.value}`).join(", ")}>
      {items.map((item, index) => <div key={item.label} style={{ flex: total ? Math.max(.08, item.value / total) : 1, backgroundColor: total ? COLORS[index] : "var(--tint-3)" }} title={`${item.label}: ${item.value}`} />)}
    </div>
    <ul className={styles.legend}>{items.map((item, index) => <li key={item.label}><i style={{ background: COLORS[index] }} /><span>{item.label}</span><strong>{formatInt(item.value)}</strong></li>)}</ul>
  </div>;
}

function Ring({ percent, label, color = ORANGE }: { percent: number; label: string; color?: string }) {
  return <div className={styles.ring} role="img" aria-label={`${label}: ${percent}%`}><svg viewBox="0 0 156 156" aria-hidden="true"><circle cx="78" cy="78" r="56" fill="none" stroke="var(--tint-3)" strokeWidth="13" />{percent > 0 && <circle cx="78" cy="78" r="56" fill="none" stroke={color} strokeWidth="13" strokeDasharray={`${percent * 3.5186} 351.86`} strokeLinecap="round" transform="rotate(-90 78 78)" />}</svg><div><strong>{percent}<small>%</small></strong><span>{label}</span></div></div>;
}

function RankedBars({ items, empty, colors = COLORS }: { items: { label: string; value: number }[]; empty: string; colors?: string[] }) {
  const peak = Math.max(...items.map((item) => item.value), 1);
  return <div className={styles.ranked}>{items.length ? items.slice(0, 4).map((item, index) => <div key={item.label}><div className={styles.rankedLabel}><span>{item.label}</span><strong>{formatInt(item.value)}</strong></div><div className={styles.rankedTrack}><i style={{ width: `${item.value / peak * 100}%`, background: colors[index % colors.length] }} /></div></div>) : <p className={styles.rankedEmpty}>{empty}</p>}</div>;
}

export default function OverviewScreen({ candidates, onNavigate, onOpenCandidate }: OverviewScreenProps) {
  const [period, setPeriod] = useState<Period>("year");
  const [query, setQuery] = useState("");
  const [anchor] = useState(() => new Date());
  const mounted = useIsMounted();
  const metrics = useMemo(() => {
    const start = periodStart(period, anchor, candidates);
    const end = anchor.getTime();
    const pool = candidates.filter((candidate) => { const at = createdTime(candidate); return at === null ? period === "all" : at >= start && at <= end; });
    const previous = candidates.filter((candidate) => { const at = createdTime(candidate); return at !== null && at >= start - (end - start) && at < start; }).length;
    const total = pool.length;
    const verified = pool.filter(isVerified).length;
    const isReviewed = (candidate: CandidateRecord) => Boolean(candidate.evaluated_at || (candidate.evaluation_status && candidate.evaluation_status !== "pending") || isVerified(candidate));
    const reviewed = pool.filter(isReviewed).length;
    const allocated = pool.filter((candidate) => candidate.assigned_staff_id).length;
    const progressing = (candidate: CandidateRecord) => PIPELINE_STATES.has(candidate.recruitment_status || "") || Boolean(candidate.placement_locked);
    const ratio = (count: number) => total ? Math.round(count / total * 100) : 0;
    const intake = seriesFor(pool, start, end);
    const quality = intake.map((point, index) => ({ ...point, value: point.value ? Math.round(seriesFor(pool, start, end, isVerified)[index].value / point.value * 100) : 0 }));
    const tally = (getLabel: (candidate: CandidateRecord) => string | null) => {
      const counts = new Map<string, number>();
      pool.forEach((candidate) => { const label = getLabel(candidate); if (label) counts.set(label, (counts.get(label) || 0) + 1); });
      return [...counts].map(([label, value]) => ({ label, value })).sort((a, b) => b.value - a.value);
    };
    return { pool, total, verified, reviewed, allocated, ratio, intake, reviews: seriesFor(pool, start, end, isReviewed), quality, growth: seriesFor(pool, start, end, undefined, true), pipeline: seriesFor(pool, start, end, progressing, true), pipelineCount: pool.filter(progressing).length, percent: period === "all" || !previous ? null : (total - previous) / previous * 100, sources: [
      { label: "WhatsApp", value: pool.filter((candidate) => candidate.source === "whatsapp").length },
      { label: "Email", value: pool.filter((candidate) => !candidate.source || candidate.source === "email").length },
      { label: "Direct entry", value: pool.filter((candidate) => candidate.source === "manual" || candidate.source === "upload").length },
    ], workload: tally((candidate) => candidate.assigned_staff_name || (candidate.assigned_staff_id ? "Assigned team" : null)), destinations: tally((candidate) => candidate.job?.country?.selected_names?.[0] || candidate.job?.country?.destination_country || null) };
  }, [candidates, period, anchor]);
  const recent = useMemo(() => [...metrics.pool].sort((a, b) => (b.created_at || "").localeCompare(a.created_at || "")).filter((candidate) => [candidateNameOf(candidate), candidate.profile?.current_designation, candidate.profile?.email].join(" ").toLowerCase().includes(query.toLowerCase())).slice(0, 5), [metrics.pool, query]);

  if (!mounted) return <DashboardSkeleton />;

  return <div className={styles.overview}>
    <section>
      <header className={styles.sectionHead}>
        <div><h1>Recruitment & Business Overview</h1><p>Track candidate growth and recruitment performance with real-time insights</p></div>
        <div className={styles.headActions}><div className={styles.period}><Select value={period} onChange={(value) => setPeriod(value as Period)} ariaLabel="Reporting period" options={[{ value: "year", label: "This year" }, { value: "quarter", label: "Last 90 days" }, { value: "month", label: "Last 30 days" }, { value: "all", label: "All time" }]} /></div><button type="button" className={styles.addButton} onClick={() => onNavigate("candidate-entry")}><Plus size={15} />Add candidate</button></div>
      </header>
      <div className={styles.metricGrid}>
        <Metric label="Total Candidates" value={formatInt(metrics.total)} hint={<Trend percent={metrics.percent} />} title="Candidate Growth Pattern" description="See how your candidate pool grows over time" onClick={() => onNavigate("candidates")}><MiniChart points={metrics.growth} label="Cumulative candidate intake" kind="area" /></Metric>
        <Metric label="Candidate Distribution" value={formatInt(metrics.total)} hint={<span className={styles.metricHint}>across sourcing channels</span>} title="Sourcing Channel Breakdown" description="See which channels bring in the most candidates" onClick={() => onNavigate("sourcing")}><Distribution items={metrics.sources} /></Metric>
        <Metric label="Profile Verification" value={`${metrics.ratio(metrics.verified)}%`} hint={<span className={styles.metricHint}>{formatInt(metrics.verified)} verified profiles</span>} title="Profile Quality Insights" description="Monitor verified profiles and review quality over time" onClick={() => onNavigate("candidates")}><MiniChart points={metrics.quality} label="Verification rate by intake period" kind="step" max={100} /></Metric>
        <Metric label="Recruitment Pipeline" value={formatInt(metrics.pipelineCount)} hint={<span className={styles.metricHint}>submitted through placement</span>} title="Recruitment Performance" description="Follow candidates from submission through placement" onClick={() => onNavigate("job-orders")}><MiniChart points={metrics.pipeline} label="Recruitment pipeline by intake period" /></Metric>
      </div>
    </section>

    <section className={styles.teamSection}>
      <header className={styles.sectionHead}><div><h2>Team Productivity & Goals</h2><p>Monitor profile review progress and team efficiency</p></div><button type="button" className={styles.textLink} onClick={() => onNavigate("staff")}>View team<ArrowRight size={13} /></button></header>
      <div className={styles.metricGrid}>
        <Metric label="Review Completion" value={`${metrics.ratio(metrics.reviewed)}%`} hint={<span className={styles.metricHint}>{formatInt(metrics.total - metrics.reviewed)} awaiting review</span>} title="Candidate Review Progress" description="Keep profile reviews moving and clear pending work" onClick={() => onNavigate("assigned-candidates")}><MiniChart points={metrics.reviews} label="Reviewed candidates by intake period" kind="bars" /></Metric>
        <Metric label="Allocation Coverage" value={`${metrics.ratio(metrics.allocated)}%`} hint={<span className={styles.metricHint}>{formatInt(metrics.total - metrics.allocated)} unassigned</span>} title="Assignment Coverage" description="Make sure every candidate has a recruiter to support them" onClick={() => onNavigate("staff")}><Ring percent={metrics.ratio(metrics.allocated)} label="allocated" color="#62c9f5" /></Metric>
        <Metric label="Recruiter Workload" value={formatInt(metrics.allocated)} hint={<span className={styles.metricHint}>assigned candidates</span>} title="Team Workload Balance" description="See how candidates are distributed across your team" onClick={() => onNavigate("staff")}><RankedBars items={metrics.workload} empty="No candidates allocated yet" /></Metric>
        <Metric label="Destination Insights" value={formatInt(metrics.destinations.length)} hint={<span className={styles.metricHint}>preferred destinations</span>} title="Country Demand Overview" description="Understand where your candidates are looking to work" onClick={() => onNavigate("data-management")}><RankedBars items={metrics.destinations} empty="No destination preferences yet" colors={["#f58b32", "#f5b76e", "#f5ce4f", "#d9d5cb"]} /></Metric>
      </div>
    </section>

    <section className={styles.recentSection}>
      <header className={styles.sectionHead}><div><h2>Recent Candidates</h2><p>Your latest profiles, ready for the next step</p></div><label className={styles.search}><Search size={14} /><input type="search" value={query} placeholder="Search candidates" aria-label="Search recent candidates" onChange={(event) => setQuery(event.target.value)} /></label></header>
      {recent.length ? <div className={styles.tableWrap}><table className={styles.table}><thead><tr><th>Candidate</th><th>Designation</th><th>Source</th><th>Added</th><th>Status</th><th><span className="sr-only">Open candidate</span></th></tr></thead><tbody>{recent.map((candidate) => {
        const name = candidateNameOf(candidate);
        const state = isVerified(candidate) ? "Verified" : needsReview(candidate) ? "Needs review" : "In progress";
        return <tr key={candidate.id}><td><span className={styles.person}><span className={styles.avatar}>{initialsOf(name)}</span><span><strong>{name}</strong><small>{candidate.profile?.email || candidate.profile?.phone || "No contact on file"}</small></span></span></td><td>{candidate.profile?.current_designation || candidate.job?.job || "—"}</td><td><span className={styles.sourceDot} style={{ background: candidate.source === "whatsapp" ? COLORS[0] : candidate.source === "manual" || candidate.source === "upload" ? COLORS[2] : COLORS[1] }} />{candidate.source === "whatsapp" ? "WhatsApp" : candidate.source === "manual" || candidate.source === "upload" ? "Direct entry" : "Email"}</td><td>{candidate.created_at ? formatDateFull(new Date(candidate.created_at)) : "—"}</td><td><span className={styles.status}><i style={{ background: state === "Verified" ? "#43a776" : state === "Needs review" ? ORANGE : "#9a9a9a" }} />{state}</span></td><td><button type="button" className={styles.openCandidate} aria-label={`Open ${name}`} onClick={() => onOpenCandidate(candidate)}><ArrowRight size={15} /></button></td></tr>;
      })}</tbody></table></div> : <div className={styles.empty}><strong>{query ? "No matching candidates" : "Your next candidate starts here"}</strong><p>{query ? "Try a different name, designation, or email." : "Add a candidate or connect your sourcing channels to start building your pipeline."}</p><button type="button" onClick={() => query ? setQuery("") : onNavigate("candidate-entry")}>{query ? "Clear search" : "Add your first candidate"}<ArrowRight size={14} /></button></div>}
      <button type="button" className={styles.textLink} onClick={() => onNavigate("candidates")}>View all candidates<ArrowRight size={13} /></button>
    </section>
  </div>;
}
