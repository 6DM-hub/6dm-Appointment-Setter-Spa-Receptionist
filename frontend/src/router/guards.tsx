import { Navigate, useLocation } from "react-router-dom";
import { ShieldAlert } from "lucide-react";
import type { ReactNode } from "react";
import type { UserRole } from "../api/client";
import { useAuth } from "../auth/AuthContext";

function FullPageSpinner({ label }: { label: string }) {
  return (
    <div className="grid min-h-screen place-items-center bg-[#07111f] text-slate-400">
      <div className="flex flex-col items-center gap-3">
        <span className="h-8 w-8 animate-spin rounded-full border-2 border-slate-700 border-t-cyan-400" />
        <p className="text-xs">{label}</p>
      </div>
    </div>
  );
}

/** Signed-in check. Remembers where the user was headed so sign-in returns them. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) return <FullPageSpinner label="Restoring your session…" />;
  if (!user) return <Navigate to="/login" replace state={{ from: location }} />;
  return <>{children}</>;
}

/**
 * Role check for a single route.
 *
 * Renders a refusal rather than redirecting: a spa client who types
 * /sales/leads should be told plainly that it isn't part of their workspace,
 * not bounced somewhere confusing. Either way they never see sales data — the
 * page is not rendered, and the API would 403 the fetch regardless.
 */
export function RequireRole({
  roles,
  children,
}: {
  roles: UserRole[];
  children: ReactNode;
}) {
  const { role } = useAuth();

  if (role && !roles.includes(role)) {
    return <Forbidden />;
  }
  return <>{children}</>;
}

export function Forbidden() {
  return (
    <div className="grid place-items-center py-24 text-center">
      <div className="max-w-md">
        <span className="mx-auto mb-4 grid h-12 w-12 place-items-center rounded-xl bg-rose-400/10 text-rose-300">
          <ShieldAlert size={22} />
        </span>
        <h2 className="font-display text-xl font-semibold text-white">
          Not part of your workspace
        </h2>
        <p className="mt-2 text-sm text-slate-400">
          This area belongs to the 6DM sales team. Your account is scoped to your
          own spa's dashboard, call logs and receptionist settings.
        </p>
      </div>
    </div>
  );
}
