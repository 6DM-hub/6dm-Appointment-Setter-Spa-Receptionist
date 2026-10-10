import { useEffect, useState } from "react";
import { apiClient, getApiErrorMessage } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";

type Offer = { package_name: string; name: string; duration_minutes: number; price_minor: number; price_source: string; discount_pct: number };
type Recipient = { contact_id: string; customer: string; reason: string; offer: Offer; message: string; channels: string[] };
type Campaign = { id: string; status: string; proposal_hash: string; proposal: { audience: Recipient[]; test_mode: boolean; schedule: string; timezone: string; limitations: unknown } };
type Delivery = { id: string; status: string; message: string; response: string | null; booking: { external_booking_id: string; simulated: boolean } | null; staged_booking?: { requested_local_start?: string; fingerprint: string; service: string; customer_name: string; preferred_staff: string | null; slot: { start: string; duration_minutes: number; team_member_id: string } | null } | null };
type Results = { messages_sent_test: number; responses: number; appointments_booked_test: number; deliveries: Delivery[]; audit: { action: string; at: string }[] };
type Preferences = { discount_limit_pct: number; services_to_promote: string[]; appointment_priority: string; discount_excluded_weekdays?: string[]; promotion_until?: string | null };
type Interpretation = { kind: string; message: string; preferences?: Preferences; facts?: unknown; options?: string[]; blocked?: boolean };
type Insight = { contact_id: string; customer: string; source: string; completed_visits: number; days_since_visit: number | null; average_days_between_visits: number | null; confirmed_preferences: unknown; engagement: { call_count: number } | null; has_upcoming_appointment: boolean; marketing_sms_allowed: boolean; favorite_services: { name: string; duration_minutes: number }[] };
type Speech = { lang: string; onresult: ((event: { results: ArrayLike<ArrayLike<{ transcript: string }>> }) => void) | null; onerror: (() => void) | null; onend: (() => void) | null; start: () => void; stop: () => void };
const prefix = "/api/v1/cara";
const button = "rounded-lg bg-indigo-600 px-4 py-2 text-white disabled:opacity-40";
const panel = "rounded-xl border border-slate-700 bg-[#0b1629] p-5 space-y-3 text-slate-200";
const input = "w-full rounded-lg border border-slate-300 p-2 text-slate-900";

export default function AskCara() {
  const auth = useAuth();
  const [request, setRequest] = useState("Prepare a 90-day reactivation campaign");
  const [testProposal, setTestProposal] = useState(true);
  const [interpretation, setInterpretation] = useState<Interpretation | null>(null);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [selected, setSelected] = useState<Campaign | null>(null);
  const [results, setResults] = useState<Results | null>(null);
  const [insights, setInsights] = useState<Insight[]>([]);
  const [prefs, setPrefs] = useState<Preferences>({ discount_limit_pct: 0, services_to_promote: [], appointment_priority: "earliest_available" });
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const [messages, setMessages] = useState<Record<string, string>>({});
  const [start, setStart] = useState("");
  const [schedule, setSchedule] = useState("");
  const [reply, setReply] = useState<Record<string, string>>({});
  const [customerNames, setCustomerNames] = useState<Record<string, string>>({});
  const [preferredStaff, setPreferredStaff] = useState("");
  const [masterUsers, setMasterUsers] = useState<{ id: string; name: string | null; is_active: boolean; is_business_master: boolean }[]>([]);
  const [integration, setIntegration] = useState<{ selected_booking_system: string; configuration_status: string; bookings_authority: string | null; customer_directory_authority: string; secure_card_supported: boolean; balances_authority: string | null; memberships_authority: string | null; payment_compatibility_note: string } | null>(null);
  const platform = auth.user?.role === "super_admin";
  const master = auth.user?.role === "spa_admin" && auth.user?.is_business_master === true;
  const dirty = selected ? selected.proposal.schedule !== schedule || selected.proposal.audience.some(p => (messages[p.contact_id] ?? p.message) !== p.message) : false;

  async function refresh() {
    const [c, i, p] = await Promise.all([apiClient.get<Campaign[]>(prefix + "/campaigns"), apiClient.get<{ customers: Insight[] }>(prefix + "/insights"), apiClient.get<Preferences>(prefix + "/preferences")]);
    setCampaigns(c.data); setInsights(i.data.customers); setPrefs(p.data);
    setMasterUsers(platform ? (await apiClient.get(prefix + "/master-users")).data : []);
    setIntegration((await apiClient.get(prefix + "/integration-status")).data);
  }
  async function run(work: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await work(); } catch (e) { setError(getApiErrorMessage(e, "Cara could not complete this step.")); } finally { setBusy(false); }
  }
  useEffect(() => {
    setSelected(null); setResults(null); setMessages({}); setInsights([]); setCampaigns([]);
    setInterpretation(null);
    setIntegration(null); setCustomerNames({}); setPreferredStaff("");
    void run(refresh);
  }, [auth.user?.tenant_id, auth.impersonatedTenantId]);
  async function open(c: Campaign) {
    setSelected(c); setMessages(Object.fromEntries(c.proposal.audience.map(p => [p.contact_id, p.message])));
    setSchedule(c.proposal.schedule);
    setResults((await apiClient.get<Results>(`${prefix}/campaigns/${c.id}/results`)).data);
  }
  async function action(suffix: string, body?: unknown) {
    if (!selected) return;
    const c = (await apiClient.post<Campaign>(`${prefix}/campaigns/${selected.id}/${suffix}`, body)).data;
    await refresh(); await open(c);
  }
  function speak() {
    const Constructor = (window as unknown as { SpeechRecognition?: new () => Speech; webkitSpeechRecognition?: new () => Speech }).SpeechRecognition
      || (window as unknown as { webkitSpeechRecognition?: new () => Speech }).webkitSpeechRecognition;
    if (!Constructor) { setNotice("Voice input is unavailable in this browser. Type your request below."); return; }
    const recognition = new Constructor(); recognition.lang = "en-US";
    recognition.onresult = e => { setRequest(e.results[0][0].transcript); setListening(false); };
    recognition.onerror = () => { setListening(false); setError("Voice input failed. You can type your request."); };
    recognition.onend = () => setListening(false);
    recognition.start(); setListening(true);
  }
  return <div className="space-y-6 p-6 max-w-6xl mx-auto">
    <div><h1 className="text-3xl font-semibold">Ask Cara</h1><p className="text-slate-400">Owner assistant · 90-day reactivation</p></div>
    <div className="rounded-xl bg-amber-50 border border-amber-300 p-4 text-amber-950"><strong>Development: test outreach and simulated bookings only.</strong> No SMS is sent and no real appointment is created here. Phone bookings continue through the existing Square integration and Ara voice.</div>
    {integration && <section className={panel}><h2 className="text-xl font-semibold">Integration authority and readiness</h2><p>Selected system: {integration.selected_booking_system} · {integration.configuration_status}. This screen checks configuration; it does not prove a live connection.</p><p>External booking authority: {integration.bookings_authority ?? "Unavailable"}. Customer directory: {integration.customer_directory_authority}. Cara stores local dashboard records.</p><p>Gift-card balances: {integration.balances_authority ?? "Unavailable"}. Memberships: {integration.memberships_authority ?? "Unavailable"}. Secure saved-card capability: {integration.secure_card_supported ? "Implemented; setup still requires verification" : "Unavailable"}.</p><p className="text-sm">{integration.payment_compatibility_note}</p></section>}
    {error && <div role="alert" className="rounded-lg bg-red-50 p-4 text-red-800">{error}</div>}
    {notice && <div role="status" className="rounded-lg bg-blue-50 p-4 text-slate-900">{notice}</div>}
    <section className={panel}><h2 className="text-xl font-semibold">Request a campaign</h2>
      <label className="block">Your instruction<textarea className={input} value={request} onChange={e => setRequest(e.target.value)} /></label>
      <label className="block">Proposal data<select className={input} value={testProposal ? "test" : "recorded"} onChange={e => setTestProposal(e.target.value === "test")}><option value="test">Marked test customers — development flow</option><option value="recorded">Actual recorded customers — review proposal only</option></select></label>
      <div className="flex flex-wrap gap-3"><button className={button} disabled={busy || listening} onClick={speak}>{listening ? "Listening…" : "Speak instruction"}</button>
      <button className={button} disabled={busy} onClick={() => void run(async () => { setInterpretation((await apiClient.post<Interpretation>(prefix + "/interpret", { message: request })).data); })}>Review instruction</button>
      <button className={button} disabled={busy} onClick={() => void run(async () => { const c = (await apiClient.post<Campaign>(prefix + "/ask", { message: request, test_mode: testProposal })).data; await refresh(); await open(c); })}>{testProposal ? "Prepare test proposal" : "Prepare recorded-data proposal for review"}</button>
      <button className={button} disabled={busy} onClick={() => void run(async () => { await apiClient.post(prefix + "/test-customers"); await refresh(); setNotice("Three clearly labeled test customers created. Two are eligible; one is opted out. No outreach sent."); })}>Create test customers</button></div>
      <p className="text-sm text-slate-400">Review spoken text before submitting. This first increment supports 90-day reactivation requests. Test history uses an active service with an exact price from your menu.</p>
      {interpretation && <div className="rounded-lg bg-slate-50 p-4 space-y-2 text-slate-900"><strong>{interpretation.kind.replace(/_/g, " ")}</strong><p>{interpretation.message}</p>{interpretation.options && <p>Options: {interpretation.options.join(", ") || "No matching menu service"}</p>}{interpretation.facts !== undefined && <pre className="overflow-auto text-xs">{JSON.stringify(interpretation.facts, null, 2)}</pre>}{interpretation.preferences && <><pre className="overflow-auto text-xs">{JSON.stringify(interpretation.preferences, null, 2)}</pre><button className={button} disabled={busy} onClick={() => void run(async () => { await apiClient.put(prefix + "/preferences", interpretation.preferences); setInterpretation(null); await refresh(); setNotice("Reviewed business preferences confirmed and saved. No outreach was sent."); })}>Confirm and save reviewed preference</button></>}</div>}
      <p className="text-sm text-slate-400">Do not include card details or patient information. Medical-office workflows are disabled pending verification; no HIPAA-compliance claim is made.</p>
    </section>
    <section className={panel}><h2 className="text-xl font-semibold">Approved business preferences</h2>
      <label className="block">Maximum discount (%)<input className={input} type="number" min="0" max="50" value={prefs.discount_limit_pct} onChange={e => setPrefs({ ...prefs, discount_limit_pct: Number(e.target.value) })} /></label>
      <label className="block">Services to promote (comma separated)<input className={input} value={prefs.services_to_promote.join(", ")} onChange={e => setPrefs({ ...prefs, services_to_promote: e.target.value.split(",").map(s => s.trim()).filter(Boolean) })} /></label>
      <label className="block">Appointment priority<select className={input} value={prefs.appointment_priority} onChange={e => setPrefs({ ...prefs, appointment_priority: e.target.value })}><option value="earliest_available">Earliest available</option><option value="preferred_provider">Preferred provider when verified</option></select></label>
      <label className="block">No-discount weekdays (mon, tue, wed, thu, fri, sat, sun)<input className={input} value={(prefs.discount_excluded_weekdays ?? []).join(", ")} onChange={e => setPrefs({ ...prefs, discount_excluded_weekdays: e.target.value.split(",").map(s => s.trim()).filter(Boolean) })} /></label>
      <label className="block">Promotion expires (ISO time with timezone; blank means no expiry)<input className={input} value={prefs.promotion_until ?? ""} onChange={e => setPrefs({ ...prefs, promotion_until: e.target.value || null })} /></label>
      <div className="flex gap-3"><button className={button} disabled={busy} onClick={() => void run(async () => { await apiClient.put(prefix + "/preferences", prefs); await refresh(); setNotice("Business preferences saved. Existing proposals require fresh approval if preferences changed."); })}>Approve and save preferences</button><button className={button} disabled={busy} onClick={() => void run(async () => { await apiClient.delete(prefix + "/preferences"); await refresh(); })}>Delete saved preferences</button></div>
      <p className="text-sm">The first flow offers existing services at their full menu price. Discounts and preferred-provider selection will be added in later increments.</p>
    </section>
    <section className={panel}><h2 className="text-xl font-semibold">Customer evidence</h2><p>Completed appointments only. Favorites are inferred; gift-card balances, membership benefits, provider preferences and revenue are unavailable in the current adapters.</p>
      <div className="overflow-x-auto"><table className="w-full text-left"><thead><tr><th>Customer / source</th><th>Visit history</th><th>Preferences and engagement</th><th>Eligibility checks</th></tr></thead><tbody>{insights.map(i => <tr key={i.contact_id} className="border-t"><td className="py-3">{i.customer}<div className="text-xs">{i.source}</div></td><td>{i.completed_visits} completed; {i.days_since_visit ?? "Unknown"} days since visit<div className="text-xs">Average interval: {i.average_days_between_visits ?? "Unknown"} days</div></td><td>Inferred: {i.favorite_services.map(s => `${s.name} (${s.duration_minutes} min)`).join(", ") || "Unknown"}<div className="text-xs">Recorded confirmed preferences: {JSON.stringify(i.confirmed_preferences)} · Recorded calls: {i.engagement?.call_count ?? "Unknown"}</div></td><td>{i.has_upcoming_appointment ? "Upcoming appointment" : "No upcoming appointment"}; {i.marketing_sms_allowed ? "SMS permission recorded" : "SMS not permitted"}</td></tr>)}</tbody></table></div>
    </section>
    {platform && <section className={panel}><h2 className="text-xl font-semibold">Establishment master assignment</h2><p>Platform administrators manage access. Only an explicitly assigned master of this establishment can approve its campaigns.</p>{masterUsers.map(u => <div key={u.id} className="flex items-center gap-3"><span>{u.name ?? u.id} · {u.is_business_master ? "Business master" : "Administrator"}</span><button className={button} disabled={busy || !u.is_active} onClick={() => void run(async () => { await apiClient.put(`${prefix}/master-users/${u.id}`, { is_business_master: !u.is_business_master }); await refresh(); })}>{u.is_business_master ? "Revoke master authority" : "Assign business master"}</button></div>)}</section>}
    <section className={panel}><h2 className="text-xl font-semibold">{master ? "Establishment approval inbox and campaigns" : "Campaigns awaiting establishment master approval"}</h2><div className="flex flex-wrap gap-2">{campaigns.map(c => <button key={c.id} className={button} disabled={busy} onClick={() => void run(() => open(c))}>{c.status} · {c.proposal.audience.length} customers · {c.id.slice(0, 8)}</button>)}</div>{!campaigns.length && <p>No proposals yet.</p>}</section>
    {selected && <section className={panel}><h2 className="text-xl font-semibold">Exact proposal · {selected.status}</h2><p>{selected.proposal.test_mode ? "SMS test outbox" : "Actual recorded customer data — review only; execution disabled"} · {new Date(selected.proposal.schedule).toLocaleString(undefined, { timeZone: selected.proposal.timezone })} · business timezone {selected.proposal.timezone}. Availability is checked when booking; no slot is promised in outreach.</p>
      <label className="block">Outreach schedule (ISO date and time with timezone)<input className={input} value={schedule} disabled={selected.status === "executed"} onChange={e => setSchedule(e.target.value)} /></label><p className="text-sm">Test runs are started manually after the scheduled time. Approval expires after 24 hours if outreach has not run.</p>
      {!selected.proposal.audience.length && <p>No eligible test customers with a matching, exactly priced service. Create test customers or review recorded history and consent.</p>}
      {selected.proposal.audience.map(p => <article className="rounded-lg border p-4 space-y-2" key={p.contact_id}><h3 className="font-semibold">{p.customer}: {p.offer.package_name}</h3><p>{p.offer.name} · {p.offer.duration_minutes} minutes · ${(p.offer.price_minor / 100).toFixed(2)} USD · discount {p.offer.discount_pct}%</p><p>Reason: {p.reason}</p><p className="text-sm">Price source: owner service menu. Approved offer is stored locally; no external package is created.</p><label className="block">Exact outreach message<textarea className={input} value={messages[p.contact_id] ?? p.message} disabled={selected.status === "executed"} onChange={e => setMessages({ ...messages, [p.contact_id]: e.target.value })} /></label></article>)}
      <div className="flex flex-wrap gap-3"><button className={button} disabled={busy || selected.status === "executed"} onClick={() => void run(async () => { const c = (await apiClient.patch<Campaign>(`${prefix}/campaigns/${selected.id}`, { expected_hash: selected.proposal_hash, messages, schedule })).data; await refresh(); await open(c); })}>Save messages and schedule — resets approval</button>
      {master && <button className={button} disabled={busy || dirty || selected.status !== "draft" || !selected.proposal.audience.length} onClick={() => void run(() => action("approve", { expected_hash: selected.proposal_hash }))}>Approve exact proposal as master</button>}
      <button className={button} disabled={busy || dirty || !selected.proposal.test_mode || selected.status !== "approved"} onClick={() => void run(() => action("execute-test"))}>Run approved test outreach</button></div><p className="text-sm">Save message edits before approving. Only master accounts can approve. Changed menu, preferences or proposal invalidate approval.</p>
    </section>}
    {results && selected && <section className={panel}><h2 className="text-xl font-semibold">Test results and replies</h2><p>{results.messages_sent_test} test messages · {results.responses} responses · {results.appointments_booked_test} simulated appointments. Verified revenue and redemptions: unavailable.</p>
      <label className="block">Simulated appointment time (your browser timezone)<input className={input} type="datetime-local" value={start} onChange={e => setStart(e.target.value)} /></label>
      <label className="block">Preferred staff member for this test (optional)<input className={input} value={preferredStaff} onChange={e => setPreferredStaff(e.target.value)} /></label><p className="text-sm">The shared booking-pipeline test requires a development backend/database. Availability and provider IDs below are simulated, not verified external openings. Payment collection is not exercised.</p>
      {results.deliveries.map(d => <article key={d.id} className="rounded-lg border p-4 space-y-2"><strong>{d.status}</strong><p>{d.message}</p><p>Response: {d.response ?? "None"}</p>{d.booking && <p>Simulator success: {d.booking.external_booking_id}. Local test result only; no external booking, payment or card collection.</p>}
      {!d.booking && d.status !== "skipped" && <><label className="block">Simulate customer reply<input className={input} value={reply[d.id] ?? "YES"} onChange={e => setReply({ ...reply, [d.id]: e.target.value })} /></label><label className="block">Name given by this test customer<input className={input} value={customerNames[d.id] ?? ""} onChange={e => setCustomerNames({ ...customerNames, [d.id]: e.target.value })} /></label><div className="flex flex-wrap gap-3"><button className={button} disabled={busy} onClick={() => void run(async () => { await apiClient.post(`${prefix}/deliveries/${d.id}/reply-test`, { message: reply[d.id] ?? "YES" }); await open(selected); })}>Record test reply</button><button className={button} disabled={busy || !start || !customerNames[d.id]?.trim() || d.status !== "responded_test"} onClick={() => void run(async () => { await apiClient.post(`${prefix}/deliveries/${d.id}/stage-test-booking`, { start: start, customer_name: customerNames[d.id], preferred_staff: preferredStaff || null }); await open(selected); })}>Check test time and read back appointment</button></div>
      {d.staged_booking?.slot && <div className="rounded-lg bg-blue-50 p-3 space-y-2 text-slate-900"><p>Read back: {d.staged_booking.customer_name}, {d.staged_booking.service}, {d.staged_booking.slot.duration_minutes} minutes, {new Date(d.staged_booking.slot.start).toLocaleString(undefined, { timeZone: selected.proposal.timezone })} ({selected.proposal.timezone}), test provider {d.staged_booking.slot.team_member_id}.</p><button className={button} disabled={busy || d.status !== "responded_test" || !start || start !== d.staged_booking.requested_local_start || customerNames[d.id] !== d.staged_booking.customer_name || (preferredStaff || null) !== d.staged_booking.preferred_staff} onClick={() => void run(async () => { await apiClient.post(`${prefix}/deliveries/${d.id}/confirm-test-booking`, { expected_fingerprint: d.staged_booking!.fingerprint, confirmation: "YES" }); await open(selected); })}>Simulate customer confirming this exact appointment</button></div>}</>}</article>)}
      <h3 className="font-semibold">Audit log</h3>{results.audit.map((e, index) => <p key={index} className="text-sm">{new Date(e.at).toLocaleString()} · {e.action}</p>)}
    </section>}
  </div>;
}
