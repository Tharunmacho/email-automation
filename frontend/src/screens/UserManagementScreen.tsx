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
import Image from "next/image";
import {
  Check,
  Eye,
  EyeOff,
  KeyRound,
  Loader2,
  Lock,
  Pencil,
  RefreshCw,
  Trash2,
  UserPlus,
  Users,
  X,
} from "lucide-react";

import Checkbox from "@/components/ui/Checkbox";
import Select from "@/components/ui/Select";
import { useModalFocus } from "@/components/ui/useModalFocus";
import { initialsOf, timeAgo } from "@/lib/format";
import {
  createUserAPI,
  deleteUserAPI,
  listUsersAPI,
  updateUserAPI,
  type ManagedUser,
} from "@/lib/api";

/**
 * Human labels for the page ids the API hands back.
 *
 * Every id in `PAGES` in `app/db/users.py` needs an entry here. Keeping the
 * vocabulary exact means an admin can never grant a page that has no screen.
 */
const PAGE_LABELS: Record<string, string> = {
  overview: "Overview",
  candidates: "Candidates",
  "candidate-entry": "Candidate Entry",
  attendance: "Attendance",
  payroll: "Payroll",
  staff: "Staff & Allocation",
  "job-orders": "Job Orders",
  sourcing: "Sourcing Hub",
  "b2b-enquiries": "B2B Enquiries",
  "data-management": "Data Management",
  users: "User Management",
  settings: "Settings",
};

const PAGE_DESCRIPTIONS: Record<string, string> = {
  overview: "Pipeline summary and performance overview",
  candidates: "Assigned candidate profiles and reviews",
  "candidate-entry": "Upload candidate documents for VeriIS extraction",
  attendance: "Daily punches, leave, grace time and exceptions",
  payroll: "Monthly salary, attendance deductions and payment status",
  staff: "Staff roster, workload and candidate assignment controls",
  "job-orders": "View and manage client job orders",
  sourcing: "View and manage sourcing clients",
  "b2b-enquiries": "View and manage incoming business enquiries",
  "data-management": "Manage jobs, countries and screening questions",
  users: "Create accounts and change page access — high privilege",
  settings: "Personal account details",
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
  { label: "General", pages: ["overview", "candidates", "candidate-entry", "attendance", "payroll", "staff", "users"] },
  {
    label: "Tools",
    pages: [
      "job-orders",
      "sourcing",
      "b2b-enquiries",
      "data-management",
    ],
  },
  { label: "Support", pages: ["settings"] },
];

/** The two roles, with what each one means stated against it rather than after it. */
const ROLE_OPTIONS = [
  {
    value: "staff",
    label: "Staff",
    hint: "Reviews the candidates allocated to them.",
  },
  {
    value: "manager",
    label: "Manager",
    hint: "Runs operations, attendance and payroll without account administration.",
  },
  {
    value: "admin",
    label: "Super Admin",
    hint: "Everything, including this page.",
  },
];

function PasswordInput({
  id,
  value,
  onChange,
  placeholder,
  disabled = false,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
  disabled?: boolean;
}) {
  const [visible, setVisible] = useState(false);

  return (
    <div className="um-password-control">
      <input
        id={id}
        className="modal-input"
        type={visible ? "text" : "password"}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        autoComplete="new-password"
        disabled={disabled}
      />
      <button
        type="button"
        className="um-password-reveal"
        disabled={disabled}
        onClick={() => setVisible((current) => !current)}
        aria-label={visible ? "Hide password" : "Show password"}
        title={visible ? "Hide password" : "Show password"}
      >
        {visible ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </div>
  );
}

/**
 * Pages a role already reaches without being granted anything.
 *
 * Mirrors `ROLE_DEFAULT_PAGES` in `app/db/users.py`, and is used only to grey
 * out a checkbox that would do nothing. The API is what actually decides; this
 * is here so an admin is not left ticking a box that cannot change anything.
 */
const ROLE_FLOOR: Record<string, string[]> = {
  admin: Object.keys(PAGE_LABELS),
  manager: Object.keys(PAGE_LABELS).filter((page) => page !== "users"),
  staff: ["candidates", "candidate-entry", "attendance", "settings"],
};

type Section = "create" | "manage";

interface Props {
  onActivity?: (message: string, type?: "info" | "success" | "error") => void;
  /** Refresh signed-in account details when its own record was edited. */
  onUserUpdated?: (user: ManagedUser) => void;
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
  onUserUpdated,
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
      setPages(
        Array.from(
          new Set(
            (res.pages ?? [])
              .filter((page) => page !== "activity")
              .map((page) => (page === "my-queue" ? "candidates" : page)),
          ),
        ),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Deferred into a microtask rather than called straight from the effect
    // body: `load` sets state, and doing that synchronously while the effect
    // runs makes React re-render on top of the render that scheduled it. The
    // flag drops the result of a run whose dependencies have already changed.
    let live = true;
    void (async () => {
      if (live) await load();
    })();
    return () => {
      live = false;
    };
  }, [load]);

  useEffect(() => {
    const onPhotoUpdated = (event: Event) => {
      const detail = (event as CustomEvent<{ id: string; photo: string }>).detail;
      if (!detail?.id || !detail.photo) return;
      setUsers((current) => current.map((account) => (
        account.id === detail.id ? { ...account, profile_photo: detail.photo } : account
      )));
    };
    window.addEventListener("adira-profile-photo-updated", onPhotoUpdated);
    return () => window.removeEventListener("adira-profile-photo-updated", onPhotoUpdated);
  }, []);

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
      const result = await updateUserAPI(user.id, patch);
      onUserUpdated?.(result.user);
      say(`${result.user.email} updated`, "success");
      setEditing(null);
      await load();
    } catch (err) {
      const message = err instanceof Error ? err.message : "Could not update the user";
      throw new Error(message);
    }
  };

  const remove = async (user: ManagedUser) => {
    if (user.id === currentUserId) {
      say("You cannot delete your own signed-in account.", "error");
      return;
    }
    if (
      !window.confirm(
        `Permanently delete ${user.name || user.email}? The account will be removed from the database and cannot sign in again.`,
      )
    ) {
      return;
    }
    try {
      const result = await deleteUserAPI(user.id);
      const moved = result.reallocated ? ` ${result.reallocated} candidate(s) were reassigned.` : "";
      say(`${user.email} deleted permanently.${moved}`, "success");
      if (editing?.id === user.id) setEditing(null);
      await load();
    } catch (err) {
      say(err instanceof Error ? err.message : "Could not delete the user", "error");
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
              {users.length} total accounts. A grant puts a page on someone&apos;s rail. It does not widen the data behind it.
            </p>
          </div>
        </header>

        {users.length === 0 ? (
          <div className="db-empty">
            <Users size={22} />
            <p className="db-empty-title">No accounts found</p>
          </div>
        ) : (
          <div className="ds-table-wrap is-ruled">
            <table className="ds-table is-ruled staff-table">
              <thead>
                <tr>
                  <th>Account</th>
                  <th>Mobile</th>
                  <th>Role</th>
                  <th>Added</th>
                  <th>Pages</th>
                  <th className="is-actions" aria-label="Actions">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id} className={user.active ? "" : "is-inactive"}>
                    <td>
                      <span className="ds-who">
                        <span className="ds-avatar" aria-hidden="true">
                          {user.profile_photo
                            ? <Image src={user.profile_photo} alt="" width={32} height={32} unoptimized />
                            : initialsOf(user.name || user.email)}
                        </span>
                        <span className="ds-who-text">
                          <strong>
                            {user.name || user.email}
                            {!user.active && <em className="staff-flag">deactivated</em>}
                          </strong>
                          {(user.role === "staff" || user.role === "manager") && (
                            <small className="crm-record-id">
                              Staff ID · {user.staff_code || `STF-${user.id.slice(-12).toUpperCase()}`}
                            </small>
                          )}
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
                        {user.role === "admin" ? "Super Admin" : user.role === "manager" ? "Manager" : "Staff"}
                      </span>
                    </td>
                    <td>{user.created_at ? timeAgo(user.created_at) : "—"}</td>
                    {/* The one column that can run long, so it is the one
                        allowed to wrap rather than widen the table. */}
                    <td className="is-wrap">
                      {user.role === "admin"
                        ? "Everything"
                        : Array.from(
                            new Set(
                              user.pages
                                .filter((page) => page !== "activity")
                                .map((page) => (page === "my-queue" ? "candidates" : page)),
                            ),
                          ).map((page) => PAGE_LABELS[page] ?? page).join(", ") || "—"}
                    </td>
                    <td className="is-actions">
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
                        <button
                          type="button"
                          className="ds-ghost-btn is-sm is-danger"
                          onClick={() => void remove(user)}
                          disabled={
                            user.id === currentUserId
                            || (user.role === "admin" && user.active && activeAdmins <= 1)
                          }
                          title={
                            user.id === currentUserId
                              ? "You cannot delete your own account"
                              : user.role === "admin" && user.active && activeAdmins <= 1
                                ? "Promote another administrator before deleting this account"
                                : "Permanently delete this account"
                          }
                        >
                          <Trash2 size={14} />
                          Delete
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
          onSave={(patch) => save(editing, patch)}
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
  const [passwordConfirmation, setPasswordConfirmation] = useState("");
  const [role, setRole] = useState("staff");
  const [grants, setGrants] = useState<string[]>([]);

  // Stated once, next to the control it governs, rather than being discovered
  // by pressing a disabled button and guessing why.
  const tooShort = password.length > 0 && password.length < 6;
  const passwordsDiffer =
    passwordConfirmation.length > 0 && passwordConfirmation !== password;
  const passwordConfirmed =
    password.length >= 6 && passwordConfirmation === password;
  const ready = Boolean(email.trim()) && passwordConfirmed;

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
            <PasswordInput
              id="u-password"
              value={password}
              onChange={setPassword}
              placeholder="At least 6 characters"
            />
            <p className={`modal-hint ${tooShort ? "is-warn" : ""}`}>
              {tooShort
                ? "Six characters minimum."
                : "Never shown back. You can set a new one from this screen at any time."}
            </p>
          </div>
          <div className="field-group">
            <label className="modal-label" htmlFor="u-password-confirmation">
              Confirm password
            </label>
            <PasswordInput
              id="u-password-confirmation"
              value={passwordConfirmation}
              onChange={setPasswordConfirmation}
              placeholder="Enter the same password again"
            />
            <p className={`modal-hint ${passwordsDiffer ? "is-warn" : ""}`}>
              {passwordsDiffer
                ? "The passwords do not match."
                : passwordConfirmed
                  ? "Password confirmed."
                  : "Required so a typing mistake cannot lock out the new user."}
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
  onSave: (patch: Parameters<typeof updateUserAPI>[1]) => Promise<void>;
}) {
  const [name, setName] = useState(user.name);
  const [email, setEmail] = useState(user.email);
  const [emailError, setEmailError] = useState("");
  const [phone, setPhone] = useState(user.phone ?? "");
  const [role, setRole] = useState(user.role);
  const [active, setActive] = useState(user.active);
  const [password, setPassword] = useState("");
  const [passwordConfirmation, setPasswordConfirmation] = useState("");
  const [grants, setGrants] = useState<string[]>(user.page_grants ?? []);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const close = () => {
    if (!saving) onCancel();
  };
  const dialogRef = useModalFocus<HTMLFormElement>(true, close);

  const locked = isLastAdmin;
  const passwordInvalid =
    password.length > 0 && (password.length < 6 || password !== passwordConfirmation);

  const validateEmail = (input: HTMLInputElement) => {
    setEmailError(input.validity.valueMissing
      ? "Email address is required."
      : input.validity.typeMismatch ? "Enter a valid email address." : "");
  };

  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (saving || passwordInvalid || !email.trim()) return;
    setSaving(true);
    setSaveError("");
    try {
      await onSave({
        email: email.trim(),
        name,
        phone,
        role,
        active,
        page_grants: grants,
        ...(password ? { password } : {}),
      });
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Could not save this account. Try again.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="modal-overlay active" onClick={close}>
      <form ref={dialogRef} className="modal-container is-narrow" role="dialog" aria-modal="true" aria-labelledby="edit-user-title" aria-describedby="edit-user-email" aria-busy={saving} tabIndex={-1} onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <div className="modal-header">
          <div>
            <h2 className="modal-title" id="edit-user-title">Edit account</h2>
            <p className="modal-subtitle" id="edit-user-email">{email.trim() || user.email}</p>
          </div>
          <button type="button" className="modal-close" onClick={close} disabled={saving} aria-label="Close account editor">
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
                disabled={saving}
              />
            </div>
            <div className="field-group">
              <label className="modal-label" htmlFor="e-email">Email address</label>
              <input
                id="e-email"
                className="modal-input"
                type="email"
                required
                autoComplete="email"
                value={email}
                disabled={saving}
                aria-invalid={emailError ? true : undefined}
                aria-describedby={emailError ? "e-email-hint e-email-error" : "e-email-hint"}
                onChange={(event) => {
                  setEmail(event.target.value);
                  if (emailError) validateEmail(event.currentTarget);
                }}
                onBlur={(event) => validateEmail(event.currentTarget)}
                onInvalid={(event) => validateEmail(event.currentTarget)}
              />
              <p className="modal-hint" id="e-email-hint">Used to sign in to this account.</p>
              {emailError && <p className="sh-form-error" id="e-email-error" role="alert">{emailError}</p>}
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
                disabled={saving}
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
                disabled={locked || saving}
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
                  disabled={locked || saving}
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
            <PasswordInput
              id="e-password"
              value={password}
              placeholder="Leave blank to keep the current one"
              onChange={setPassword}
              disabled={saving}
            />
          </div>

          {password && (
            <div className="field-group">
              <label className="modal-label" htmlFor="e-password-confirmation">
                <KeyRound size={12} /> Confirm new password
              </label>
              <PasswordInput
                id="e-password-confirmation"
                value={passwordConfirmation}
                placeholder="Enter the new password again"
                onChange={setPasswordConfirmation}
                disabled={saving}
              />
              <p className={`modal-hint ${passwordInvalid ? "is-warn" : ""}`}>
                {password.length < 6
                  ? "The new password must contain at least six characters."
                  : password !== passwordConfirmation
                    ? "The passwords do not match."
                    : "Password confirmed."}
              </p>
            </div>
          )}

          <PagePicker role={role} grants={grants} pages={pages} onChange={setGrants} disabled={saving} />
          {saveError && <p className="sh-form-error" role="alert">{saveError}</p>}
        </div>

        <div className="modal-footer">
          <button type="button" className="modal-cancel-btn" onClick={close} disabled={saving}>
            Cancel
          </button>
          <button
            type="submit"
            className="db-btn is-primary"
            disabled={passwordInvalid || saving}
          >
            {saving ? <Loader2 size={14} className="icon-spin" /> : <Check size={14} />}
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </div>
  );
}

/**
 * The page checkboxes.
 *
 * A page the role already reaches is shown ticked and locked. Staff always need
 * their assigned Candidates and personal Settings; every other destination is
 * controlled exactly by its checkbox.
 */
function PagePicker({
  role,
  grants,
  pages,
  onChange,
  disabled = false,
}: {
  role: string;
  grants: string[];
  pages: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
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
              : "Checked pages appear in their navigation after refresh. Unchecked pages are completely hidden. Candidates still shows only profiles assigned to that staff member."}
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
                  // Required role pages use a lock. Optional pages say exactly
                  // whether the user will see or not see the destination.
                  locked={isAdmin || inFloor}
                  disabled={disabled}
                  onChange={() => toggle(page)}
                  label={PAGE_LABELS[page] ?? page}
                  hint={
                    <>
                      {PAGE_DESCRIPTIONS[page] ?? "Application section"}
                      {" · "}
                      {inFloor && !isAdmin
                        ? "Required for staff — always visible"
                        : grants.includes(page)
                          ? "Visible in this user’s navigation"
                          : "Hidden completely from this user"}
                    </>
                  }
                />
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
