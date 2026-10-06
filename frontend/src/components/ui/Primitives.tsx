import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

export function PageHeader({
  eyebrow,
  title,
  subtitle,
  actions,
}: {
  eyebrow?: string;
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
      <div>
        {eyebrow && (
          <p className="mb-2 text-[10px] font-bold uppercase tracking-[.22em] text-slate-500">
            {eyebrow}
          </p>
        )}
        <h2 className="font-display text-2xl font-semibold tracking-tight text-white">
          {title}
        </h2>
        {subtitle && <p className="mt-2 text-sm text-slate-400">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
  padded = true,
}: {
  title?: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  padded?: boolean;
}) {
  return (
    <section className="overflow-hidden rounded-2xl border border-slate-800 bg-[#0b1a2c]">
      {(title || actions) && (
        <div className="flex flex-col gap-2 border-b border-slate-800 px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
          <div>
            {title && <h3 className="text-sm font-semibold text-white">{title}</h3>}
            {subtitle && <p className="mt-1 text-[11px] text-slate-500">{subtitle}</p>}
          </div>
          {actions}
        </div>
      )}
      <div className={padded ? "p-5" : undefined}>{children}</div>
    </section>
  );
}

export function StatCard({
  label,
  value,
  sub,
  icon: Icon,
  accent = "cyan",
}: {
  label: string;
  value: string | number;
  sub?: string;
  icon?: LucideIcon;
  accent?: "cyan" | "emerald" | "amber" | "rose";
}) {
  const accents = {
    cyan: "bg-cyan-400/10 text-cyan-300",
    emerald: "bg-emerald-400/10 text-emerald-300",
    amber: "bg-amber-400/10 text-amber-300",
    rose: "bg-rose-400/10 text-rose-300",
  } as const;

  return (
    <div className="rounded-2xl border border-slate-800 bg-[#0b1a2c] p-5 transition hover:border-slate-700">
      <div className="mb-6 flex items-center justify-between">
        <p className="text-xs font-medium text-slate-400">{label}</p>
        {Icon && (
          <span className={`grid h-8 w-8 place-items-center rounded-lg ${accents[accent]}`}>
            <Icon size={16} />
          </span>
        )}
      </div>
      <p className="font-display text-3xl font-semibold tracking-tight text-white">
        {value}
      </p>
      {sub && <p className="mt-2 text-[11px] text-slate-500">{sub}</p>}
    </div>
  );
}

/**
 * One component for the loading / error / empty states every data page needs,
 * so they read the same everywhere instead of each page inventing its own.
 */
export function StateBlock({
  loading,
  error,
  empty,
  emptyLabel = "Nothing here yet.",
  children,
}: {
  loading?: boolean;
  error?: string | null;
  empty?: boolean;
  emptyLabel?: string;
  children: ReactNode;
}) {
  if (loading) {
    return (
      <div className="flex items-center gap-3 p-6 text-xs text-slate-500">
        <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-700 border-t-cyan-400" />
        Loading…
      </div>
    );
  }
  if (error) {
    return (
      <div className="m-5 rounded-xl border border-rose-400/20 bg-rose-400/5 p-4 text-xs text-rose-300">
        {error}
      </div>
    );
  }
  if (empty) {
    return <div className="p-6 text-center text-xs text-slate-500">{emptyLabel}</div>;
  }
  return <>{children}</>;
}
