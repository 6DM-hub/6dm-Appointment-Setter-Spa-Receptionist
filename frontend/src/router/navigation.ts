import {
  Activity,
  CalendarDays,
  Cog,
  Headphones,
  LineChart,
  PhoneCall,
  Target,
  UsersRound,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import type { UserRole } from "../api/client";

export type NavSectionId = "overview" | "sales" | "spa";

export interface NavItem {
  label: string;
  path: string;
  icon: LucideIcon;
  /** Roles allowed to see AND reach this route. */
  roles: UserRole[];
  description?: string;
}

export interface NavSection {
  id: NavSectionId;
  label: string | null;
  items: NavItem[];
}

const ALL_ROLES: UserRole[] = ["super_admin", "spa_admin", "spa_staff"];
const SALES_ONLY: UserRole[] = ["super_admin"];
const SPA_VIEWERS: UserRole[] = ["super_admin", "spa_admin", "spa_staff"];

/**
 * The whole navigation tree, with its access rules attached.
 *
 * `roles` here is consumed by both the sidebar (what to render) and the route
 * guards (what to admit), so a tab can never be visible-but-forbidden or
 * reachable-but-hidden. The 6DM Sales Agent section is `super_admin`-only, which
 * is why a spa client sees no trace of leads, campaigns, sales analytics or
 * Dominic's calendar — not a disabled item, no item at all.
 */
export const NAV_SECTIONS: NavSection[] = [
  {
    id: "overview",
    label: null,
    items: [
      {
        label: "Command center",
        path: "/",
        icon: Activity,
        roles: ALL_ROLES,
        description: "Live activity and today's numbers",
      },
    ],
  },
  {
    id: "sales",
    label: "6DM Sales Agent",
    items: [
      {
        label: "Leads",
        path: "/sales/leads",
        icon: Target,
        roles: SALES_ONLY,
        description: "B2B pipeline",
      },
      {
        label: "Outbound campaigns",
        path: "/sales/campaigns",
        icon: PhoneCall,
        roles: SALES_ONLY,
        description: "Dial queue and call triggers",
      },
      {
        label: "Sales analytics",
        path: "/sales/analytics",
        icon: LineChart,
        roles: SALES_ONLY,
        description: "Connect and conversion rates",
      },
      {
        label: "Dominic's calendar",
        path: "/sales/calendar",
        icon: CalendarDays,
        roles: SALES_ONLY,
        description: "Booked presentations",
      },
    ],
  },
  {
    id: "spa",
    label: "Spa Receptionist",
    items: [
      {
        label: "Inbound calls",
        path: "/spa/calls",
        icon: Headphones,
        roles: SPA_VIEWERS,
        description: "Call log and transcripts",
      },
      {
        label: "Guests",
        path: "/spa/guests",
        icon: UsersRound,
        roles: SPA_VIEWERS,
        description: "People who have called in",
      },
      {
        label: "Services",
        path: "/spa/services",
        icon: Sparkles,
        roles: SPA_VIEWERS,
        description: "Service menu and categories",
      },
      {
        label: "Receptionist settings",
        path: "/spa/settings",
        icon: Cog,
        roles: SPA_VIEWERS,
        description: "Prompt, staff and hours",
      },
    ],
  },
];

/** Sections filtered to what `role` may see, dropping any that empty out. */
export function navigationFor(role: UserRole | undefined): NavSection[] {
  if (!role) return [];
  return NAV_SECTIONS.map((section) => ({
    ...section,
    items: section.items.filter((item) => item.roles.includes(role)),
  })).filter((section) => section.items.length > 0);
}

/** Where a role lands after signing in, or when it hits a route it can't see. */
export function homePathFor(role: UserRole | undefined): string {
  return role ? "/" : "/login";
}

/** Access rules keyed by path, so guards and sidebar cannot disagree. */
export const ROUTE_ROLES: Record<string, UserRole[]> = Object.fromEntries(
  NAV_SECTIONS.flatMap((section) =>
    section.items.map((item) => [item.path, item.roles])
  )
);
