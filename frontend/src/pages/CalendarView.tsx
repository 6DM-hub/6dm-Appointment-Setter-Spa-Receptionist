import { useEffect, useMemo, useState } from "react";
import { CalendarDays } from "lucide-react";
import { fetchAppointments, type Appointment } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { PageHeader, Panel, StateBlock } from "../components/ui/Primitives";

const STATUS_STYLES: Record<string, string> = {
  scheduled: "bg-cyan-400/10 text-cyan-300",
  confirmed: "bg-emerald-400/10 text-emerald-300",
  completed: "bg-slate-700/40 text-slate-300",
  cancelled: "bg-rose-400/10 text-rose-300",
  no_show: "bg-amber-400/10 text-amber-300",
};

const CARD_STATUS_LABELS: Record<string, string> = {
  card_confirmed: "Card on file",
  pending_card: "Pending card",
  not_required: "Card not required",
  failed: "Card save failed",
  not_supported: "Card not supported",
  unknown: "Card status unknown",
};

const CARD_STATUS_STYLES: Record<string, string> = {
  card_confirmed: "bg-emerald-400/10 text-emerald-300",
  pending_card: "bg-amber-400/10 text-amber-300",
  not_required: "bg-slate-700/40 text-slate-400",
  failed: "bg-rose-400/10 text-rose-300",
  not_supported: "bg-slate-700/40 text-slate-400",
  unknown: "bg-slate-700/40 text-slate-400",
};

/**
 * Upcoming bookings for whichever calendar the caller's scope maps to:
 * Dominic's sales calendar for the 6DM workspace, the spa's service calendar
 * for a tenant. The API filters by scope, so this component needs no branch.
 */
export default function CalendarView({
  eyebrow = "Schedule",
  title = "Calendar",
  subtitle,
}: {
  eyebrow?: string;
  title?: string;
  subtitle?: string;
}) {
  const { impersonatedTenantId } = useAuth();
  const [appointments, setAppointments] = useState<Appointment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    fetchAppointments({ from_time: new Date().toISOString() })
      .then((rows) => {
        setAppointments(rows);
        setError(null);
      })
      .catch(() => setError("Unable to load the calendar."))
      .finally(() => setLoading(false));
  }, [impersonatedTenantId]);

  const byDay = useMemo(() => {
    const groups = new Map<string, Appointment[]>();
    for (const appointment of appointments) {
      const day = new Date(appointment.start_time).toDateString();
      groups.set(day, [...(groups.get(day) || []), appointment]);
    }
    return [...groups.entries()];
  }, [appointments]);

  return (
    <>
      <PageHeader eyebrow={eyebrow} title={title} subtitle={subtitle} />

      <Panel
        title="Upcoming"
        subtitle={`${appointments.length} booking${appointments.length === 1 ? "" : "s"} ahead`}
        padded={false}
      >
        <StateBlock
          loading={loading}
          error={error}
          empty={appointments.length === 0}
          emptyLabel="Nothing on the calendar yet."
        >
          <div className="divide-y divide-slate-800/80">
            {byDay.map(([day, rows]) => (
              <div key={day} className="px-5 py-4">
                <p className="mb-3 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[.18em] text-slate-500">
                  <CalendarDays size={12} /> {day}
                </p>
                <div className="space-y-3">
                  {rows.map((appointment) => (
                    <div
                      key={appointment.id}
                      className="flex gap-3 border-l-2 border-slate-700 pl-3"
                    >
                      <span className="w-12 pt-0.5 text-[11px] font-semibold text-slate-400">
                        {new Date(appointment.start_time).toLocaleTimeString([], {
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-xs font-medium text-slate-200">
                          {appointment.title}
                        </p>
                        <p className="mt-1 text-[10px] text-slate-500">
                          {Math.round(
                            (new Date(appointment.end_time).getTime() -
                              new Date(appointment.start_time).getTime()) /
                              60000
                          )}{" "}
                          min
                          {appointment.booking_provider
                            ? ` · ${appointment.booking_provider.replace(/_/g, " ")}`
                            : ""}
                        </p>
                      </div>
                      <div className="flex flex-col items-end gap-1">
                        <span
                          className={`h-fit rounded-md px-2 py-1 text-[10px] font-semibold ${
                            STATUS_STYLES[appointment.status] ||
                            "bg-slate-700/40 text-slate-300"
                          }`}
                        >
                          {appointment.status.replace(/_/g, " ")}
                        </span>
                        {appointment.card_status && CARD_STATUS_LABELS[appointment.card_status] && (
                          <span
                            className={`h-fit rounded-md px-2 py-1 text-[10px] font-semibold ${
                              CARD_STATUS_STYLES[appointment.card_status] ||
                              "bg-slate-700/40 text-slate-300"
                            }`}
                          >
                            {CARD_STATUS_LABELS[appointment.card_status]}
                          </span>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </StateBlock>
      </Panel>
    </>
  );
}
