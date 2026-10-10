import { useState } from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  ChevronRight,
  CircleCheck,
  X,
} from "lucide-react";
import { counterpartyNumber, type CallLog } from "../api/client";

const STATUS_STYLES: Record<string, string> = {
  completed: "bg-emerald-400/10 text-emerald-300",
  in_progress: "bg-cyan-400/10 text-cyan-300",
  ringing: "bg-cyan-400/10 text-cyan-300",
  queued: "bg-slate-700/40 text-slate-300",
  failed: "bg-rose-400/10 text-rose-300",
  busy: "bg-rose-400/10 text-rose-300",
  no_answer: "bg-amber-400/10 text-amber-300",
  cancelled: "bg-slate-700/40 text-slate-400",
};

const formatDuration = (seconds: number | null): string => {
  if (!seconds) return "—";
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(Math.round(seconds % 60)).padStart(2, "0")}`;
};

const sentimentOf = (log: CallLog): string | null => {
  const value = log.ai_analysis?.["sentiment"];
  return typeof value === "string" ? value : null;
};

const bookingOutcomeOf = (log: CallLog): string | null => {
  const value = log.ai_analysis?.["booking_outcome"];
  return typeof value === "string" ? value.replace(/_/g, " ") : null;
};

const callerLabelOf = (log: CallLog): string =>
  log.contact?.full_name && log.contact.full_name !== "Unknown"
    ? log.contact.full_name
    : "Unknown caller";

/** Call history with a transcript drawer. Rows are whatever the API returned,
 *  which is already filtered to the caller's tenant. */
export default function RecentCallsTable({ logs }: { logs: CallLog[] }) {
  const [selected, setSelected] = useState<CallLog | null>(null);

  return (
    <>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-left">
          <thead className="bg-[#091525] text-[10px] uppercase tracking-wider text-slate-500">
            <tr>
              <th className="px-5 py-3 font-semibold">Caller</th>
              <th className="px-4 py-3 font-semibold">Direction</th>
              <th className="px-4 py-3 font-semibold">Duration</th>
              <th className="px-4 py-3 font-semibold">Status</th>
              <th className="px-4 py-3 font-semibold">Sentiment</th>
              <th className="px-4 py-3 font-semibold">Language</th>
              <th className="px-4 py-3 font-semibold">Booking</th>
              <th className="px-4 py-3 font-semibold">When</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800/80">
            {logs.map((log) => (
              <tr
                key={log.id}
                onClick={() => setSelected(log)}
                className="cursor-pointer transition hover:bg-slate-800/40"
              >
                <td className="px-5 py-4">
                  <p className="text-xs font-semibold text-slate-200">
                    {callerLabelOf(log)}
                  </p>
                  <p className="mt-0.5 text-[10px] text-slate-500">
                    {counterpartyNumber(log)}
                  </p>
                </td>
                <td className="px-4 py-4">
                  <span
                    className={`inline-flex items-center gap-1.5 text-[11px] ${
                      log.direction === "outbound" ? "text-cyan-300" : "text-emerald-300"
                    }`}
                  >
                    {log.direction === "outbound" ? (
                      <ArrowUpRight size={13} />
                    ) : (
                      <ArrowDownLeft size={13} />
                    )}
                    {log.direction}
                  </span>
                </td>
                <td className="px-4 py-4 text-xs text-slate-400">
                  {formatDuration(log.duration_seconds)}
                </td>
                <td className="px-4 py-4">
                  <span
                    className={`rounded-md px-2 py-1 text-[10px] font-semibold ${
                      STATUS_STYLES[log.status] || "bg-slate-700/40 text-slate-300"
                    }`}
                  >
                    {log.status.replace(/_/g, " ")}
                  </span>
                </td>
                <td className="px-4 py-4 text-xs text-slate-400">
                  {sentimentOf(log) || "—"}
                </td>
                <td className="px-4 py-4 text-xs text-slate-400">
                  {log.direction === "inbound" ? log.primary_language || "—" : "—"}
                </td>
                <td className="px-4 py-4 text-xs capitalize text-slate-400">
                  {bookingOutcomeOf(log) || "—"}
                </td>
                <td className="px-4 py-4 text-xs text-slate-500">
                  {new Date(log.created_at).toLocaleString()}
                </td>
                <td className="px-4 py-4 text-right">
                  <ChevronRight size={16} className="text-slate-600" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <div className="fixed inset-0 z-40 bg-slate-950/70 backdrop-blur-sm">
          <div className="absolute inset-y-0 right-0 w-full max-w-xl overflow-y-auto border-l border-slate-700 bg-[#0b1a2c] shadow-2xl">
            <div className="sticky top-0 z-10 flex items-start justify-between border-b border-slate-800 bg-[#0b1a2c]/95 px-6 py-5 backdrop-blur">
              <div>
                <p className="mb-2 text-[10px] font-bold uppercase tracking-[.2em] text-cyan-300">
                  Conversation detail
                </p>
                <h2 className="font-display text-xl font-semibold text-white">
                  {callerLabelOf(selected)}
                </h2>
                <p className="mt-1 text-xs text-slate-500">
                  {counterpartyNumber(selected)} ·{" "}
                  {new Date(selected.created_at).toLocaleString()}
                </p>
                {selected.direction === "inbound" && selected.primary_language && (
                  <p className="mt-2 text-xs text-cyan-300">
                    Primary language: {selected.primary_language}
                  </p>
                )}
              </div>
              <button
                onClick={() => setSelected(null)}
                className="rounded-lg p-2 text-slate-500 hover:bg-slate-800 hover:text-white"
                aria-label="Close"
              >
                <X size={17} />
              </button>
            </div>

            <div className="space-y-5 p-6">
                {selected.ai_analysis.sensitive_health_request === true && <p className="rounded-lg border border-amber-400/30 bg-amber-400/10 p-3 text-xs text-amber-200">Sensitive-health request flagged for human review. Medical-office workflows are disabled pending verification.</p>}
              {selected.ai_summary && (
                <section className="rounded-xl border border-slate-800 bg-[#07111f] p-4">
                  <h3 className="mb-2 text-[10px] font-bold uppercase tracking-wider text-slate-500">
                    AI summary
                  </h3>
                  <p className="text-xs leading-relaxed text-slate-300">
                    {selected.ai_summary}
                  </p>
                </section>
              )}

              <section className="grid gap-3 rounded-xl border border-slate-800 bg-[#07111f] p-4 sm:grid-cols-2">
                <div>
                  <h3 className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Status</h3>
                  <p className="mt-1 text-xs capitalize text-slate-300">{selected.status.replace(/_/g, " ")}</p>
                </div>
                <div>
                  <h3 className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Duration</h3>
                  <p className="mt-1 text-xs text-slate-300">{formatDuration(selected.duration_seconds)}</p>
                </div>
                <div>
                  <h3 className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Booking result</h3>
                  <p className="mt-1 text-xs capitalize text-slate-300">{bookingOutcomeOf(selected) || "No booking"}</p>
                </div>
                <div>
                  <h3 className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Linked contact</h3>
                  <p className="mt-1 text-xs text-slate-300">{selected.contact?.full_name || "None"}</p>
                </div>
                {selected.direction === "inbound" && (
                  <div>
                    <h3 className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Primary language</h3>
                    <p className="mt-1 text-xs text-slate-300">{selected.primary_language || "Not detected"}</p>
                  </div>
                )}
                {typeof selected.ai_analysis?.["linked_appointment_id"] === "string" && (
                  <div>
                    <h3 className="text-[10px] font-bold uppercase tracking-wider text-slate-500">Linked appointment</h3>
                    <p className="mt-1 break-all text-xs text-slate-300">{selected.ai_analysis["linked_appointment_id"] as string}</p>
                  </div>
                )}
              </section>

              {selected.recording_url && (
                <section className="rounded-xl border border-slate-800 bg-[#07111f] p-4">
                  <h3 className="mb-3 text-[10px] font-bold uppercase tracking-wider text-slate-500">
                    Recording
                  </h3>
                  <audio controls src={selected.recording_url} className="w-full" />
                </section>
              )}

              <section>
                <h3 className="mb-3 text-[10px] font-bold uppercase tracking-wider text-slate-500">
                  Transcript
                </h3>
                <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap rounded-xl border border-slate-800 bg-[#07111f] p-4 font-mono text-[11px] leading-relaxed text-slate-300">
                  {selected.transcript || "No transcript captured for this call."}
                </pre>
              </section>

              {selected.status === "completed" && (
                <p className="flex items-center gap-2 rounded-lg border border-emerald-400/20 bg-emerald-400/5 px-3 py-2 text-[11px] text-emerald-300">
                  <CircleCheck size={14} /> Call completed and analysed.
                </p>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
