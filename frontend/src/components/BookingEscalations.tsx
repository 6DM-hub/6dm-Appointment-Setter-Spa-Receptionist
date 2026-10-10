import { useEffect, useState } from "react";
import { apiClient, getApiErrorMessage } from "../api/client";
import { useAuth } from "../auth/AuthContext";
type Request = { id: string; status: string; priority: string; category: string; call_reference: string; version: number; details: Record<string, unknown>; delivery: unknown; history: { at: string; actor: string; to?: string; action?: string; note?: string }[] };
function detailText(key: string, value: unknown, details: Record<string, unknown>): string {
  if (key === "requested_start" && typeof value === "string" && typeof details.timezone === "string") {
    try { return new Date(value).toLocaleString(undefined, { timeZone: details.timezone }) + " · " + details.timezone; } catch { return value; }
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value ?? "Unknown");
}
const prefix = "/api/v1/booking-escalations";
export default function BookingEscalations() {
  const auth = useAuth();
  return <Inbox key={`${auth.user?.tenant_id ?? ""}:${auth.impersonatedTenantId ?? ""}`} />;
}
function Inbox() {
  const auth = useAuth();
  const canManage = auth.user?.role !== "spa_staff";
  const [canEdit, setCanEdit] = useState(false);
  const [staffIds, setStaffIds] = useState<string[]>([]);
  const [staffMembers, setStaffMembers] = useState<{ id: string; name: string }[]>([]);
  const [rows, setRows] = useState<Request[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [sms, setSms] = useState("");
  const [email, setEmail] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [categories, setCategories] = useState<string[]>([]);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [ids, setIds] = useState<Record<string, string>>({});
  async function run(fn: () => Promise<void>) { setBusy(true); setError(""); try { await fn(); } catch(e) { setError(getApiErrorMessage(e, "Could not update staff requests.")); } finally { setBusy(false); } }
  async function refresh() { setRows((await apiClient.get<Request[]>(prefix)).data); }
  useEffect(() => { setRows([]); setNotes({}); setIds({}); void run(async () => { await refresh(); setCanEdit((await apiClient.get(prefix + "/access")).data.can_resolve); if (!canManage) return; const s = (await apiClient.get(prefix + "/settings")).data; setSms(s.sms_destinations.join(", ")); setEmail(s.email_destinations.join(", ")); setEnabled(s.notifications_enabled); setCategories(s.categories); setStaffIds(s.staff_user_ids ?? []); setStaffMembers(s.staff_members ?? []); }); }, [auth.user?.tenant_id, auth.impersonatedTenantId]);
  return <section className="rounded-xl border border-slate-700 p-5 space-y-3">
    <h2 className="text-xl font-semibold">Needs Staff Attention</h2>
    <p>Failed requests are not confirmed appointments. Staff status is a work record; Cara requires provider success before confirming a booking to a customer.</p>
    {error && <p role="alert">{error}</p>}
    {canManage && <details><summary>Business escalation notifications</summary>
      <label className="block"><input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} /> Enable staff alerts</label>
      <label className="block">SMS recipients (E.164, comma separated)<input className="block text-black" value={sms} onChange={e => setSms(e.target.value)} /></label>
      <label className="block">Email recipients (comma separated)<input className="block text-black" value={email} onChange={e => setEmail(e.target.value)} /></label>
      <p>Email requires the server mail transport. Same-day requests have urgent priority. Empty category selection includes all errors.</p>
      {["permissions", "configuration", "unavailable", "conflict", "temporary", "unknown_outcome"].map(c => <label className="mr-3" key={c}><input type="checkbox" checked={categories.includes(c)} onChange={e => setCategories(e.target.checked ? [...categories, c] : categories.filter(x => x !== c))} />{c}</label>)}
      <button disabled={busy} onClick={() => void run(async () => { const split = (v: string) => v.split(",").map(x => x.trim()).filter(Boolean); await apiClient.put(prefix + "/settings", { sms_destinations: split(sms), email_destinations: split(email), notifications_enabled: enabled, categories, staff_user_ids: staffIds }); })}>Save notification settings</button>
      <p>Staff permitted to resolve requests:</p>{staffMembers.map(u => <label className="block" key={u.id}><input type="checkbox" checked={staffIds.includes(u.id)} onChange={e => setStaffIds(e.target.checked ? [...staffIds, u.id] : staffIds.filter(id => id !== u.id))} />{u.name}</label>)}
    </details>}
    <button disabled={busy} onClick={() => void run(refresh)}>Refresh requests</button>
    {!rows.length && <p>No failed-booking requests.</p>}
    {rows.map(r => <article className="border rounded p-3 space-y-2" key={r.id}>
      <h3>{r.priority === "urgent" ? "URGENT · " : ""}{r.status.replace(/_/g, " ")} · {r.category}</h3>
      <p>Call: {r.call_reference}</p>
      <dl>{Object.entries(r.details).map(([k, v]) => <div key={k}><dt className="font-semibold">{k.replace(/_/g, " ")}</dt><dd className="break-words">{detailText(k, v, r.details)}</dd></div>)}</dl>
      <p>Delivery: {JSON.stringify(r.delivery)}</p>
      <details><summary>Resolution history</summary>{r.history.map((h, i) => <p key={i}>{h.at} · {h.to ?? h.action} · {h.note} · Staff: {h.actor}</p>)}</details>
      {r.details.provider === "square" && <a href="https://squareup.com/dashboard/appointments/calendar" target="_blank" rel="noreferrer">Open Square Appointments for manual scheduling</a>}
      <label className="block">Resolution note<input className="text-black block" value={notes[r.id] ?? ""} onChange={e => setNotes({...notes, [r.id]: e.target.value})} /></label>
      <label className="block">Provider booking ID (required for Booked)<input className="text-black block" value={ids[r.id] ?? ""} onChange={e => setIds({...ids, [r.id]: e.target.value})} /></label>
      <div className="flex gap-3">{["pending", "contacted", "booked", "unable_to_book"].map(status => <button key={status} disabled={busy || !canEdit || status === r.status} onClick={() => void run(async () => { await apiClient.patch(`${prefix}/${r.id}`, { status, expected_version: r.version, note: notes[r.id] ?? "", provider_booking_id: ids[r.id] || null }); await refresh(); })}>{status.replace(/_/g, " ")}</button>)}</div>
      <p>Customer messages are not automatically sent when changing staff status. For an Unable to Book request, record the customer's explicit SMS follow-up consent in the note before sending. Square handles booking confirmations.</p>
      {r.status === "unable_to_book" && !["temporary", "unknown_outcome"].includes(r.category) && !r.details.provider_success_verified && <button disabled={busy || !canEdit || !(notes[r.id] ?? "").trim()} onClick={() => void run(async () => { await apiClient.post(`${prefix}/${r.id}/customer-followup`, { consent_note: notes[r.id] }); await refresh(); })}>Send consented assistance follow-up</button>}
    </article>)}
  </section>;
}
