import { useEffect, useState } from "react";
import {
  CalendarCheck,
  PhoneCall,
  Smile,
  Timer,
} from "lucide-react";
import { fetchAnalytics, type AnalyticsSummary } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { PageHeader, Panel, StatCard, StateBlock } from "../components/ui/Primitives";

const formatDuration = (seconds: number | null): string => {
  if (seconds === null) return "—";
  const minutes = Math.floor(seconds / 60);
  return minutes > 0 ? `${minutes}m ${Math.round(seconds % 60)}s` : `${Math.round(seconds)}s`;
};

function Breakdown({ rows }: { rows: [string, number][] }) {
  const total = rows.reduce((sum, [, value]) => sum + value, 0) || 1;
  return (
    <div className="space-y-3">
      {rows.map(([label, value]) => (
        <div key={label}>
          <div className="mb-1 flex justify-between text-[11px]">
            <span className="text-slate-400 capitalize">{label.replace(/_/g, " ")}</span>
            <span className="font-semibold text-slate-200">{value}</span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-slate-800">
            <div
              className="h-full rounded-full bg-cyan-400/70"
              style={{ width: `${(value / total) * 100}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

/**
 * One analytics view for both products.
 *
 * It renders whatever scope the API answered in — the sales workspace for a
 * super admin, or a single spa — so a spa client physically cannot request
 * another tenant's numbers, and 6DM does not need a second page to read them.
 */
export default function AnalyticsView({
  eyebrow = "Reporting",
  title = "Analytics",
}: {
  eyebrow?: string;
  title?: string;
}) {
  const { impersonatedTenantId } = useAuth();
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    fetchAnalytics()
      .then((summary) => {
        setData(summary);
        setError(null);
      })
      .catch(() => setError("Unable to load analytics."))
      .finally(() => setLoading(false));
  }, [impersonatedTenantId]);

  return (
    <>
      <PageHeader
        eyebrow={eyebrow}
        title={title}
        subtitle={
          data
            ? `${new Date(data.window_start).toLocaleDateString()} – ${new Date(
                data.window_end
              ).toLocaleDateString()} · ${
                data.scope === "spa" ? "this spa only" : "6DM sales workspace"
              }`
            : undefined
        }
      />

      <StateBlock loading={loading} error={error}>
        {data && (
          <>
            <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              <StatCard
                label="Calls"
                value={data.calls.total}
                sub={data.scope === "spa" ? `${data.calls.inbound} inbound` : `${data.calls.inbound} in · ${data.calls.outbound} out`}
                icon={PhoneCall}
              />
              <StatCard
                label="Bookings"
                value={data.bookings.total}
                sub={`${Math.round(data.booking_conversion_rate * 100)}% of answered calls`}
                icon={CalendarCheck}
                accent="emerald"
              />
              <StatCard
                label="Average duration"
                value={formatDuration(data.average_call_duration_seconds)}
                sub={`${data.calls.completed} completed`}
                icon={Timer}
                accent="amber"
              />
              <StatCard
                label="Positive calls"
                value={data.sentiment.positive}
                sub={`${data.sentiment.unscored} awaiting analysis`}
                icon={Smile}
                accent="emerald"
              />
            </section>

            <div className="mt-6 grid gap-6 lg:grid-cols-3">
              <Panel title="Call outcomes">
                <Breakdown
                  rows={[
                    ["completed", data.calls.completed],
                    ["failed or missed", data.calls.failed],
                  ]}
                />
              </Panel>
              <Panel title="Booking status">
                <Breakdown
                  rows={[
                    ["scheduled", data.bookings.scheduled],
                    ["confirmed", data.bookings.confirmed],
                    ["completed", data.bookings.completed],
                    ["cancelled", data.bookings.cancelled],
                    ["no_show", data.bookings.no_show],
                  ]}
                />
              </Panel>
              <Panel title="Sentiment">
                <Breakdown
                  rows={[
                    ["positive", data.sentiment.positive],
                    ["neutral", data.sentiment.neutral],
                    ["negative", data.sentiment.negative],
                    ["unscored", data.sentiment.unscored],
                  ]}
                />
              </Panel>
            </div>
          </>
        )}
      </StateBlock>
    </>
  );
}
