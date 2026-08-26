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
  Check,
  KeyRound,
  Lock,
  Pencil,
  RefreshCw,
  UserPlus,
  Users,
  X,
} from "lucide-react";

import Checkbox from "@/components/ui/Checkbox";
import Select from "@/components/ui/Select";
import { initialsOf, timeAgo } from "@/lib/format";
import {
  createUserAPI,
  listUsersAPI,
  updateUserAPI,
  type ManagedUser,
} from "@/lib/api";

/**
 * Human labels for the page ids the API hands back.
 *
 * Every id in `PAGES` in `app/db/users.py` needs an entry here. Two of them —
 * `visualizer` and `resume-parser` — had none, so the permission list was
 * printing the raw ids in among the labelled rows.
 */
const PAGE_LABELS: Record<string, string> = {
  overview: "Overview",
  candidates: "Candidates",
  "my-queue": "My Candidates",
  staff: "Staff & Allocation",
  "job-orders": "Job Orders",
  sourcing: "Sourcing Hub",
  "b2b-enquiries": "B2B Enquiries",
  "data-management": "Data Management",
  users: "User Management",
  visualizer: "Visualizer",
  "resume-parser": "Résumé Parser",
  activity: "Activity Logs",
  settings: "Settings",
};

/**
 * The permission list, grouped the way the rail is grouped.
 *
 * One flat run of twelve checkboxes gave an admin nothing to navigate by — the
 * order was the API's tuple order, which is not an order that means anything on
 * screen. These are the rail's own groups, so what you tick here is laid out
 * like the thing it produces.
 *
 * An id the API sends that is not listed here still gets rendered, under
 * "Other" — a permission that silently vanishes from this screen because
 * somebody added it to the backend and not to this constant is worse than an
 * ungrouped row.
 */
const PAGE_GROUPS: { label: string; pages: string[] }[] = [
  { label: "Workspace", pages: ["my-queue"] },
  { label: "General", pages: ["overview", "candidates", "staff", "users"] },
  {
    label: "Tools",
    pages: [
      "job-orders",
      "sourcing",
      "b2b-enquiries",
      "data-management",
      "visualizer",
      "resume-parser",
    ],
  },
  { label: "Support", pages: ["activity", "settings"] },
];

/** The two roles, with what each one means stated against it rather than after it. */
const ROLE_OPTIONS = [
  {
    value: "staff",
    label: "Staff",
    hint: "Reviews the candidates allocated to them.",
  },
  {
    value: "admin",
    label: "Super Admin",
    hint: "Everything, including this page.",
  },
];

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
  /**
   * Open straight onto the create form.
   *
   * Set when the admin arrived by asking for a new account somewhere else —
   * "Add staff" on the staff console — so the click that expressed the intent
   * lands on the form rather than on the roster with the form one click away.
   *
   * Read once, at mount: the shell unmounts this screen whenever the rail moves
   * elsewhere, so a later, ordinary visit to User Management mounts it afresh
   * with the flag already cleared and opens on the roster.
   */
  openCreate?: boolean;
}

export default function UserManagementScreen({
  onActivity,
  currentUserId,
  openCreate = false,
}: Props) {
  const [section, setSection] = useState<Section>(openCreate ? "create" : "manage");
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
        phone: draft.phone.trim(),
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
      <section className="db-card">
        <span className="app-boot-spinner" />
      </section>
    );
  }


  return (
    <div className="staff-admin">
      {error && (
        <section className="db-card">
          <h3 className="db-card-title">Could not load</h3>
          <p className="db-card-sub">{error}</p>
          <button type="button" className="db-btn" onClick={() => void load()}>
            Try again
          </button>
        </section>
      )}

      <header className="ds-head">
        <div>
          <h1 className="ds-head-title">User management</h1>
          <p className="ds-head-sub">
            Accounts, roles, and which pages each person can reach.
          </p>
        </div>

        <div className="ds-head-actions">
          <button type="button" className="ds-ghost-btn" onClick={() => void load()} title="Refresh">
            <RefreshCw size={15} /> Refresh
          </button>
          <button type="button" className="ds-primary-btn" onClick={() => setSection("create")}>
            <UserPlus size={15} /> Create user
          </button>
        </div>
      </header>

      {section === "create" && (
        <CreateUserForm pages={pages} onCancel={() => setSection("manage")} onCreate={create} />
      )}

      <section className="db-card">
        <header className="db-card-head">
          <div>
            <h3 className="db-card-title">Accounts matrix</h3>
            <p className="db-card-sub">
              {users.length} total accounts. A grant puts a page on someone's rail. It does not widen the data behind it.
            </p>
          </div>
        </header>

        {users.length === 0 ? (
          <div className="db-empty">
            <Users size={22} />
            <p className="db-empty-title">No accounts found</p>
          </div>
        ) : (
          <div className="ds-table-wrap">
            <table className="ds-table staff-table">
              <thead>
                <tr>
                  <th>Account</th>
                  <th>Mobile</th>
                  <th>Role</th>
                  <th>Added</th>
                  <th>Pages</th>
                  <th aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id} className={user.active ? "" : "is-inactive"}>
                    <td>
                      <span className="ds-who">
                        <span className="ds-avatar" aria-hidden="true">
                          {initialsOf(user.name || user.email)}
                        </span>
                        <span className="ds-who-text">
                          <strong>
                            {user.name || user.email}
                            {!user.active && <em className="staff-flag">deactivated</em>}
                          </strong>
                          <small>{user.email}</small>
                        </span>
                      </span>
                    </td>
                    {/* Dialable where the browser can, plain text where it
                        cannot — an admin reading this column is usually about
                        to ring the person in it. */}
                    <td>
                      {user.phone ? (
                        <a className="um-phone" href={`tel:${user.phone.replace(/\s+/g, "")}`}>
                          {user.phone}
                        </a>
                      ) : (
                        <span className="um-phone is-empty">—</span>
                      )}
                    </td>
                    <td>
                      <span className={`ds-status ${user.role === "admin" ? "is-info" : "is-ok"}`}>
                        <i aria-hidden="true" />
                        {user.role === "admin" ? "Super Admin" : "Staff"}
                      </span>
                    </td>
                    <td>{user.created_at ? timeAgo(user.created_at) : "—"}</td>
                    {/* The one column that can run long, so it is the one
                        allowed to wrap rather than widen the table. */}
                    <td className="is-wrap">
                      {user.role === "admin"
                        ? "Everything"
                        : user.pages.map((p) => PAGE_LABELS[p] ?? p).join(", ") || "—"}
                    </td>
                    <td>
                      <div className="staff-actions">
                        <button
                          type="button"
                          className="ds-ghost-btn is-sm"
                          onClick={() => setEditing(user)}
                          title="Edit account details and permissions"
                        >
                          <Pencil size={14} />
                          Edit
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {activeAdmins <= 1 && (
          <div className="db-card-sub" style={{ marginTop: "1rem" }}>
            <Lock size={12} /> One active administrator. They cannot be demoted or disabled until
            somebody else is promoted — there would be no way back in.
          </div>
        )}
      </section>

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
  /** Free text. Optional, and no format is imposed — see the field's hint. */
  phone: string;
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
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState("staff");
  const [grants, setGrants] = useState<string[]>([]);

  // Stated once, next to the control it governs, rather than being discovered
  // by pressing a disabled button and guessing why.
  const tooShort = password.length > 0 && password.length < 6;
  const ready = Boolean(email.trim()) && password.length >= 6;

  return (
    <div className="db-card um-create-card">
      <div className="db-card-head">
        <div className="um-create-head">
          <UserPlus size={16} />
          <div>
            <h3 className="db-card-title">Create a user</h3>
            <p className="db-card-sub">They can sign in as soon as this is saved.</p>
          </div>
        </div>
      </div>

      {/* Two columns of equal-width fields, and every label on the same
          baseline as the one beside it — which is what `.um-form-grid` buys
          over the old `.modal-row-2`: a hint under one field no longer pushes
          its neighbour's input out of line, because the hint sits in the
          field's own row rather than in the grid's. */}
      <div className="um-form">
        <div className="um-form-grid">
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
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="priya@example.com"
            />
          </div>

          <div className="field-group">
            <label className="modal-label" htmlFor="u-phone">
              Mobile number
            </label>
            <input
              id="u-phone"
              className="modal-input"
              type="tel"
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+91 98765 43210"
            />
            <p className="modal-hint">
              Optional, and stored as typed — country code, extension or a second number all fit.
            </p>
          </div>
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
              placeholder="At least 6 characters"
            />
            <p className={`modal-hint ${tooShort ? "is-warn" : ""}`}>
              {tooShort
                ? "Six characters minimum."
                : "Never shown back. You can set a new one from this screen at any time."}
            </p>
          </div>
          <div className="field-group">
            <span className="modal-label">Role</span>
            <Select
              value={role}
              options={ROLE_OPTIONS}
              onChange={setRole}
              ariaLabel="Role"
            />
            <p className="modal-hint">
              {role === "admin"
                ? "Full access, and can edit these permissions."
                : "Sees only the candidates allocated to them."}
            </p>
          </div>
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
          disabled={!ready}
          onClick={() => onCreate({ email, password, name, phone, role, grants })}
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
    phone?: string;
  }) => void;
}) {
  const [name, setName] = useState(user.name);
  const [phone, setPhone] = useState(user.phone ?? "");
  const [role, setRole] = useState(user.role);
  const [active, setActive] = useState(user.active);
  const [password, setPassword] = useState("");
  const [grants, setGrants] = useState<string[]>(user.page_grants ?? []);

  const locked = isLastAdmin;

  return (
    <div className="modal-overlay active" onClick={onCancel}>
      <div className="modal-container is-narrow" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-label">{user.email}</div>
          <button type="button" className="modal-close" onClick={onCancel}>
            <X size={16} />
          </button>
        </div>

        <div className="modal-body">
          <div className="um-form-grid">
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
            <div className="field-group">
              <label className="modal-label" htmlFor="e-phone">
                Mobile number
              </label>
              <input
                id="e-phone"
                className="modal-input"
                type="tel"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
                placeholder="+91 98765 43210"
              />
            </div>
          </div>

          <div className="um-form-grid">
            <div className="field-group">
              <span className="modal-label">Role</span>
              <Select
                value={role}
                options={ROLE_OPTIONS}
                onChange={setRole}
                disabled={locked}
                ariaLabel="Role"
              />
            </div>
            <div className="field-group">
              <span className="modal-label">Status</span>
              {/* The checkbox is the control, so it sits where an input would —
                  on the field's own row, aligned with the dropdown beside it.
                  It used to live inside the label, which put it half a line
                  above every other control in the form. */}
              <div className="um-form-control">
                <Checkbox
                  checked={active}
                  disabled={locked}
                  onChange={setActive}
                  label="Account is active"
                  hint={
                    active
                      ? "Receives new allocations."
                      : "Keeps existing work, receives nothing new."
                  }
                />
              </div>
              {isSelf && <p className="modal-hint">This is you.</p>}
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
                phone,
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

  // Grouped the way the rail is grouped, and only over the ids the API actually
  // sent. Anything it sent that no group claims lands in "Other" rather than
  // being dropped — see PAGE_GROUPS.
  const available = new Set(pages);
  const claimed = new Set(PAGE_GROUPS.flatMap((group) => group.pages));
  const groups = [
    ...PAGE_GROUPS.map((group) => ({
      label: group.label,
      pages: group.pages.filter((page) => available.has(page)),
    })),
    { label: "Other", pages: pages.filter((page) => !claimed.has(page)) },
  ].filter((group) => group.pages.length > 0);

  const grantedCount = pages.filter((page) => floor.has(page) || grants.includes(page)).length;

  return (
    <div className="um-pages">
      <div className="um-pages-head">
        <div>
          <span className="modal-label">Pages this account can reach</span>
          <p className="modal-hint">
            {isAdmin
              ? "A Super Admin reaches every page, including this one. Nothing to choose."
              : "Ticking a page puts it on their rail. It does not change what the page shows them — a staff member still sees only the candidates allocated to them."}
          </p>
        </div>
        {/* A running count, because the answer to "what does this account
            reach?" is otherwise a manual tally of twelve checkboxes. */}
        <span className="um-pages-count">
          {isAdmin ? "All pages" : `${grantedCount} of ${pages.length}`}
        </span>
      </div>

      <div className="um-page-grid">
        {groups.map((group) => (
          <div key={group.label} className="um-page-group">
            <p className="um-page-group-label">{group.label}</p>
            {group.pages.map((page) => {
              const inFloor = floor.has(page);
              return (
                <Checkbox
                  key={page}
                  checked={grants.includes(page)}
                  // An admin reaches everything and a staff member always
                  // reaches their own queue: both are "on because the role says
                  // so", which is what `locked` draws — a padlock, not a greyed
                  // tick that reads as unavailable.
                  locked={isAdmin || inFloor}
                  onChange={() => toggle(page)}
                  label={PAGE_LABELS[page] ?? page}
                  hint={inFloor && !isAdmin ? "Always on their rail" : undefined}
                />
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
