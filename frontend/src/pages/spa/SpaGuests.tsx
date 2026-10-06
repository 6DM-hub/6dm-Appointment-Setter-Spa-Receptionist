import { useEffect, useState } from "react";
import { fetchContacts, getApiErrorMessage, type Contact } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";
import { PageHeader, Panel, StateBlock } from "../../components/ui/Primitives";

/** Guests the receptionist has recorded for this spa. Tenant-filtered server side. */
export default function SpaGuests() {
  const { impersonatedTenantId } = useAuth();
  const [guests, setGuests] = useState<Contact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    fetchContacts()
      .then((rows) => {
        setGuests(rows);
        setError(null);
      })
      .catch((err) => setError(getApiErrorMessage(err, "Unable to load guests.")))
      .finally(() => setLoading(false));
  }, [impersonatedTenantId]);

  return (
    <>
      <PageHeader
        eyebrow="Spa Receptionist"
        title="Guests"
        subtitle="People who have called in. Created automatically when the receptionist books them."
      />

      <Panel
        title="Guest list"
        subtitle={`${guests.length} guest${guests.length === 1 ? "" : "s"}`}
        padded={false}
      >
        <StateBlock
          loading={loading}
          error={error}
          empty={guests.length === 0}
          emptyLabel="No guests recorded yet."
        >
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-left">
              <thead className="bg-[#091525] text-[10px] uppercase tracking-wider text-slate-500">
                <tr>
                  <th className="px-5 py-3 font-semibold">Guest</th>
                  <th className="px-4 py-3 font-semibold">Phone</th>
                  <th className="px-4 py-3 font-semibold">Email</th>
                  <th className="px-4 py-3 font-semibold">First seen</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/80">
                {guests.map((guest) => (
                  <tr key={guest.id} className="transition hover:bg-slate-800/40">
                    <td className="px-5 py-4 text-xs font-semibold text-slate-200">
                      {guest.full_name}
                    </td>
                    <td className="px-4 py-4 text-xs text-slate-400">
                      {guest.phone_number}
                    </td>
                    <td className="px-4 py-4 text-xs text-slate-500">
                      {guest.email || "—"}
                    </td>
                    <td className="px-4 py-4 text-xs text-slate-500">
                      {new Date(guest.created_at).toLocaleDateString()}
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
