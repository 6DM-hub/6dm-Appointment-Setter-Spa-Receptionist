import { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw, Search } from "lucide-react";
import { createLead, fetchLeads, type Lead } from "../../api/client";
import { PageHeader, Panel, StateBlock } from "../../components/ui/Primitives";

/** The 6DM B2B pipeline. Reachable only by a super admin — the route guard and
 *  the `/api/v1/leads` 403 both enforce it. */
export default function LeadsPage() {
  const [leads, setLeads] = useState<Lead[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [newPhone, setNewPhone] = useState("");
  const [newName, setNewName] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async (term: string) => {
    setLoading(true);
    try {
      const page = await fetchLeads(term ? { search: term } : undefined);
      setLeads(page.items);
      setTotal(page.total);
      setError(null);
    } catch {
      setError("Unable to load the lead pipeline.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(search);
    // Debounce so typing doesn't fire a request per keystroke.
  }, [load, search]);

  const submitLead = async (event: React.FormEvent) => {
    event.preventDefault();
    setFormError(null);
    const [first, ...rest] = newName.trim().split(" ");
    try {
      await createLead({
        phone_number: newPhone.trim(),
        first_name: first || null,
        last_name: rest.join(" ") || null,
      });
      setNewPhone("");
      setNewName("");
      setAdding(false);
      await load(search);
    } catch (err: unknown) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response
        ?.data?.detail;
      setFormError(detail || "Unable to add this lead.");
    }
  };

  return (
    <>
      <PageHeader
        eyebrow="6DM Sales Agent"
        title="Leads"
        subtitle={`${total} B2B prospect${total === 1 ? "" : "s"} in the outbound pipeline.`}
        actions={
          <>
            <button
              onClick={() => void load(search)}
              className="rounded-xl border border-slate-700 p-2.5 text-slate-400 transition hover:text-white"
              aria-label="Refresh"
            >
              <RefreshCw size={15} />
            </button>
            <button
              onClick={() => setAdding((value) => !value)}
              className="inline-flex items-center gap-2 rounded-xl bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 transition hover:bg-cyan-300"
            >
              <Plus size={15} /> Add lead
            </button>
          </>
        }
      />

      {adding && (
        <Panel title="New lead">
          <form onSubmit={submitLead} className="flex flex-col gap-3 sm:flex-row">
            <input
              required
              value={newPhone}
              onChange={(event) => setNewPhone(event.target.value)}
              placeholder="+15550102040"
              className="flex-1 rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none placeholder:text-slate-600"
            />
            <input
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              placeholder="Jordan Ellis"
              className="flex-1 rounded-lg border border-slate-700 bg-[#07111f] px-3 py-2.5 text-sm text-white outline-none placeholder:text-slate-600"
            />
            <button
              type="submit"
              className="rounded-lg bg-cyan-400 px-4 py-2.5 text-xs font-bold text-slate-950 hover:bg-cyan-300"
            >
              Save
            </button>
          </form>
          {formError && <p className="mt-3 text-xs text-rose-300">{formError}</p>}
        </Panel>
      )}

      <Panel
        title="Pipeline"
        subtitle="Call history and booked presentations per prospect."
        padded={false}
        actions={
          <div className="flex items-center gap-2 rounded-lg border border-slate-700 bg-[#07111f] px-3">
            <Search size={14} className="text-slate-500" />
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search name, phone or email"
              className="w-56 bg-transparent py-2 text-xs text-white outline-none placeholder:text-slate-600"
            />
          </div>
        }
      >
        <StateBlock
          loading={loading}
          error={error}
          empty={leads.length === 0}
          emptyLabel={search ? "No leads match that search." : "No leads yet."}
        >
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-left">
              <thead className="bg-[#091525] text-[10px] uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="px-5 py-3 font-semibold">Prospect</th>
                  <th className="px-4 py-3 font-semibold">Phone</th>
                  <th className="px-4 py-3 font-semibold">Calls</th>
                  <th className="px-4 py-3 font-semibold">Last contacted</th>
                  <th className="px-4 py-3 font-semibold">Presentation</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {leads.map((lead) => (
                  <tr key={lead.id} className="transition hover:bg-slate-800/40">
                    <td className="px-5 py-4">
                      <p className="text-xs font-semibold text-slate-200">
                        {lead.full_name}
                      </p>
                      {lead.email && (
                        <p className="mt-0.5 text-[10px] text-slate-500">{lead.email}</p>
                      )}
                    </td>
                    <td className="px-4 py-4 text-xs text-slate-400">
                      {lead.phone_number}
                    </td>
                    <td className="px-4 py-4 text-xs text-slate-400">
                      {lead.call_count}
                    </td>
                    <td className="px-4 py-4 text-xs text-slate-500">
                      {lead.last_call_at
                        ? new Date(lead.last_call_at).toLocaleDateString()
                        : "Never"}
                    </td>
                    <td className="px-4 py-4">
                      {lead.upcoming_appointment_at ? (
                        <span className="rounded-md bg-emerald-400/10 px-2 py-1 text-[10px] font-semibold text-emerald-300">
                          {new Date(lead.upcoming_appointment_at).toLocaleString()}
                        </span>
                      ) : (
                        <span className="rounded-md bg-slate-700/40 px-2 py-1 text-[10px] font-semibold text-slate-400">
                          Not booked
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </StateBlock>
      </Panel>
    </>
  );
}
