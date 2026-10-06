import { useEffect, useState } from "react";
import { Building2, Check, ChevronDown, Plus, Zap } from "lucide-react";
import { fetchSpaAccounts, type SpaAccountSummary } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { canSwitchTenant } from "../../auth/roles";
import SpaOnboardingForm from "./SpaOnboardingForm";

/**
 * Super-admin-only tenant switcher.
 *
 * Selecting a spa sets the `X-Tenant-Id` header on every subsequent request, so
 * calls, guests, appointments and analytics all resolve to that tenant. Renders
 * nothing for spa roles — they have exactly one tenant and the API rejects the
 * header for anyone else.
 */
export default function TenantSwitcher() {
  const { role, impersonatedTenantId, inspectTenant } = useAuth();
  const [open, setOpen] = useState(false);
  const [spas, setSpas] = useState<SpaAccountSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [onboarding, setOnboarding] = useState(false);

  const allowed = canSwitchTenant(role);

  useEffect(() => {
    if (!allowed) return;
    fetchSpaAccounts()
      .then(setSpas)
      .catch(() => setError("Unable to load spa accounts"));
  }, [allowed]);

  if (!allowed) return null;

  const active = spas.find((spa) => spa.id === impersonatedTenantId);
  const label = active ? active.name : "6DM sales workspace";

  const select = (tenantId: string | null) => {
    inspectTenant(tenantId);
    setOpen(false);
    // Every mounted page holds tenant-scoped data it fetched under the old
    // header; a reload is the honest way to swap the whole view at once.
    window.location.reload();
  };

  return (
    <>
      <div className="relative">
      <button
        onClick={() => setOpen((value) => !value)}
        className="flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-900/80 px-3 py-2 text-xs font-semibold text-slate-200 transition hover:border-slate-600"
      >
        {active ? (
          <Building2 size={14} className="text-emerald-300" />
        ) : (
          <Zap size={14} className="text-cyan-300" />
        )}
        <span className="max-w-[180px] truncate">{label}</span>
        <ChevronDown size={14} className="text-slate-500" />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="absolute right-0 z-40 mt-2 w-72 overflow-hidden rounded-xl border border-slate-700 bg-[#0b1a2c] shadow-2xl">
            <p className="border-b border-slate-800 px-4 py-2.5 text-[10px] font-bold uppercase tracking-[.18em] text-slate-500">
              Inspect workspace
            </p>

            <button
              onClick={() => select(null)}
              className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-slate-800/60"
            >
              <Zap size={15} className="text-cyan-300" />
              <span className="flex-1">
                <span className="block text-xs font-semibold text-slate-100">
                  6DM sales workspace
                </span>
                <span className="block text-[10px] text-slate-500">
                  Outbound B2B leads and Dominic's calendar
                </span>
              </span>
              {!impersonatedTenantId && <Check size={14} className="text-cyan-300" />}
            </button>

            <div className="max-h-72 overflow-y-auto border-t border-slate-800">
              {error && <p className="px-4 py-3 text-[11px] text-rose-300">{error}</p>}
              {!error && spas.length === 0 && (
                <p className="px-4 py-3 text-[11px] text-slate-500">
                  No spa accounts yet. Create one to onboard a client.
                </p>
              )}
              {spas.map((spa) => (
                <button
                  key={spa.id}
                  onClick={() => select(spa.id)}
                  className="flex w-full items-center gap-3 px-4 py-3 text-left transition hover:bg-slate-800/60"
                >
                  <Building2 size={15} className="text-emerald-300" />
                  <span className="flex-1 min-w-0">
                    <span className="block truncate text-xs font-semibold text-slate-100">
                      {spa.name}
                    </span>
                    <span className="block truncate text-[10px] text-slate-500">
                      {spa.twilio_phone_number || "no number assigned"} ·{" "}
                      {spa.booking_provider.replace(/_/g, " ")}
                    </span>
                  </span>
                  {impersonatedTenantId === spa.id && (
                    <Check size={14} className="text-emerald-300" />
                  )}
                </button>
              ))}
            </div>
            <button
              onClick={() => { setOpen(false); setOnboarding(true); }}
              className="flex w-full items-center gap-2 border-t border-slate-800 px-4 py-3 text-left text-xs font-semibold text-emerald-300 hover:bg-slate-800/60"
            >
              <Plus size={15} /> Create spa account
            </button>
          </div>
        </>
      )}
      </div>
      {onboarding && <SpaOnboardingForm onClose={() => setOnboarding(false)} />}
    </>
  );
}
