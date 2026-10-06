import { useEffect, useMemo, useState } from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  CalendarDays,
  Clock3,
  ShieldCheck,
  Star,
  Target,
  RefreshCw,
} from "lucide-react";
import {
  fetchAnalytics,
  fetchCallLogs,
  type AnalyticsSummary,
  type CallLog,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { canAccessSalesAgent } from "../auth/roles";
import LaunchOutboundCall from "../components/LaunchOutboundCall";
import RecentCallsTable from "../components/RecentCallsTable";
import { PageHeader, Panel, StatCard, StateBlock } from "../components/ui/Primitives";

const formatDuration = (seconds: number | null): string => {
  if (seconds === null) return "—";
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${Math.round(seconds % 60)}s` : `${Math.round(seconds)}s`;
};

const percent = (value: number): string => `${Math.round(value * 100)}%`;

export default function CommandCenter() {
  const { role, user, impersonatedTenantId } = useAuth();
  const [analytics, setAnalytics] = useState<AnalyticsSummary | null>(null);
  const [calls, setCalls] = useState<CallLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  // The lens follows the scope the API is already answering in: a spa user has
  // only their own spa, and a super admin is looking at the sales workspace
  // unless the tenant switcher says otherwise. There is no separate toggle to
  // fall out of sync with the data.
  const showingSalesWorkspace =
    canAccessSalesAgent(role) && !impersonatedTenantId;

  const loadDashboard = async () => {
    setLoading(true);
    setRefreshing(true);
    const callQuery = showingSalesWorkspace ? undefined : { direction: "inbound" as const };
    try {
      const [summary, logs] = await Promise.all([fetchAnalytics(), fetchCallLogs(callQuery)]);
        setAnalytics(summary);
        setCalls(logs);
        setError(null);
    } catch {
      setError("Unable to refresh workspace data.");
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  };

  useEffect(() => {
    void loadDashboard();
  }, [impersonatedTenantId, showingSalesWorkspace]);

  const kpis = useMemo(() => {
    if (!analytics) return [];
    if (showingSalesWorkspace) {
      return [
        {
          label: "Calls placed",
          value: analytics.calls.outbound,
          sub: `${analytics.calls.completed} connected`,
          icon: ArrowUpRight,
          accent: "cyan" as const,
        },
        {
          label: "Presentations booked",
          value: analytics.bookings.total,
          sub: `${percent(analytics.booking_conversion_rate)} of answered calls`,
          icon: CalendarDays,
          accent: "emerald" as const,
        },
        {
          label: "Leads in pipeline",
          value: analytics.contacts_total,
          sub: "B2B prospects on file",
          icon: Target,
          accent: "amber" as const,
        },
        {
          label: "Positive sentiment",
          value: analytics.sentiment.positive,
          sub: `${analytics.sentiment.negative} negative`,
          icon: Star,
          accent: "cyan" as const,
        },
      ];
    }
    return [
      {
        label: "Inbound calls handled",
        value: analytics.calls.inbound,
        sub: `${analytics.calls.failed} missed or failed`,
        icon: ArrowDownLeft,
        accent: "emerald" as const,
      },
      {
        label: "Services booked",
        value: analytics.bookings.total,
        sub: `${analytics.bookings.cancelled} cancelled`,
        icon: CalendarDays,
        accent: "cyan" as const,
      },
      {
        label: "Average call duration",
        value: formatDuration(analytics.average_call_duration_seconds),
        sub: "Across answered calls",
        icon: Clock3,
        accent: "amber" as const,
      },
      {
        label: "Positive sentiment",
        value: analytics.sentiment.positive,
        sub: `${analytics.sentiment.unscored} not yet scored`,
        icon: Star,
        accent: "emerald" as const,
      },
    ];
  }, [analytics, showingSalesWorkspace]);

  return (
    <>
      <PageHeader
        eyebrow="Workspace lens"
        title={
          user?.full_name
            ? `Welcome back, ${user.full_name.split(" ")[0]}.`
            : "Welcome back."
        }
        subtitle={
          showingSalesWorkspace
            ? "Your outbound B2B pipeline overview is ready for review."
            : "Your spa's inbound reception overview is ready for review."
        }
        actions={
          <div className="flex items-center gap-2">
            {showingSalesWorkspace && <LaunchOutboundCall />}
            <button
              type="button"
              onClick={() => void loadDashboard()}
              disabled={refreshing}
              aria-label="Refresh dashboard"
              title="Refresh dashboard"
              className="inline-flex items-center gap-2 rounded-xl border border-slate-700 px-3 py-2.5 text-xs font-semibold text-slate-300 transition hover:border-cyan-400/50 hover:text-cyan-300 disabled:cursor-wait disabled:opacity-60"
            >
              <RefreshCw size={15} className={refreshing ? "animate-spin" : ""} />
              {refreshing ? "Refreshing" : "Refresh"}
            </button>
          </div>
        }
      />

      <StateBlock loading={loading && !analytics} error={error}>
        <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {kpis.map((kpi) => (
            <StatCard key={kpi.label} {...kpi} />
          ))}
        </section>
      </StateBlock>

      <Panel
        title="Recent calls"
        subtitle={
          showingSalesWorkspace
            ? "Outbound conversations from the 6DM Sales Agent."
            : "Inbound conversations your AI receptionist answered."
        }
        padded={false}
      >
        <StateBlock
          loading={loading}
          error={error}
          empty={calls.length === 0}
          emptyLabel="No calls recorded in this workspace yet."
        >
          <RecentCallsTable logs={calls} />
        </StateBlock>
      </Panel>

      <div className="rounded-2xl border border-cyan-400/15 bg-cyan-400/[.04] p-5">
        <div className="flex gap-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-cyan-400/10 text-cyan-300">
            <ShieldCheck size={17} />
          </span>
          <div>
            <p className="text-xs font-semibold text-cyan-200">
              Conversation guardrails on
            </p>
            <p className="mt-1 text-[11px] leading-relaxed text-slate-500">
              Short spoken responses, calendar confirmation checks, and booking
              limited to configured opening hours are active.
            </p>
          </div>
        </div>
      </div>
    </>
  );
}
