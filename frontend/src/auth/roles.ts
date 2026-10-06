import type { UserRole } from "../api/client";

export const ROLES: Record<string, UserRole> = {
  SUPER_ADMIN: "super_admin",
  SPA_ADMIN: "spa_admin",
  SPA_STAFF: "spa_staff",
};

export const ROLE_LABELS: Record<UserRole, string> = {
  super_admin: "6DM Super Admin",
  spa_admin: "Spa Admin",
  spa_staff: "Spa Staff",
};

/**
 * The single predicate that gates the entire 6DM Sales Agent surface — leads,
 * outbound campaigns, sales analytics and Dominic's calendar.
 *
 * Every navigation item, route guard and inline control derives from this, so
 * "hide the sales tabs from spa clients" is one rule rather than a decision
 * repeated in a dozen components. The server enforces the same boundary with
 * 403s; this only keeps a spa client from seeing controls that would fail.
 */
export const canAccessSalesAgent = (role: UserRole | undefined): boolean =>
  role === "super_admin";

/** Whether this principal may switch which tenant they are inspecting. */
export const canSwitchTenant = (role: UserRole | undefined): boolean =>
  role === "super_admin";

/** Spa roles are locked to their own tenant's dashboard. */
export const isSpaRole = (role: UserRole | undefined): boolean =>
  role === "spa_admin" || role === "spa_staff";

/** spa_staff is read-only; only an admin may edit receptionist settings. */
export const canEditSpaSettings = (role: UserRole | undefined): boolean =>
  role === "super_admin" || role === "spa_admin";
