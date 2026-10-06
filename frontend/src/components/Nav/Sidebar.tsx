import { AudioLines, LogOut } from "lucide-react";
import { NavLink } from "react-router-dom";
import { useAuth } from "../../auth/AuthContext";
import { ROLE_LABELS } from "../../auth/roles";
import { navigationFor } from "../../router/navigation";

/**
 * Role-driven navigation.
 *
 * The item list comes straight from `navigationFor(role)`, so for a spa_admin or
 * spa_staff the "6DM Sales Agent" group is absent from the DOM entirely — not
 * hidden with CSS, not disabled. There is nothing to inspect or re-enable.
 */
export default function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { role, user, signOut } = useAuth();
  const sections = navigationFor(role);

  return (
    <div className="flex h-full w-64 flex-col border-r border-slate-800/80 bg-[#091525]">
      <div className="flex items-center gap-3 border-b border-slate-800/80 px-5 py-5">
        <span className="grid h-9 w-9 place-items-center rounded-xl bg-cyan-400 text-slate-950">
          <AudioLines size={18} />
        </span>
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold text-white">6DM Voice</p>
          <p className="truncate text-[10px] uppercase tracking-wider text-slate-500">
            {role ? ROLE_LABELS[role] : "—"}
          </p>
        </div>
      </div>

      <nav className="flex-1 space-y-6 overflow-y-auto px-3 py-5">
        {sections.map((section) => (
          <div key={section.id}>
            {section.label && (
              <p className="mb-2 px-3 text-[10px] font-bold uppercase tracking-[.18em] text-slate-600">
                {section.label}
              </p>
            )}
            <div className="space-y-1">
              {section.items.map((item) => (
                <NavLink
                  key={item.path}
                  to={item.path}
                  end={item.path === "/"}
                  onClick={onNavigate}
                  className={({ isActive }) =>
                    `flex items-center gap-3 rounded-xl px-3 py-2.5 text-xs font-medium transition ${
                      isActive
                        ? "bg-cyan-400/10 text-cyan-300"
                        : "text-slate-400 hover:bg-slate-800/60 hover:text-slate-100"
                    }`
                  }
                >
                  <item.icon size={16} />
                  <span className="truncate">{item.label}</span>
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>

      <div className="border-t border-slate-800/80 px-3 py-4">
        <div className="mb-2 px-2">
          <p className="truncate text-xs font-medium text-slate-300">
            {user?.full_name || user?.email}
          </p>
          <p className="truncate text-[10px] text-slate-600">{user?.email}</p>
        </div>
        <button
          onClick={signOut}
          className="flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-xs font-medium text-slate-400 transition hover:bg-slate-800/60 hover:text-slate-100"
        >
          <LogOut size={15} /> Sign out
        </button>
      </div>
    </div>
  );
}
