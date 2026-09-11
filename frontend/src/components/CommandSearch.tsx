"use client";

import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { ArrowUpRight, CornerDownLeft, History, Search, UserRound, X, type LucideIcon } from "lucide-react";

import { useModalFocus } from "@/components/ui/useModalFocus";
import type { AuthUser, CandidateRecord } from "@/lib/api";
import { candidateNameOf } from "@/lib/format";
import { navGroupsFor, navPath, type NavId } from "@/lib/nav";
import "./command-search.css";

interface CommandSearchProps {
  user: AuthUser;
  /** The permission-scoped records already supplied to this workspace. */
  candidates?: CandidateRecord[];
  candidatesLoading?: boolean;
  onNavigate?: (id: NavId) => void;
  onOpenCandidate?: (id: string) => void;
}

type CommandResult = {
  id: string;
  kind: "recent" | "navigation" | "candidate";
  title: string;
  detail: string;
  icon: LucideIcon;
  destination: string;
};

const EMPTY_CANDIDATES: CandidateRecord[] = [];
const HISTORY_LIMIT = 5;
const CANDIDATE_LIMIT = 8;

function readRecentSearches(key: string): string[] {
  try {
    const value: unknown = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    return Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0 && item.length <= 160).slice(0, HISTORY_LIMIT)
      : [];
  } catch {
    return [];
  }
}

function Highlight({ text, query }: { text: string; query: string }) {
  const terms = query.trim().split(/\s+/).filter(Boolean);
  if (!terms.length) return text;
  const pattern = terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  const parts = text.split(new RegExp(`(${pattern})`, "gi"));
  return parts.map((part, index) => index % 2
    ? <mark key={index}>{part}</mark>
    : part);
}

function CommandDialog({
  user,
  candidates = EMPTY_CANDIDATES,
  candidatesLoading = false,
  onNavigate,
  onOpenCandidate,
  initialRecent,
  historyKey,
  onClose,
}: CommandSearchProps & { initialRecent: string[]; historyKey: string; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [recent, setRecent] = useState(initialRecent);
  const [active, setActive] = useState(0);
  const dialogRef = useModalFocus<HTMLDivElement>(true, onClose);
  const inputRef = useRef<HTMLInputElement>(null);
  const id = useId();
  const listId = `${id}-results`;
  const terms = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const navigation = useMemo(() => navGroupsFor(user.role, user.pages).flatMap((group) =>
    group.items.map((item) => ({ ...item, group: group.label }))), [user.role, user.pages]);
  const canSearchCandidates = Boolean(onOpenCandidate) && navigation.some((item) => item.id === "candidates");
  const candidateIndex = useMemo(() => canSearchCandidates ? candidates.map((candidate) => {
    const title = candidateNameOf(candidate);
    const profile = candidate.profile;
    const designation = profile.current_designation || profile.job_title || profile.job_preference;
    const email = profile.email || candidate.source_email?.from_addr;
    const detail = [candidate.candidate_code, designation, email].filter(Boolean).join(" · ") || "Open candidate profile";
    return {
      id: `candidate:${candidate.id}`,
      kind: "candidate" as const,
      title,
      detail,
      icon: UserRound,
      destination: candidate.id,
      searchText: [title, detail, ...(profile.skills ?? [])].join(" ").toLocaleLowerCase(),
    };
  }) : [], [candidates, canSearchCandidates]);
  const matches = (text: string) => terms.every((term) => text.toLocaleLowerCase().includes(term));
  const pageResults: CommandResult[] = navigation.filter((item) => matches(`${item.label} ${item.group}`)).map((item) => ({
    id: `navigation:${item.id}`,
    kind: "navigation",
    title: item.label,
    detail: item.group,
    icon: item.icon,
    destination: item.id,
  }));
  const candidateMatches = query.trim().length >= 2 ? candidateIndex.filter((candidate) => matches(candidate.searchText)) : [];
  const recentResults: CommandResult[] = terms.length === 0 ? recent.map((text) => ({
    id: `recent:${text}`,
    kind: "recent",
    title: text,
    detail: "Search again",
    icon: History,
    destination: text,
  })) : [];
  const groups = [
    { label: "Recent searches", results: recentResults },
    { label: "Go to", results: pageResults },
    { label: "Candidates", results: candidateMatches.slice(0, CANDIDATE_LIMIT) },
  ].filter((group) => group.results.length > 0);
  const results = groups.flatMap((group) => group.results);
  const activeIndex = Math.min(active, results.length - 1);
  const activeId = activeIndex >= 0 ? `${id}-result-${activeIndex}` : undefined;

  useEffect(() => {
    if (activeId) document.getElementById(activeId)?.scrollIntoView({ block: "nearest" });
  }, [activeId]);

  const rememberSearch = () => {
    const text = query.trim();
    if (!text) return;
    const next = [text, ...recent.filter((item) => item.toLocaleLowerCase() !== text.toLocaleLowerCase())].slice(0, HISTORY_LIMIT);
    try { window.localStorage.setItem(historyKey, JSON.stringify(next)); }
    catch { /* Search remains available when browser storage is disabled. */ }
  };

  const choose = (result: CommandResult) => {
    if (result.kind === "recent") {
      setQuery(result.destination);
      setActive(0);
      inputRef.current?.focus();
      return;
    }
    rememberSearch();
    onClose();
    if (result.kind === "candidate") onOpenCandidate?.(result.destination);
    else if (onNavigate) onNavigate(result.destination as NavId);
    else window.location.assign(navPath(result.destination as NavId));
  };

  const onInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (results.length) setActive((activeIndex + (event.key === "ArrowDown" ? 1 : -1) + results.length) % results.length);
    } else if (event.key === "Enter" && results[activeIndex]) {
      event.preventDefault();
      choose(results[activeIndex]);
    }
  };

  let resultIndex = 0;
  return (
    <div className="command-search-overlay" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div className="command-search-dialog" ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby={`${id}-title`} tabIndex={-1}>
        <h2 id={`${id}-title`} className="command-search-sr-only">Search your workspace</h2>
        <div className="command-search-input-row">
          <Search size={20} aria-hidden="true" />
          <input
            ref={inputRef}
            data-dialog-initial-focus
            type="text"
            role="combobox"
            aria-label="Search pages and candidates"
            aria-expanded="true"
            aria-autocomplete="list"
            aria-controls={listId}
            aria-activedescendant={activeId}
            aria-describedby={`${id}-scope`}
            placeholder={canSearchCandidates ? "Search pages, candidates, skills…" : "Search pages…"}
            autoComplete="off"
            spellCheck={false}
            maxLength={160}
            value={query}
            onChange={(event) => { setQuery(event.target.value); setActive(0); }}
            onKeyDown={onInputKeyDown}
          />
          <button type="button" className="command-search-close" aria-label="Close search" onClick={onClose}><X size={18} /></button>
        </div>
        <div className="command-search-body" role="region" aria-label="Scrollable search results" tabIndex={0}>
          <div id={listId} role="listbox" aria-label="Search results">
            {groups.map((group, groupIndex) => (
              <div key={group.label} className="command-search-group" role="group" aria-labelledby={`${id}-group-${groupIndex}`}>
                <div id={`${id}-group-${groupIndex}`} className="command-search-group-label">{group.label}</div>
                {group.results.map((result) => {
                  const index = resultIndex++;
                  const Icon = result.icon;
                  return (
                    <div
                      key={result.id}
                      id={`${id}-result-${index}`}
                      className={`command-search-result ${index === activeIndex ? "is-active" : ""}`}
                      role="option"
                      aria-selected={index === activeIndex}
                      onPointerMove={() => setActive(index)}
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => choose(result)}
                      onKeyDown={(event) => { if (event.key === "Enter") choose(result); }}
                    >
                      <span className={`command-search-result-icon is-${result.kind}`} aria-hidden="true"><Icon size={17} /></span>
                      <span className="command-search-result-copy"><strong><Highlight text={result.title} query={query} /></strong><small><Highlight text={result.detail} query={query} /></small></span>
                      <ArrowUpRight size={15} className="command-search-result-arrow" aria-hidden="true" />
                    </div>
                  );
                })}
              </div>
            ))}
          </div>
          {results.length === 0 && (
            <div className="command-search-empty">
              <Search size={25} aria-hidden="true" />
              <strong>{candidatesLoading && canSearchCandidates ? "Looking for candidates…" : "No matching results"}</strong>
              <p>Try a page name{canSearchCandidates ? ", candidate name or skill" : ""}, or use fewer words.</p>
              <button type="button" onClick={() => { setQuery(""); setActive(0); inputRef.current?.focus(); }}>Clear search</button>
            </div>
          )}
          {canSearchCandidates && query.trim().length > 0 && (
            <p className="command-search-candidate-hint">
              {query.trim().length < 2 ? "Type at least 2 characters to search candidates."
                : candidatesLoading ? "Candidate profiles are loading…"
                  : candidateMatches.length > CANDIDATE_LIMIT ? `Showing ${CANDIDATE_LIMIT} of ${candidateMatches.length} candidate matches. Add more detail to narrow your search.`
                    : candidateMatches.length === 0 && results.length > 0 ? "No candidates match this search." : null}
            </p>
          )}
        </div>
        <div className="command-search-footer">
          <p id={`${id}-scope`}>{canSearchCandidates ? "Pages and candidates in your workspace" : "Pages available to your account"}</p>
          {recent.length > 0 && query.length === 0 && <button type="button" onClick={() => {
            setRecent([]);
            setActive(0);
            try { window.localStorage.removeItem(historyKey); } catch { /* Optional local history. */ }
          }}>Clear recent searches</button>}
          <span className="command-search-keys" aria-hidden="true"><kbd>↑↓</kbd> Move <kbd><CornerDownLeft size={11} /></kbd> Open</span>
        </div>
        <span className="command-search-sr-only" role="status" aria-live="polite">{results.length} {results.length === 1 ? "result" : "results"} available.</span>
      </div>
    </div>
  );
}

export default function CommandSearch(props: CommandSearchProps) {
  const [open, setOpen] = useState(false);
  const [recent, setRecent] = useState<string[]>([]);
  const historyKey = `adira-command-search:${encodeURIComponent(props.user.id)}`;
  const openSearch = () => {
    setRecent(readRecentSearches(historyKey));
    setOpen(true);
  };

  useEffect(() => {
    const onShortcut = (event: globalThis.KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.altKey || event.key.toLowerCase() !== "k") return;
      // Keep the active form or photo editor's keyboard contract intact.
      if (!open && document.querySelector('[aria-modal="true"]')) return;
      event.preventDefault();
      if (open) setOpen(false);
      else {
        setRecent(readRecentSearches(historyKey));
        setOpen(true);
      }
    };
    document.addEventListener("keydown", onShortcut);
    return () => document.removeEventListener("keydown", onShortcut);
  }, [historyKey, open]);

  return (
    <>
      <button type="button" className="topbar-search command-search-trigger" aria-label="Search workspace (Ctrl or Command K)" aria-haspopup="dialog" aria-expanded={open} onClick={openSearch}>
        <Search size={17} aria-hidden="true" />
        <span>Search workspace…</span>
        <kbd className="topbar-kbd" aria-hidden="true">⌘ / Ctrl K</kbd>
      </button>
      {open && createPortal(<CommandDialog {...props} key={props.user.id} initialRecent={recent} historyKey={historyKey} onClose={() => setOpen(false)} />, document.body)}
    </>
  );
}
