/**
 * Account roles, mirrored from `app/db/users.py`.
 *
 * A finance manager is a branch manager who also countersigns paid leave, LOP
 * and Extra OT for every branch, so anything a manager sees, they see too.
 */
export const MANAGER_ROLES = ["manager", "finance_manager"] as const;

export function isManagerRole(role: string | undefined | null): boolean {
  return role === "manager" || role === "finance_manager";
}

/** Staff and managers: the roles on attendance and payroll. */
export function isEmployeeRole(role: string | undefined | null): boolean {
  return role === "staff" || isManagerRole(role);
}

export function roleLabel(role: string | undefined | null): string {
  if (role === "admin") return "Super Admin";
  if (role === "finance_manager") return "Finance Manager";
  if (role === "manager") return "Manager";
  return "Staff";
}
