"use client";

import { useMemo } from "react";
import { ArrowLeft, History, User as UserIcon } from "lucide-react";

import { logsForCandidate, type CandidateLog, type CandidateLogAction } from "@/lib/candidateLog";
import { candidateNameOf, formatDateFull, formatInt, initialsOf, timeAgo } from "@/lib/format";
import type { CandidateRecord } from "@/lib/api";

interface CandidateLogsScreenProps {
  candidate: CandidateRecord;
  logs: CandidateLog[];
  onBack: () => void;
}

/**
 * What each action is called on the timeline, and which node colour it takes.
 *
 * The badge says what happened to the *record*, not what the app did — "profile
 * saved" rather than "edited" — because that is the sentence someone auditing
 * the record is looking for.
 */
const EVENT: Record<CandidateLogAction, { label: string; tone: "blue" | "green" | "amber" | "red" }> = {
  Viewed: { label: "Candidate profile viewed", tone: "blue" },
  "Opened editor": { label: "Candidate profile updated", tone: "blue" },
  Edited: { label: "Candidate profile saved", tone: "green" },
  Renamed: { label: "Candidate profile renamed", tone: "green" },
  Verified: { label: "Candidate profile verified", tone: "green" },
  Deleted: { label: "Candidate profile deleted", tone: "red" },
  Failed: { label: "Action failed", tone: "amber" },
};

/**
 * "03:40 pm" — hand-formatted rather than `toLocaleTimeString`, because the
 * server and the browser can resolve different locales and any mismatch in the
 * rendered text is a hydration error.
 */
function clockOf(iso: string): string {
  const date = new Date(iso);
  const hours = date.getHours();
  const suffix = hours < 12 ? "am" : "pm";
  const hour12 = hours % 12 === 0 ? 12 : hours % 12;
  return `${String(hour12).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")} ${suffix}`;
}

/**
 * The viewer's own timezone, short form — "IST", "GMT+2".
 *
 * Read from the browser rather than assumed: a timestamp with the wrong zone
 * stamped on it is worse than one with no zone at all, and this app is used
 * from more than one.
 */
function zoneOf(date: Date): string {
  try {
    const parts = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(date);
    return parts.find((part) => part.type === "timeZoneName")?.value ?? "";
  } catch {
    return "";
  }
}

/**
 * One candidate's history, as a vertical timeline.
 *
 * Entries are matched on the candidate id alone, so this screen can only ever
 * show events belonging to the person named at the top of it. Newest first: on
 * a history screen the last thing that happened is what you came to read.
 */
export default function CandidateLogsScreen({ candidate, logs, onBack }: CandidateLogsScreenProps) {
  const displayName = candidateNameOf(candidate);

  const entries = useMemo(
    () => logsForCandidate(logs, candidate.id).reverse(),
    [logs, candidate.id],
  );

  /** Grouped by day, so a long history reads as "what happened when". */
  const days = useMemo(() => {
    const grouped = new Map<string, CandidateLog[]>();
    for (const entry of entries) {
      const date = new Date(entry.at);
      const key = `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
      const bucket = grouped.get(key);
      if (bucket) bucket.push(entry);
      else grouped.set(key, [entry]);
    }
    return [...grouped.values()];
  }, [entries]);

  const lastAt = entries[0]?.at;

  return (
    <div className="cscreen" style={{ animation: "fadeIn 0.3s ease" }}>
      <div className="cscreen-topbar">
        <button type="button" className="jod-back" onClick={onBack} title="Back to the candidates list">
          <ArrowLeft size={15} /> Back to candidates
        </button>
      </div>

      <header className="clog-hero">
        <span className="cprof-monogram" aria-hidden="true">
          {initialsOf(displayName)}
        </span>
        <div className="cprof-hero-text">
          <div className="cprof-hero-line">
            <h2 className="cprof-name">Activity — {displayName}</h2>
            <span className="clog-count">
              {formatInt(entries.length)} event{entries.length === 1 ? "" : "s"}
            </span>
          </div>
          <p className="cprof-meta">
            {lastAt
              ? `Last activity ${timeAgo(lastAt)}. History for this candidate only.`
              : "History for this candidate only."}
          </p>
        </div>
      </header>

      {entries.length === 0 ? (
        <div className="db-empty">
          <History size={28} strokeWidth={1.6} />
          <span className="db-empty-title">No activity recorded for {displayName} yet</span>
          <span className="db-empty-sub">
            Viewing, editing, verifying or deleting this profile will appear here.
          </span>
        </div>
      ) : (
        <div className="tl">
          {days.map((dayEntries, dayIndex) => {
            const dayDate = new Date(dayEntries[0].at);
            return (
              <section key={dayIndex} className="tl-day">
                <h3 className="tl-day-label">{formatDateFull(dayDate)}</h3>

                <ol className="tl-track">
                  {dayEntries.map((entry, index) => {
                    const event = EVENT[entry.action];
                    const date = new Date(entry.at);
                    return (
                      <li key={`${entry.at}-${index}`} className="tl-item">
                        <span className={`tl-node is-${event.tone}`} aria-hidden="true" />

                        <article className="tl-card">
                          <header className="tl-card-head">
                            <span className={`tl-badge is-${event.tone}`}>{event.label}</span>
                            <time className="tl-time" dateTime={entry.at}>
                              {clockOf(entry.at)}
                            </time>
                          </header>

                          <p className="tl-message">{entry.message}</p>

                          <footer className="tl-card-foot">
                            <span className="tl-actor">
                              <UserIcon size={12} />
                              {entry.actor ?? "system"}
                            </span>
                            <span className="tl-stamp">
                              {formatDateFull(date)}
                              {zoneOf(date) ? ` (${zoneOf(date)})` : ""}
                            </span>
                          </footer>
                        </article>
                      </li>
                    );
                  })}
                </ol>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
