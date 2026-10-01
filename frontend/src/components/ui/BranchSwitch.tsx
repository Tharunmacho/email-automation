"use client";

import { useEffect, useState } from "react";

import Select from "@/components/ui/Select";
import { fetchBranches } from "@/lib/api";

/**
 * The agency's two original branches, in the order every screen shows them.
 * Mirrors `app.branches.BRANCHES`; the Royapettah desk handles Singapore and
 * Malaysia, Mount Road every other destination. Branches added in Data
 * Management follow them — read those with `useBranches`.
 */
export const BRANCHES = ["Royapettah", "Mount Road"] as const;

/** Fired after Data Management adds or removes a branch. */
export const BRANCHES_CHANGED = "adira:branches-changed";

/** Every branch, original and added, kept current across screens. */
export function useBranches(): string[] {
  const [branches, setBranches] = useState<string[]>([...BRANCHES]);
  useEffect(() => {
    let alive = true;
    const load = () => {
      fetchBranches()
        .then((result) => { if (alive && result.items.length) setBranches(result.items); })
        .catch(() => { /* keep the original branches */ });
    };
    load();
    window.addEventListener(BRANCHES_CHANGED, load);
    return () => { alive = false; window.removeEventListener(BRANCHES_CHANGED, load); };
  }, []);
  return branches;
}

/**
 * A branch dropdown. A stored value that is no longer a registered branch is
 * still offered, so editing an old record never silently changes its branch.
 */
export function BranchSelect({ id, value, onChange, branches, placeholder = "No branch" }: {
  id?: string;
  value: string;
  onChange: (branch: string) => void;
  branches: readonly string[];
  placeholder?: string;
}) {
  const names = branchOptions(value ? [...branches, value] : branches, false);
  return (
    <Select
      id={id}
      value={names.find((name) => sameBranch(name, value)) ?? ""}
      options={[{ value: "", label: placeholder }, ...names.map((name) => ({ value: name, label: name }))]}
      onChange={onChange}
      ariaLabel="Branch"
    />
  );
}

/** Case- and whitespace-insensitive branch comparison, as the backend does it. */
export function sameBranch(a?: string | null, b?: string | null): boolean {
  const norm = (value?: string | null) => (value ?? "").split(/\s+/).filter(Boolean).join(" ").toLowerCase();
  return norm(a) === norm(b);
}

/**
 * The fixed branches first, then any other branch name in use (a third office
 * typed into User Management), each once.
 */
export function branchOptions(extra: readonly string[] = [], includeFixed = true): string[] {
  const out: string[] = [];
  for (const name of [...(includeFixed ? BRANCHES : []), ...extra]) {
    if (name && !out.some((seen) => sameBranch(seen, name))) out.push(name);
  }
  return out;
}

interface Props {
  /** "" means every branch. */
  value: string;
  onChange: (branch: string) => void;
  branches?: readonly string[];
  /** Offer an "All branches" button ahead of the branches. */
  allowAll?: boolean;
  counts?: Record<string, number>;
  ariaLabel?: string;
}

/** A segmented control that switches a screen between branches. */
export default function BranchSwitch({
  value,
  onChange,
  branches = BRANCHES,
  allowAll = true,
  counts,
  ariaLabel = "Branch",
}: Props) {
  const options = [...(allowAll ? [""] : []), ...branches];
  return (
    <div className="scope-switch branch-switch" role="group" aria-label={ariaLabel}>
      {options.map((branch) => {
        const active = branch === "" ? value === "" : sameBranch(value, branch);
        const count = counts?.[branch];
        return (
          <button
            key={branch || "__all"}
            type="button"
            className={active ? "is-active" : ""}
            aria-pressed={active}
            onClick={() => onChange(branch)}
          >
            {branch || "All branches"}
            {typeof count === "number" && <span className="branch-switch-count">{count}</span>}
          </button>
        );
      })}
    </div>
  );
}
