import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { fetchCallLogs, type CallLog } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import BrowserTestCall from "../../components/BrowserTestCall";
import RecentCallsTable from "../../components/RecentCallsTable";
import { PageHeader, Panel, StateBlock } from "../../components/ui/Primitives";

/**
 * Inbound call log and transcripts for one spa.
 *
 * The rows are whatever `GET /api/v1/calls` returned, which the backend has
 * already filtered to the caller's `tenant_id`. There is no client-side filter
 * to bypass — another spa's transcripts never reach the browser.
 */
export default function SpaCalls() {
  const { impersonatedTenantId } = useAuth();
  const [calls, setCalls] = useState<CallLog[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    setLoading(true);
    fetchCallLogs({ direction: "inbound" })
      .then((rows) => {
        setCalls(rows);
        setError(null);
      })
      .catch(() => setError("Unable to load call logs."))
      .finally(() => setLoading(false));
  };

  useEffect(load, [impersonatedTenantId]);

  return (
    <>
      <PageHeader
        eyebrow="Spa Receptionist"
        title="Inbound calls"
        subtitle="Every call your AI receptionist answered, with transcript and summary."
        actions={
          <button
            onClick={load}
            className="rounded-xl border border-slate-700 p-2.5 text-slate-400 transition hover:text-white"
            aria-label="Refresh"
          >
            <RefreshCw size={15} />
          </button>
        }
      />

      <Panel title="AI Receptionist Test" subtitle="Test the browser microphone path without affecting the live phone flow.">
        <BrowserTestCall />
      </Panel>

      <Panel
        title="Call log"
        subtitle={`${calls.length} inbound call${calls.length === 1 ? "" : "s"}`}
        padded={false}
      >
        <StateBlock
          loading={loading}
          error={error}
          empty={calls.length === 0}
          emptyLabel="No inbound calls yet."
        >
          <RecentCallsTable logs={calls} />
        </StateBlock>
      </Panel>
    </>
  );
}
