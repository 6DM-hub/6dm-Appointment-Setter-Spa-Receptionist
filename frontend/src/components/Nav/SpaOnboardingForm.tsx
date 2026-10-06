import { useState } from "react";
import { Building2, X } from "lucide-react";
import { createSpaAccount, registerUser } from "../../api/client";

const DEFAULT_HOURS = {
  mon: [{ open: "09:00", close: "18:00" }],
  tue: [{ open: "09:00", close: "18:00" }],
  wed: [{ open: "09:00", close: "18:00" }],
  thu: [{ open: "09:00", close: "18:00" }],
  fri: [{ open: "09:00", close: "18:00" }],
};

export default function SpaOnboardingForm({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [phone, setPhone] = useState("");
  const [timezone, setTimezone] = useState("America/Los_Angeles");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const spa = await createSpaAccount({
        name,
        twilio_phone_number: phone || undefined,
        timezone,
        business_hours: DEFAULT_HOURS,
        services: [],
        staff: [],
        booking_provider: "google_calendar",
        booking_config: {},
      });
      await registerUser({
        email,
        password,
        full_name: `${name} Admin`,
        role: "spa_admin",
        tenant_id: spa.id,
      });
      onClose();
      window.location.reload();
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail;
      setError(typeof detail === "string" ? detail : "Unable to create the spa account.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-slate-950/80 px-4 backdrop-blur-sm">
      <form onSubmit={submit} className="w-full max-w-lg rounded-2xl border border-slate-700 bg-[#0b1a2c] shadow-2xl">
        <div className="flex items-start justify-between border-b border-slate-800 px-5 py-4">
          <div className="flex items-center gap-3">
            <span className="grid h-9 w-9 place-items-center rounded-lg bg-emerald-400/10 text-emerald-300">
              <Building2 size={17} />
            </span>
            <div>
              <h2 className="text-sm font-semibold text-white">Create spa account</h2>
              <p className="mt-1 text-[11px] text-slate-500">Set up inbound reception and the first spa admin.</p>
            </div>
          </div>
          <button type="button" onClick={onClose} aria-label="Close" className="text-slate-500 hover:text-white">
            <X size={18} />
          </button>
        </div>

        <div className="grid gap-4 p-5 sm:grid-cols-2">
          <label className="sm:col-span-2"><span className="field-label">Spa name</span><input required value={name} onChange={(event) => setName(event.target.value)} className="field-input" placeholder="Harbor Wellness" /></label>
          <label><span className="field-label">Twilio number</span><input value={phone} onChange={(event) => setPhone(event.target.value)} className="field-input" placeholder="+15551234567" /></label>
          <label><span className="field-label">Timezone</span><input required value={timezone} onChange={(event) => setTimezone(event.target.value)} className="field-input" placeholder="America/Los_Angeles" /></label>
          <label><span className="field-label">Admin email</span><input required type="email" value={email} onChange={(event) => setEmail(event.target.value)} className="field-input" placeholder="owner@example.com" /></label>
          <label><span className="field-label">Temporary password</span><input required minLength={8} type="password" value={password} onChange={(event) => setPassword(event.target.value)} className="field-input" placeholder="At least 8 characters" /></label>
        </div>

        {error && <p className="mx-5 mb-4 rounded-lg border border-rose-400/20 bg-rose-400/5 px-3 py-2 text-xs text-rose-300">{error}</p>}
        <div className="flex justify-end gap-2 border-t border-slate-800 px-5 py-4">
          <button type="button" onClick={onClose} className="rounded-lg px-3 py-2 text-xs font-semibold text-slate-400 hover:text-white">Cancel</button>
          <button type="submit" disabled={submitting} className="rounded-lg bg-emerald-400 px-4 py-2 text-xs font-bold text-slate-950 hover:bg-emerald-300 disabled:opacity-60">
            {submitting ? "Creating…" : "Create spa"}
          </button>
        </div>
      </form>
    </div>
  );
}