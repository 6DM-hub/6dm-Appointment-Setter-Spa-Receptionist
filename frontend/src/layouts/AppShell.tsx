import { useEffect, useState } from "react";
import { Menu, RefreshCw, ShieldCheck, X } from "lucide-react";
import { Outlet } from "react-router-dom";
import TenantSwitcher from "../components/Nav/TenantSwitcher";
import Sidebar from "../components/Nav/Sidebar";
import { useAuth } from "../auth/AuthContext";
import { canSwitchTenant, isSpaRole } from "../auth/roles";
import { fetchProviderHealth, type ProviderHealth } from "../api/client";

function StatusDot({ label, detail }: { label: string; detail: string }) {
  const connected = detail === "Connected" || detail === "Active";
  return (
    <div className="flex items-center gap-2 border-r border-slate-800 pr-4 last:border-0 last:pr-0">
      <span className="relative flex h-2 w-2">
        <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${connected ? "bg-emerald-400" : "bg-rose-400"} opacity-60`} />
        <span className={`relative inline-flex h-2 w-2 rounded-full ${connected ? "bg-emerald-400" : "bg-rose-400"}`} />
      </span>
      <span className="text-[11px] font-medium text-slate-300">{label}</span>
      <span className={`text-[10px] ${connected ? "text-emerald-400" : "text-rose-400"}`}>{detail}</span>
    </div>
  );
}

export default function AppShell() {
  const { role, impersonatedTenantId } = useAuth();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [providerHealth, setProviderHealth] = useState<ProviderHealth | null>(null);
  const [refreshingProviders, setRefreshingProviders] = useState(false);

  const refreshProviderStatus = async () => {
    if (refreshingProviders) return;
    setRefreshingProviders(true);
    try {
      setProviderHealth(await fetchProviderHealth());
    } catch {
      setProviderHealth({ twilio: "disconnected", grok: "disconnected" });
    } finally {
      setRefreshingProviders(false);
    }
  };

  useEffect(() => {
    void refreshProviderStatus();
  }, []);

  return (
    <div className="min-h-screen bg-[#07111f] text-slate-100 selection:bg-cyan-400/30">
      <aside className="fixed inset-y-0 left-0 z-20 hidden lg:block">
        <Sidebar />
      </aside>

      {mobileNavOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-slate-950/80 backdrop-blur-sm"
            onClick={() => setMobileNavOpen(false)}
          />
          <div className="absolute inset-y-0 left-0">
            <Sidebar onNavigate={() => setMobileNavOpen(false)} />
          </div>
        </div>
      )}

      <main className="lg:pl-64">
        <header className="border-b border-slate-800/80 bg-[#091525]/90 px-5 py-4 backdrop-blur md:px-8">
          <div className="mx-auto flex max-w-[1500px] flex-col gap-4 xl:flex-row xl:items-center xl:justify-between">
            <div className="flex items-center gap-3">
              <button
                onClick={() => setMobileNavOpen((open) => !open)}
                className="text-slate-400 lg:hidden"
                aria-label="Toggle navigation"
              >
                {mobileNavOpen ? <X size={20} /> : <Menu size={20} />}
              </button>
              <div>
                <h1 className="font-display text-lg font-semibold tracking-tight text-white">
                  Voice command center
                </h1>
                <p className="mt-0.5 text-xs text-slate-500">
                  {isSpaRole(role)
                    ? "Your spa's inbound reception, calls and settings."
                    : "Outbound sales and every spa tenant."}
                </p>
              </div>
            </div>

            <div className="flex flex-col gap-3 md:flex-row md:items-center">
              <div className="hidden items-center gap-4 xl:flex">
                <StatusDot
                  label="Twilio Webhook"
                  detail={providerHealth?.twilio === "connected" ? "Connected" : providerHealth?.twilio === "not_configured" ? "Not configured" : "Disconnected"}
                />
                <StatusDot
                  label="Grok Engine"
                  detail={providerHealth?.grok === "connected" ? "Active" : providerHealth?.grok === "not_configured" ? "Not configured" : "Disconnected"}
                />
                <button
                  type="button"
                  onClick={() => void refreshProviderStatus()}
                  disabled={refreshingProviders}
                  aria-label="Refresh provider status"
                  title="Refresh status"
                  className="inline-flex h-7 w-7 items-center justify-center rounded-md border border-slate-700 text-slate-400 transition hover:border-cyan-400/50 hover:text-cyan-300 disabled:cursor-wait disabled:opacity-60"
                >
                  <RefreshCw size={14} className={refreshingProviders ? "animate-spin" : ""} />
                </button>
              </div>
              <TenantSwitcher />
            </div>
          </div>

          {/* A super admin looking at a client's data should never be able to
              forget it — a spa user never sees this banner at all. */}
          {canSwitchTenant(role) && impersonatedTenantId && (
            <div className="mx-auto mt-3 flex max-w-[1500px] items-center gap-2 rounded-xl border border-emerald-400/20 bg-emerald-400/[.06] px-3 py-2 text-[11px] text-emerald-200">
              <ShieldCheck size={14} />
              Inspecting a client spa. Sales tools are hidden from this tenant's
              own users.
            </div>
          )}
        </header>

        <div className="mx-auto max-w-[1500px] space-y-6 px-5 py-6 md:px-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
