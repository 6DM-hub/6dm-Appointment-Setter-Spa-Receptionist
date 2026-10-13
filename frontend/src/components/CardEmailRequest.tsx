import { useState } from "react";
import { apiClient } from "../api/client";

export default function CardEmailRequest({ appointmentId }: { appointmentId: string }) {
  const [email, setEmail] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [requestId] = useState(() => crypto.randomUUID());
  const [finished, setFinished] = useState(false);
  const [message, setMessage] = useState("");
  return <details className="mt-2 max-w-xs text-xs text-slate-300">
    <summary>Email secure card link</summary>
    <form className="mt-2 space-y-2" onSubmit={async (event) => {
      event.preventDefault();
      if (busy || finished || !consent) return;
      setBusy(true);
      try {
        const { data } = await apiClient.post(`/api/v1/appointments/${appointmentId}/card-email`, {
          confirmed_email: email, consent, request_id: requestId,
        });
        const messages: Record<string, string> = {
          accepted: "Email accepted by the mail server. Inbox delivery is not yet verified.",
          already_attempted: "This request was already attempted. Check receipt before sending again.",
          delivery_unknown: "Delivery is uncertain. Check receipt before sending again. The booking remains intact.",
          not_configured: "Card-link email is not configured for this business.",
          email_mismatch: "The address must match the customer's saved contact email. Verify their contact record first.",
          rejected: "The mail server rejected the email. The booking remains intact.",
        };
        setMessage(messages[data.status] || "Unable to email a card link for this booking. Check its customer and card status.");
        setFinished(["accepted", "already_attempted", "delivery_unknown", "rejected"].includes(data.status));
      } catch {
        setMessage("Unable to verify the send result. Retry uses the same request ID to prevent a duplicate.");
      } finally { setBusy(false); }
    }}>
      <label className="block">Customer's confirmed email
        <input type="email" required value={email} disabled={busy || finished}
          onChange={event => setEmail(event.target.value)} className="block w-full rounded bg-slate-800 p-2" />
      </label>
      <label className="block"><input type="checkbox" checked={consent} disabled={busy || finished}
        onChange={event => setConsent(event.target.checked)} /> I verified this address with the customer and they agreed to receive the secure card link.</label>
      <button type="submit" disabled={!consent || busy || finished} className="rounded bg-cyan-900 px-3 py-2 disabled:opacity-50">
        {busy ? "Sending…" : "Send card-link email"}
      </button>
      <p role="status">{message}</p>
    </form>
  </details>;
}
