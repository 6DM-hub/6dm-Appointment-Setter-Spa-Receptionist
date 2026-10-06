import { useEffect, useState } from "react";
import { PhoneOutgoing } from "lucide-react";
import { fetchCallLogs, fetchLeads, type CallLog, type Lead } from "../../api/client";
import LaunchOutboundCall from "../../components/LaunchOutboundCall";
import RecentCallsTable from "../../components/RecentCallsTable";
import { PageHeader, Panel, StateBlock } from "../../components/ui/Primitives";

/** Dial queue + outbound call history for the 6DM Sales Agent. */
export default function OutboundCampaigns() {
  const [queue, setQueue] = useState<Lead[]>([]);
  const [calls, setCalls] = useState<CallLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      // Leads with no presentation on the books are the ones worth dialling.
      fetchLeads({ booked: false }),
      fetchCallLogs({ direction: "outbound" }),
    ])
      .then(([leadPage, logs]) => {
        setQueue(leadPage.items);
        setCalls(logs);
        setError(null);
      })
      .catch(() => setError("Unable to load outbound campaign data."))
      .finally(() => setLoading(false));
  }, []);

  return (
    <>
      <PageHeader
        eyebrow="6DM Sales Agent"
        title="Outbound campaigns"
        subtitle="Prospects not yet booked, and every call the agent has placed."
        actions={<LaunchOutboundCall />}
      />

      <div className="grid gap-6 xl:grid-cols-[minmax(330px,.8fr)_minmax(0,1.6fr)]">
        <Panel
          title="Dial queue"
          subtitle={`${queue.length} lead${queue.length === 1 ? "" : "s"} with no presentation booked`}
          padded={false}
        >
          <StateBlock
            loading={loading}
            error={error}
            empty={queue.length === 0}
            emptyLabel="Every lead has a presentation booked."
          >
            <div className="divide-y divide-slate-800/80">
              {queue.map((lead) => (
                <div key={lead.id} className="flex items-center gap-3 px-5 py-3">
                  <span className="grid h-8 w-8 place-items-center rounded-full bg-slate-800 text-[10px] font-bold text-slate-300">
                    {lead.full_name
                      .split(" ")
                      .map((part) => part[0])
                      .join("")
                      .slice(0, 2)}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-xs font-medium text-slate-200">
                      {lead.full_name}
                    </p>
                    <p className="truncate text-[10px] text-slate-500">
                      {lead.phone_number} · {lead.call_count} call
                      {lead.call_count === 1 ? "" : "s"}
                    </p>
                  </div>
                  <PhoneOutgoing size={14} className="text-slate-600" />
                </div>
              ))}
            </div>
          </StateBlock>
        </Panel>

        <Panel title="Outbound call history" padded={false}>
          <StateBlock
            loading={loading}
            error={error}
            empty={calls.length === 0}
            emptyLabel="No outbound calls placed yet."
          >
            <RecentCallsTable logs={calls} />
          </StateBlock>
        </Panel>
      </div>
    </>
  );
}
