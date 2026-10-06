import { useState } from "react";
import { Phone, PhoneCall, Plus, X } from "lucide-react";
import { initiateOutboundCall } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { canAccessSalesAgent } from "../auth/roles";

/**
 * The outbound dial trigger for the 6DM Sales Agent.
 *
 * Renders nothing at all unless the signed-in user is a super admin — a spa
 * client has no outbound capability, and `POST /telephony/voice/outbound`
 * answers 403 for their token, so showing the button would only offer a
 * guaranteed failure.
 */
export default function LaunchOutboundCall() {
  const { role } = useAuth();
  const [open, setOpen] = useState(false);
  const [phone, setPhone] = useState("");
  const [objective, setObjective] = useState(
    "Qualify the business and book a sales presentation on Dominic's calendar."
  );
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  if (!canAccessSalesAgent(role)) return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    setStatus(null);
    try {
      const result = await initiateOutboundCall(phone, objective);
      setStatus(`Call queued (${result.call_sid}).`);
      setPhone("");
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail;
      setError(detail || "Unable to place the call.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center justify-center gap-2 rounded-xl bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 transition hover:bg-cyan-300"
      >
        <Plus size={16} /> Launch outbound call
      </button>

      {open && (
        <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/80 p-4 backdrop-blur-sm">
          <form
            onSubmit={submit}
            className="w-full max-w-lg rounded-2xl border border-slate-700 bg-[#0b1a2c] p-6 shadow-2xl"
          >
            <div className="mb-6 flex items-start justify-between">
              <div>
                <p className="mb-2 text-[10px] font-bold uppercase tracking-[.2em] text-cyan-300">
                  6DM Sales Agent
                </p>
                <h2 className="font-display text-xl font-semibold text-white">
                  Launch outbound call
                </h2>
                <p className="mt-1 text-xs text-slate-500">
                  The agent will qualify this lead and book onto Dominic's calendar.
                </p>
              </div>
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-lg p-2 text-slate-500 hover:bg-slate-800 hover:text-white"
                aria-label="Close"
              >
                <X size={17} />
              </button>
            </div>

            <label className="block">
              <span className="mb-2 block text-xs font-medium text-slate-400">
                Phone number
              </span>
              <div className="flex items-center rounded-lg border border-slate-700 bg-[#07111f] px-3">
                <Phone size={15} className="text-slate-500" />
                <input
                  required
                  value={phone}
                  onChange={(event) => setPhone(event.target.value)}
                  placeholder="+15550102040"
                  className="w-full bg-transparent px-3 py-3 text-sm text-white outline-none placeholder:text-slate-600"
                />
              </div>
            </label>

            <label className="mt-4 block">
              <span className="mb-2 block text-xs font-medium text-slate-400">
                Call objective
              </span>
              <textarea
                rows={3}
                value={objective}
                onChange={(event) => setObjective(event.target.value)}
                className="w-full resize-none rounded-lg border border-slate-700 bg-[#07111f] px-3 py-3 text-sm text-slate-200 outline-none"
              />
            </label>

            {error && (
              <p className="mt-4 rounded-lg border border-rose-400/20 bg-rose-400/5 px-3 py-2 text-xs text-rose-300">
                {error}
              </p>
            )}
            {status && (
              <p className="mt-4 rounded-lg border border-emerald-400/20 bg-emerald-400/5 px-3 py-2 text-xs text-emerald-300">
                {status}
              </p>
            )}

            <div className="mt-6 flex justify-end gap-3">
              <button
                type="button"
                onClick={() => setOpen(false)}
                className="rounded-lg px-4 py-2.5 text-xs font-semibold text-slate-400 hover:text-white"
              >
                Close
              </button>
              <button
                type="submit"
                disabled={submitting}
                className="inline-flex items-center gap-2 rounded-lg bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 hover:bg-cyan-300 disabled:opacity-60"
              >
                <PhoneCall size={14} /> {submitting ? "Dialling…" : "Start call"}
              </button>
            </div>
          </form>
        </div>
      )}
    </>
  );
}
