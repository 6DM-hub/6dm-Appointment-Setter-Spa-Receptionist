import type { SpaAccount, SpaService, BusinessHoursWindow } from "../api/client";

type Hours = Record<string, BusinessHoursWindow[]>;
const days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const input = "rounded border border-slate-600 bg-slate-900 p-2 text-sm";

function HoursEditor({ value, onChange, special = false }: { value: Hours; onChange: (next: Hours) => void; special?: boolean }) {
  return <div className="space-y-2">{(special ? Object.keys(value) : days).map(day => <div key={day} className="flex flex-wrap items-center gap-2">
    <span className="w-28">{day}</span>
    {(value[day] ?? []).map((window, index) => <span key={index} className="flex gap-2">
      {(["open", "close"] as const).map(field => <input key={field} aria-label={`${day} ${field}`} type="time" className={input} value={window[field]} onChange={e => onChange({ ...value, [day]: value[day].map((v, i) => i === index ? { ...v, [field]: e.target.value } : v) })} />)}
      <button type="button" onClick={() => onChange({ ...value, [day]: value[day].filter((_, i) => i !== index) })}>Remove window</button>
    </span>)}
    {!value[day]?.length && <span>Closed</span>}
    <button type="button" onClick={() => onChange({ ...value, [day]: [...(value[day] ?? []), { open: "09:00", close: "18:00" }] })}>Add window</button>
    {special && <button type="button" onClick={() => { const next = { ...value }; delete next[day]; onChange(next); }}>Remove exception</button>}
  </div>)}{special && <label>Add a special date (starts closed) <input aria-label="New special date" type="date" className={input} onChange={e => { if (e.target.value) onChange({ ...value, [e.target.value]: value[e.target.value] ?? [] }); }} /></label>}</div>;
}

export default function VisitSettings({ spa, onChange, disabled }: { spa: SpaAccount; onChange: (spa: SpaAccount) => void; disabled: boolean }) {
  const visit = spa.booking_policies?.visit ?? {};
  const policy = (next: Partial<typeof visit>) => onChange({ ...spa, booking_policies: { ...spa.booking_policies, visit: { ...visit, ...next } } });
  const service = (index: number, next: Partial<SpaService>) => onChange({ ...spa, services: spa.services.map((s, i) => i === index ? { ...s, ...next } : s) });
  return <fieldset disabled={disabled} className="space-y-5 rounded-xl border border-slate-700 p-5 text-slate-200">
    <legend>Complete visits and scheduling rules</legend>
    <p>Square verifies the whole visit, including staff and provider-managed resources. Other integrations require staff assistance for combined treatments.</p>
    {([['enabled', 'Allow multiple services in one visit'], ['allow_reorder', 'Allow Cara to rearrange treatment order'], ['allow_after_hours', 'Allow verified after-hours visits']] as const).map(([key, label]) => <label key={key} className="block"><input type="checkbox" checked={visit[key] ?? (key === 'enabled')} onChange={e => policy({ [key]: e.target.checked })} /> {label}</label>)}
    <details><summary>Holiday and special opening hours</summary><HoursEditor special value={visit.special_hours ?? {}} onChange={special_hours => policy({ special_hours })} /></details>
    {visit.allow_after_hours && <details><summary>Permitted after-hours windows</summary><HoursEditor value={visit.after_hours ?? {}} onChange={after_hours => policy({ after_hours })} /></details>}
    <p>Configure buffers and rooms in Square as well. Cara refuses visits when Square cannot verify these requirements. Preparation buffers currently require staff-assisted booking.</p>
    {spa.services.map((s, index) => <div key={index} className="space-y-2 border-t border-slate-700 pt-3"><strong>{s.name}</strong><div className="flex flex-wrap gap-3">
      {([['preparation_buffer_minutes', 'Preparation'], ['transition_buffer_minutes', 'Transition'], ['cleanup_buffer_minutes', 'Cleanup']] as const).map(([key, label]) => <label key={key}>{label} minutes <input className={`${input} w-20`} min={0} max={180} type="number" value={s[key] ?? 0} onChange={e => service(index, { [key]: Number(e.target.value) })} /></label>)}
      <label>Required Square room / equipment IDs <input className={input} value={(s.resource_ids ?? []).join(', ')} onChange={e => service(index, { resource_ids: e.target.value.split(',').map(x => x.trim()).filter(Boolean) })} /></label>
    </div></div>)}
    <p>Qualifications remain in the team settings. Staff schedules below further restrict Square availability; leaving the weekly schedule empty uses Square's schedule.</p>
    {spa.staff.map((member, index) => {
      const update = (next: Partial<typeof member>) => onChange({ ...spa, staff: spa.staff.map((m, i) => i === index ? { ...m, ...next } : m) });
      return <details key={index}><summary>{member.name || 'Staff member'} — schedule</summary>
        <label>Square team member ID <input className={input} value={member.provider_id ?? ''} onChange={e => update({ provider_id: e.target.value })} /></label>
        <HoursEditor value={member.hours ?? {}} onChange={hours => update({ hours })} />
        <details><summary>Staff special dates</summary><HoursEditor special value={member.special_hours ?? {}} onChange={special_hours => update({ special_hours })} /></details>
      </details>;
    })}
  </fieldset>;
}
