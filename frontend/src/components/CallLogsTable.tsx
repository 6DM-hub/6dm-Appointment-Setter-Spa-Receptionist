import React, { useState } from "react";
import { FileText, PhoneIncoming, Clock, X } from "lucide-react";
import { type CallLog, counterpartyNumber } from "../api/client";

interface CallLogsTableProps {
  logs: CallLog[];
}

export const CallLogsTable: React.FC<CallLogsTableProps> = ({ logs }) => {
  const [selectedLog, setSelectedLog] = useState<CallLog | null>(null);

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
      <div className="p-4 border-b border-slate-800 flex justify-between items-center">
        <h2 className="text-lg font-semibold text-white">Call History</h2>
        <span className="text-xs text-slate-400">{logs.length} calls logged</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm text-slate-300">
          <thead className="bg-slate-950 text-slate-400 uppercase text-xs">
            <tr>
              <th className="p-4">Contact / Phone</th>
              <th className="p-4">Status</th>
              <th className="p-4">Duration</th>
              <th className="p-4">Date</th>
              <th className="p-4 text-right">Transcript</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {logs.length === 0 ? (
              <tr>
                <td colSpan={5} className="p-6 text-center text-slate-500">
                  No call logs available.
                </td>
              </tr>
            ) : (
              logs.map((log) => (
                <tr key={log.id} className="hover:bg-slate-800/50 transition-colors">
                  <td className="p-4 font-medium text-slate-200 flex items-center gap-2">
                    <PhoneIncoming className="w-4 h-4 text-indigo-400" />
                    {log.contact?.full_name || counterpartyNumber(log)}
                  </td>
                  <td className="p-4">
                    <span className="px-2 py-0.5 rounded-full text-xs font-semibold bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                      {log.status}
                    </span>
                  </td>
                  <td className="p-4 text-slate-400 flex items-center gap-1">
                    <Clock className="w-3.5 h-3.5" />
                    {log.duration_seconds ?? 0}s
                  </td>
                  <td className="p-4 text-slate-400">
                    {new Date(log.created_at).toLocaleString()}
                  </td>
                  <td className="p-4 text-right">
                    <button
                      onClick={() => setSelectedLog(log)}
                      className="px-3 py-1.5 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg text-xs font-medium inline-flex items-center gap-1 transition-colors"
                    >
                      <FileText className="w-3.5 h-3.5" /> View Transcript
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Transcript Drawer / Modal */}
      {selectedLog && (
        <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex justify-end z-50">
          <div className="w-full max-w-lg bg-slate-900 border-l border-slate-800 p-6 h-full overflow-y-auto flex flex-col justify-between">
            <div>
              <div className="flex justify-between items-center mb-6">
                <h3 className="text-lg font-bold text-white">Call Details & Transcript</h3>
                <button
                  onClick={() => setSelectedLog(null)}
                  className="p-1 text-slate-400 hover:text-white rounded-lg hover:bg-slate-800"
                >
                  <X className="w-5 h-5" />
                </button>
              </div>

              <div className="space-y-4 mb-6">
                <div>
                  <label className="text-xs text-slate-500 uppercase font-bold">Caller</label>
                  <p className="text-slate-200 font-medium">
                    {selectedLog.contact?.full_name || "Unknown"} ({counterpartyNumber(selectedLog)})
                  </p>
                </div>
                {selectedLog.ai_summary && (
                  <div>
                    <label className="text-xs text-slate-500 uppercase font-bold">AI Summary</label>
                    <p className="text-sm text-indigo-200 bg-indigo-950/40 border border-indigo-800/40 p-3 rounded-lg">
                      {selectedLog.ai_summary}
                    </p>
                  </div>
                )}
                <div>
                  <label className="text-xs text-slate-500 uppercase font-bold mb-2 block">
                    Full Transcript
                  </label>
                  <pre className="p-4 bg-slate-950 border border-slate-800 rounded-lg text-xs text-slate-300 font-mono whitespace-pre-wrap leading-relaxed max-h-96 overflow-y-auto">
                    {selectedLog.transcript || "No transcript available for this call."}
                  </pre>
                </div>
              </div>
            </div>

            <button
              onClick={() => setSelectedLog(null)}
              className="w-full py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 rounded-lg text-sm font-medium"
            >
              Close
            </button>
          </div>
        </div>
      )}
    </div>
  );
};