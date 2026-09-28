"use client";

/**
 * The agency's two branches, in the order every screen shows them. Mirrors
 * `app.branches.BRANCHES`; the Royapettah desk handles Singapore and Malaysia,
 * Mount Road every other destination.
 */
export const BRANCHES = ["Royapettah", "Mount Road"] as const;

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
