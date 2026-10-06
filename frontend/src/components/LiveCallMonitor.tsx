import React from "react";
import { PhoneCall, Mic, User, Clock } from "lucide-react";
import { type CallLog, counterpartyNumber } from "../api/client";

interface LiveCallMonitorProps {
  activeCall?: CallLog;
}

export const LiveCallMonitor: React.FC<LiveCallMonitorProps> = ({ activeCall }) => {
  if (!activeCall) {
    return (
      <div className="p-6 bg-slate-900 border border-slate-800 rounded-xl text-center text-slate-400">
        <PhoneCall className="w-8 h-8 mx-auto mb-2 opacity-40 animate-pulse text-indigo-400" />
        <p className="font-medium text-slate-300">No calls currently active</p>
        <p className="text-xs text-slate-500 mt-1">Inbound calls will stream live metrics here.</p>
      </div>
    );
  }

  return (
    <div className="p-6 bg-gradient-to-r from-indigo-950/60 to-slate-900 border border-indigo-500/30 rounded-xl shadow-lg">
      <div className="flex items-center justify-between border-b border-indigo-500/20 pb-4 mb-4">
        <div className="flex items-center gap-3">
          <span className="relative flex h-3 w-3">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
          </span>
          <h3 className="font-semibold text-white tracking-wide">Live Call Active</h3>
        </div>
        <span className="px-3 py-1 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 rounded-full text-xs font-semibold uppercase">
          {activeCall.status}
        </span>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
        <div className="flex items-center gap-3 bg-slate-800/50 p-3 rounded-lg border border-slate-700/50">
          <User className="w-5 h-5 text-indigo-400" />
          <div>
            <p className="text-xs text-slate-400">Caller</p>
            <p className="font-medium text-slate-200">{activeCall.contact?.full_name || counterpartyNumber(activeCall)}</p>
          </div>
        </div>

        <div className="flex items-center gap-3 bg-slate-800/50 p-3 rounded-lg border border-slate-700/50">
          <Clock className="w-5 h-5 text-indigo-400" />
          <div>
            <p className="text-xs text-slate-400">Duration</p>
            <p className="font-medium text-slate-200">{activeCall.duration_seconds ?? 0}s</p>
          </div>
        </div>

        <div className="flex items-center gap-3 bg-slate-800/50 p-3 rounded-lg border border-slate-700/50">
          <Mic className="w-5 h-5 text-indigo-400 animate-bounce" />
          <div>
            <p className="text-xs text-slate-400">Engine</p>
            <p className="font-medium text-slate-200">Grok AI Voice Agent</p>
          </div>
        </div>
      </div>
    </div>
  );
};