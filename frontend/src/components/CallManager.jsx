import React, { useState, useEffect } from 'react';
import { Phone, PhoneCall, Clock, CheckCircle2, XCircle, AlertCircle, RefreshCw } from 'lucide-react';

const API_BASE_URL =
  import.meta.env.NEXT_PUBLIC_API_URL ||
  import.meta.env.VITE_API_BASE_URL ||
  'http://127.0.0.1:8000/api/v1';

export default function CallManager() {
  const [phoneNumber, setPhoneNumber] = useState('');
  const [callObjective, setCallObjective] = useState('');
  const [isCalling, setIsCalling] = useState(false);
  const [statusMessage, setStatusMessage] = useState(null);

  const [callLogs, setCallLogs] = useState([]);
  const [loadingLogs, setLoadingLogs] = useState(false);

  // Fetch Call Logs on load
  useEffect(() => {
    fetchCallLogs();
  }, []);

  const getAuthToken = () => localStorage.getItem('access_token') || '';

  const fetchCallLogs = async () => {
    setLoadingLogs(true);
    try {
      const response = await fetch(`${API_BASE_URL}/call-logs`, {
        headers: {
          'Authorization': `Bearer ${getAuthToken()}`,
          'Content-Type': 'application/json',
        },
      });
      if (response.ok) {
        const data = await response.json();
        setCallLogs(data);
      }
    } catch (err) {
      console.error('Failed to fetch call logs:', err);
    } finally {
      setLoadingLogs(false);
    }
  };

  const handleInitiateCall = async (e) => {
    e.preventDefault();
    if (!phoneNumber) return;

    setIsCalling(true);
    setStatusMessage({ type: 'info', text: 'Initiating Twilio outbound call...' });

    try {
      const response = await fetch(`${API_BASE_URL}/telephony/voice/outbound`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${getAuthToken()}`,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          to_number: phoneNumber,
          call_objective: callObjective || 'General consultation or appointment booking',
        }),
      });

      const data = await response.json();

      if (response.ok) {
        setStatusMessage({
          type: 'success',
          text: `Call queued! SID: ${data.call_sid}`,
        });
        setPhoneNumber('');
        setCallObjective('');
        // Refresh call logs to see the new queued item
        setTimeout(fetchCallLogs, 1500);
      } else {
        throw new Error(data.detail || 'Failed to trigger outbound call');
      }
    } catch (err) {
      setStatusMessage({ type: 'error', text: err.message });
    } finally {
      setIsCalling(false);
    }
  };

  const renderStatusBadge = (status) => {
    const s = (status || '').toLowerCase();
    switch (s) {
      case 'completed':
        return <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800"><CheckCircle2 className="w-3 h-3 mr-1" /> Completed</span>;
      case 'in-progress':
      case 'answered':
      case 'ringing':
        return <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-800 animate-pulse"><PhoneCall className="w-3 h-3 mr-1" /> Active</span>;
      case 'queued':
      case 'initiated':
        return <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-yellow-100 text-yellow-800"><Clock className="w-3 h-3 mr-1" /> Queued</span>;
      default:
        return <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-red-100 text-red-800"><XCircle className="w-3 h-3 mr-1" /> {status}</span>;
    }
  };

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-8">
      {/* Header */}
      <div className="flex justify-between items-center border-b pb-4">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">AI Voice Call Center</h1>
          <p className="text-sm text-gray-500">Trigger AI outbound calls and view real-time conversation logs.</p>
        </div>
        <button
          onClick={fetchCallLogs}
          className="flex items-center space-x-1 text-sm bg-gray-100 hover:bg-gray-200 text-gray-700 px-3 py-2 rounded-md transition"
        >
          <RefreshCw className={`w-4 h-4 ${loadingLogs ? 'animate-spin' : ''}`} />
          <span>Refresh Logs</span>
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-8">
        {/* Outbound Call Trigger Panel */}
        <div className="bg-white border rounded-xl p-6 shadow-sm space-y-4">
          <h2 className="text-lg font-semibold text-gray-800 flex items-center">
            <Phone className="w-5 h-5 mr-2 text-blue-600" />
            New Outbound Call
          </h2>

          <form onSubmit={handleInitiateCall} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Target Phone Number</label>
              <input
                type="tel"
                required
                placeholder="+12149702434"
                value={phoneNumber}
                onChange={(e) => setPhoneNumber(e.target.value)}
                className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
              />
            </div>

            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Call Objective / Prompt Focus</label>
              <textarea
                rows="3"
                placeholder="Schedule a dental checkup for next Tuesday..."
                value={callObjective}
                onChange={(e) => setCallObjective(e.target.value)}
                className="w-full border rounded-lg px-3 py-2 text-sm focus:ring-2 focus:ring-blue-500 outline-none"
              />
            </div>

            <button
              type="submit"
              disabled={isCalling}
              className="w-full bg-blue-600 hover:bg-blue-700 text-white font-medium py-2.5 px-4 rounded-lg text-sm flex items-center justify-center space-x-2 transition disabled:opacity-50"
            >
              <PhoneCall className="w-4 h-4" />
              <span>{isCalling ? 'Dialing...' : 'Initiate AI Call'}</span>
            </button>
          </form>

          {statusMessage && (
            <div className={`p-3 rounded-lg text-xs flex items-start space-x-2 ${
              statusMessage.type === 'error' ? 'bg-red-50 text-red-700 border border-red-200' :
              statusMessage.type === 'success' ? 'bg-green-50 text-green-700 border border-green-200' :
              'bg-blue-50 text-blue-700 border border-blue-200'
            }`}>
              <AlertCircle className="w-4 h-4 shrink-0 mt-0.5" />
              <span>{statusMessage.text}</span>
            </div>
          )}
        </div>

        {/* Call History Table */}
        <div className="lg:col-span-2 bg-white border rounded-xl p-6 shadow-sm space-y-4">
          <h2 className="text-lg font-semibold text-gray-800">Call Logs & Transcripts</h2>

          {callLogs.length === 0 ? (
            <div className="text-center py-12 text-gray-400 text-sm">
              No recent call history found.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left border-collapse text-sm">
                <thead>
                  <tr className="border-b text-xs uppercase text-gray-400">
                    <th className="pb-3 font-medium">Recipient</th>
                    <th className="pb-3 font-medium">Direction</th>
                    <th className="pb-3 font-medium">Status</th>
                    <th className="pb-3 font-medium">Summary / Objective</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {callLogs.map((log) => (
                    <tr key={log.id} className="hover:bg-gray-50 transition">
                      <td className="py-3 font-medium text-gray-900">{log.to_number}</td>
                      <td className="py-3 text-xs uppercase text-gray-500">{log.direction}</td>
                      <td className="py-3">{renderStatusBadge(log.status)}</td>
                      <td className="py-3 text-gray-600 max-w-xs truncate">
                        {log.ai_summary || log.call_objective || 'N/A'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}