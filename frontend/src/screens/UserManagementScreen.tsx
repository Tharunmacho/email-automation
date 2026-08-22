"use client";

/**
 * Accounts, and which pages each of them can reach.
 *
 * Two things an admin does here, which is why the screen has two sections
 * rather than one list with a form bolted on: create a user, and decide what an
 * existing user sees.
 *
 * The one idea worth stating on the screen itself, because it is the thing
 * people get wrong about permission systems: **a grant adds a page and does not
 * widen the data behind it.** Ticking "Candidates" for a staff account puts the
 * candidates screen on their rail; it does not show them anybody else's
 * candidates, because that restriction lives in the API's own scoping and not
 * in this menu. An admin who believes otherwise will either grant too little
 * and field complaints, or grant freely and assume they have leaked the
 * database. Neither is true, so the screen says so.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  KeyRound,
  Loader2,
  Lock,
  Pencil,
  RefreshCw,
  ShieldCheck,
  UserPlus,
  Users,
  X,
} from "lucide-react";

import { initialsOf, timeAgo } from "@/lib/format";
import {
  createUserAPI,
  listUsersAPI,
  updateUserAPI,
  type ManagedUser,
} from "@/lib/api";

/** Human labels for the page ids the API hands back. */
const PAGE_LABELS: Record<string, string> = {
  overview: "Overview",
  candidates: "Candidates",
  "my-queue": "My Candidates",
  staff: "Staff & Allocation",
  "job-orders": "Job Orders",
  sourcing: "Sourcing Hub",
  "data-management": "Data Management",
  users: "User Management",
  activity: "Activity Logs",
  settings: "Settings",
};

/**
 * Pages a role already reaches without being granted anything.
 *
 * Mirrors `ROLE_DEFAULT_PAGES` in `app/db/users.py`, and is used only to grey
 * out a checkbox that would do nothing. The API is what actually decides; this
 * is here so an admin is not left ticking a box that cannot change anything.
 */
const ROLE_FLOOR: Record<string, string[]> = {
  admin: Object.keys(PAGE_LABELS),
  staff: ["my-queue"],
};

type Section = "create" | "manage";

interface Props {
  onActivity?: (message: string, type?: "info" | "success" | "error") => void;
  /** The signed-in admin, so the screen can refuse to let them lock themselves out. */
  currentUserId?: string;
}

export default function UserManagementScreen({ onActivity, currentUserId }: Props) {
  const [section, setSection] = useState<Section>("manage");
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [pages, setPages] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<ManagedUser | null>(null);

  const say = useCallback(
    (message: string, type: "info" | "success" | "error" = "info") => onActivity?.(message, type),
    [onActivity],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await listUsersAPI();
      setUsers(res.items ?? []);
      setPages(res.pages ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const activeAdmins = useMemo(
    () => users.filter((u) => u.role === "admin" && u.active).length,
    [users],
  );

  const create = async (draft: CreateDraft) => {
    try {
      await createUserAPI({
        email: draft.email.trim(),
        password: draft.password,
        name: draft.name.trim(),
        role: draft.role,
        page_grants: draft.grants,
      });
      say(`${draft.email} created`, "success");
      setSection("manage");
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not create the user", "error");
    }
  };

  const save = async (user: ManagedUser, patch: Parameters<typeof updateUserAPI>[1]) => {
    try {
      await updateUserAPI(user.id, patch);
      say(`${user.email} updated`, "success");
      setEditing(null);
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not update the user", "error");
    }
  };

  if (loading) {
    return (
      <div className="db-empty">
        <Loader2 size={18} className="icon-spin" />
        <div className="db-empty-title">Loading</div>
      </div>
    );
  }

  return (
    <div className="um-screen">
      <div className="db-tabs">
        <button
          type="button"
          className={`db-btn ${section === "manage" ? "is-primary" : ""}`}
          onClick={() => setSection("manage")}
        >
          <Users size={14} /> Manage users
          <span className="db-tab-count">{users.length}</span>
        </button>
        <button
          type="button"
          className={`db-btn ${section === "create" ? "is-primary" : ""}`}
          onClick={() => setSection("create")}
        >
          <UserPlus size={14} /> Create user
        </button>
        <button type="button" className="db-btn" onClick={() => void load()}>
          <RefreshCw size={14} /> Refresh
        </button>
      </div>

      {error && (
        <div className="db-card">
          <div className="db-card-head">
            <AlertTriangle size={16} />
            <div className="db-card-title">Could not load</div>
          </div>
          <div className="db-card-sub">{error}</div>
        </div>
      )}

      {section === "create" && (
        <CreateUserForm pages={pages} onCancel={() => setSection("manage")} onCreate={create} />
      )}

      {section === "manage" && (
        <div className="db-card">
          <div className="db-card-head">
            <ShieldCheck size={16} />
            <div className="db-card-title">Accounts</div>
          </div>
          <div className="db-card-sub">
            A grant puts a page on someone’s rail. It does not widen the data behind it — a staff
            member with the Candidates page still sees only the candidates allocated to them.
          </div>

          <table className="dm-table">
            <thead>
              <tr>
                <th>Person</th>
                <th>Role</th>
                <th>Pages they reach</th>
                <th>Added</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td className="dm-cell-strong">
                    <span className="dm-pill">{initialsOf(user.name || user.email)}</span>{" "}
                    {user.name || user.email}
                    <div className="dm-cell-muted">{user.email}</div>
                  </td>
                  <td>
                    {user.role === "admin" ? "Super Admin" : "Staff"}
                    {!user.active && <span className="dm-pill is-off"> · disabled</span>}
                  </td>
                  <td className="dm-cell-muted">
                    {user.role === "admin"
                      ? "Everything"
                      : user.pages.map((p) => PAGE_LABELS[p] ?? p).join(", ")}
                  </td>
                  <td className="dm-cell-muted">
                    {user.created_at ? timeAgo(user.created_at) : "—"}
                  </td>
                  <td className="dm-cell-actions">
                    <button type="button" className="db-btn" onClick={() => setEditing(user)}>
                      <Pencil size={13} /> Edit
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          {activeAdmins <= 1 && (
            <div className="db-card-sub">
              <Lock size={12} /> One active administrator. They cannot be demoted or disabled until
              somebody else is promoted — there would be no way back in.
            </div>
          )}
        </div>
      )}

      {editing && (
        <EditUserModal
          user={editing}
          pages={pages}
          isSelf={editing.id === currentUserId}
          isLastAdmin={editing.role === "admin" && activeAdmins <= 1}
          onCancel={() => setEditing(null)}
          onSave={(patch) => void save(editing, patch)}
        />
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */

interface CreateDraft {
  email: string;
  password: string;
  name: string;
  role: string;
  grants: string[];
}

function CreateUserForm({
  pages,
  onCancel,
  onCreate,
}: {
  pages: string[];
  onCancel: () => void;
  onCreate: (draft: CreateDraft) => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("staff");
  const [grants, setGrants] = useState<string[]>([]);

  return (
    <div className="db-card">
      <div className="db-card-head">
        <UserPlus size={16} />
        <div className="db-card-title">Create a user</div>
      </div>

      <div className="modal-row-2">
        <div className="field-group">
          <label className="modal-label" htmlFor="u-name">
            Name
          </label>
          <input
            id="u-name"
            className="modal-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Priya Raman"
          />
        </div>
        <div className="field-group">
          <label className="modal-label" htmlFor="u-email">
            Email
          </label>
          <input
            id="u-email"
            className="modal-input"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="priya@example.com"
          />
        </div>
      </div>

      <div className="modal-row-2">
        <div className="field-group">
          <label className="modal-label" htmlFor="u-password">
            Password
          </label>
          <input
            id="u-password"
            className="modal-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
          <div className="modal-hint">
            They can be given a new one from this screen at any time; it is never shown back.
          </div>
        </div>
        <div className="field-group">
          <label className="modal-label" htmlFor="u-role">
            Role
          </label>
          <select
            id="u-role"
            className="modal-select"
            value={role}
            onChange={(e) => setRole(e.target.value)}
          >
            <option value="staff">Staff — reviews the candidates allocated to them</option>
            <option value="admin">Super Admin — everything, including this page</option>
          </select>
        </div>
      </div>

      <PagePicker role={role} grants={grants} pages={pages} onChange={setGrants} />

      <div className="modal-footer">
        <button type="button" className="modal-cancel-btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="db-btn is-primary"
          disabled={!email.trim() || password.length < 6}
          onClick={() => onCreate({ email, password, name, role, grants })}
        >
          <Check size={14} /> Create
        </button>
      </div>
    </div>
  );
}

function EditUserModal({
  user,
  pages,
  isSelf,
  isLastAdmin,
  onCancel,
  onSave,
}: {
  user: ManagedUser;
  pages: string[];
  isSelf: boolean;
  isLastAdmin: boolean;
  onCancel: () => void;
  onSave: (patch: {
    name?: string;
    role?: string;
    active?: boolean;
    password?: string;
    page_grants?: string[];
  }) => void;
}) {
  const [name, setName] = useState(user.name);
  const [role, setRole] = useState(user.role);
  const [active, setActive] = useState(user.active);
  const [password, setPassword] = useState("");
  const [grants, setGrants] = useState<string[]>(user.page_grants ?? []);

  const locked = isLastAdmin;

  return (
    <div className="modal-container is-narrow">
      <div className="modal-header">
        <div className="modal-label">{user.email}</div>
        <button type="button" className="modal-close" onClick={onCancel}>
          <X size={16} />
        </button>
      </div>

      <div className="modal-body">
        <div className="field-group">
          <label className="modal-label" htmlFor="e-name">
            Name
          </label>
          <input
            id="e-name"
            className="modal-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div className="modal-row-2">
          <div className="field-group">
            <label className="modal-label" htmlFor="e-role">
              Role
            </label>
            <select
              id="e-role"
              className="modal-select"
              value={role}
              disabled={locked}
              onChange={(e) => setRole(e.target.value)}
            >
              <option value="staff">Staff</option>
              <option value="admin">Super Admin</option>
            </select>
          </div>
          <div className="field-group">
            <label className="modal-label">
              <input
                type="checkbox"
                checked={active}
                disabled={locked}
                onChange={(e) => setActive(e.target.checked)}
              />{" "}
              Account is active
            </label>
            {isSelf && <div className="modal-hint">This is you.</div>}
          </div>
        </div>

        {locked && (
          <div className="modal-hint">
            <Lock size={12} /> The last active administrator. Promote somebody else before changing
            this account’s role or disabling it.
          </div>
        )}

        <div className="field-group">
          <label className="modal-label" htmlFor="e-password">
            <KeyRound size={12} /> New password
          </label>
          <input
            id="e-password"
            className="modal-input"
            type="password"
            value={password}
            placeholder="leave blank to keep the current one"
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <PagePicker role={role} grants={grants} pages={pages} onChange={setGrants} />
      </div>

      <div className="modal-footer">
        <button type="button" className="modal-cancel-btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="db-btn is-primary"
          onClick={() =>
            onSave({
              name,
              role,
              active,
              page_grants: grants,
              ...(password ? { password } : {}),
            })
          }
        >
          <Check size={14} /> Save
        </button>
      </div>
    </div>
  );
}

/**
 * The page checkboxes.
 *
 * A page the role already reaches is shown ticked and disabled rather than
 * hidden: an admin looking at a staff account needs to see that "My Candidates"
 * is there without being able to take it away, because taking it away is not
 * something this system can express — grants add.
 */
function PagePicker({
  role,
  grants,
  pages,
  onChange,
}: {
  role: string;
  grants: string[];
  pages: string[];
  onChange: (next: string[]) => void;
}) {
  const floor = new Set(ROLE_FLOOR[role] ?? []);
  const isAdmin = role === "admin";

  const toggle = (page: string) => {
    onChange(grants.includes(page) ? grants.filter((p) => p !== page) : [...grants, page]);
  };

  return (
    <div className="field-group">
      <div className="modal-label">Pages this account can reach</div>
      <div className="modal-hint">
        {isAdmin
          ? "A Super Admin reaches every page, including this one. Nothing to choose."
          : "Ticking a page puts it on their rail. It does not change what the page shows them — a staff member still sees only the candidates allocated to them."}
      </div>

      <div className="um-page-grid">
        {pages.map((page) => {
          const inFloor = floor.has(page);
          const checked = inFloor || grants.includes(page);
          return (
            <label key={page} className="modal-label um-page-row">
              <input
                type="checkbox"
                checked={checked}
                disabled={isAdmin || inFloor}
                onChange={() => toggle(page)}
              />{" "}
              {PAGE_LABELS[page] ?? page}
              {inFloor && !isAdmin && <span className="dm-cell-muted"> — always</span>}
            </label>
          );
        })}
      </div>
    </div>
  );
}
