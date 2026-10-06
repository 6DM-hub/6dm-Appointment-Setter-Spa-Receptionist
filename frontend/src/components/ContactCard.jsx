import React, { useState } from 'react';
import { Phone, Loader2, CheckCircle2, AlertCircle } from 'lucide-react';
import { initiateCall } from '../services/api';

export default function ContactCard({ contact }) {
  const [loading, setLoading] = useState(false);
  const [callStatus, setCallStatus] = useState(null); // 'success' | 'error' | null
  const [errorMessage, setErrorMessage] = useState('');

  const handleCall = async () => {
    setLoading(true);
    setCallStatus(null);
    setErrorMessage('');

    try {
      const data = await initiateCall(contact.phone_number);
      setCallStatus('success');
      console.log('Call initiated with SID:', data.call_sid);
    } catch (err) {
      setCallStatus('error');
      setErrorMessage(err?.response?.data?.detail || err.message || 'Call failed to trigger');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="border rounded-lg p-4 shadow-sm flex items-center justify-between bg-white">
      <div>
        <h3 className="text-lg font-semibold text-gray-800">{contact.full_name}</h3>
        <p className="text-sm text-gray-500">{contact.phone_number}</p>
      </div>

      <div className="flex items-center gap-2">
        {callStatus === 'success' && (
          <span className="text-xs text-green-600 flex items-center gap-1 font-medium">
            <CheckCircle2 size={16} /> Calling...
          </span>
        )}

        {callStatus === 'error' && (
          <span className="text-xs text-red-600 flex items-center gap-1 font-medium" title={errorMessage}>
            <AlertCircle size={16} /> Failed
          </span>
        )}

        <button
          onClick={handleCall}
          disabled={loading}
          className="flex items-center gap-2 bg-indigo-600 hover:bg-indigo-700 disabled:bg-indigo-300 text-white px-4 py-2 rounded-md font-medium text-sm transition-colors"
        >
          {loading ? (
            <>
              <Loader2 className="animate-spin" size={16} />
              Dialing...
            </>
          ) : (
            <>
              <Phone size={16} />
              Call Contact
            </>
          )}
        </button>
      </div>
    </div>
  );
}