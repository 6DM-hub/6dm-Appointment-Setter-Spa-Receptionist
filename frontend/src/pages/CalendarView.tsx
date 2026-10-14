import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Globe2, RefreshCw } from "lucide-react";
import {
  fetchAppointmentsInRange,
  fetchMySpaAccount,
  fetchSpaAccount,
  getApiErrorMessage,
  type Appointment,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { PageHeader, Panel, StateBlock } from "../components/ui/Primitives";
import {
  fetchWindow,
  layoutDay,
  todayIn,
  initialScrollMinute,
  weekDays,
  type DaySegment,
} from "../lib/calendarLayout";
import {
  addCivilDays,
  browserTimeZone,
  civilDateKey,
  isValidTimeZone,
  sameCivilDate,
  zonedMinuteOfDay,
  type CivilDate,
} from "../lib/zonedTime";

const STATUS_BLOCK_STYLES: Record<string, string> = {
  scheduled: "border-cyan-400/60 bg-cyan-400/15 text-cyan-100",
  confirmed: "border-emerald-400/60 bg-emerald-400/15 text-emerald-100",
  completed: "border-slate-500/60 bg-slate-700/40 text-slate-200",
  cancelled: "border-rose-400/40 bg-rose-400/5 text-rose-200/70 line-through",
  no_show: "border-amber-400/60 bg-amber-400/15 text-amber-100",
};

const CARD_STATUS_LABELS: Record<string, string> = {
  card_confirmed: "Card on file",
  pending_card: "Pending card",
  not_required: "Card not required",
  failed: "Card save failed",
  not_supported: "Card not supported",
  unknown: "Card status unknown",
};

const HOUR_HEIGHT = 56;
const PX_PER_MINUTE = HOUR_HEIGHT / 60;
const GRID_HEIGHT = 24 * HOUR_HEIGHT;

type ZoneState =
  | { status: "loading" }
  | { status: "ready"; timeZone: string; source: "spa" | "browser" }
  | { status: "error"; message: string };

interface WeekData {
  items: Appointment[];
  total: number;
  complete: boolean;
}

// Civil dates are formatted as UTC midnights so the browser's zone never shifts them.
const utcFormat = (options: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat(undefined, { ...options, timeZone: "UTC" });
const weekdayLabel = utcFormat({ weekday: "short" });
const monthDayLabel = utcFormat({ month: "short", day: "numeric" });
const fullDateLabel = utcFormat({ month: "short", day: "numeric", year: "numeric" });
const hourLabel = utcFormat({ hour: "numeric" });
const civilUtc = (d: CivilDate) => Date.UTC(d.year, d.month - 1, d.day);

const toolbarButton =
  "rounded-lg border border-slate-700 text-slate-300 transition hover:bg-slate-800 disabled:opacity-40";

/**
 * Week calendar for whichever schedule the caller's scope maps to: Dominic's
 * sales calendar for the 6DM workspace, the spa's service calendar for a
 * tenant. Read-only. Times are drawn in the spa's configured time zone; the
 * sales workspace has no zone setting, so it uses the browser's and says so.
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
  const { impersonatedTenantId, effectiveTenantId } = useAuth();
  const [zone, setZone] = useState<ZoneState>({ status: "loading" });
  const [anchor, setAnchor] = useState<CivilDate | null>(null);
  const [week, setWeek] = useState<WeekData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, []);

  // Resolve the zone before drawing anything: a wrong zone shifts every block.
  useEffect(() => {
    let cancelled = false;
    setZone({ status: "loading" });
    setAnchor(null);
    if (!effectiveTenantId) {
      setZone({ status: "ready", timeZone: browserTimeZone(), source: "browser" });
      return;
    }
    // Same lookup as SpaSettings: a super admin reads the spa picked in the
    // switcher; a spa user's own tenant comes from their token.
    const request = impersonatedTenantId
      ? fetchSpaAccount(impersonatedTenantId)
      : fetchMySpaAccount();
    request
      .then((account) => {
        if (cancelled) return;
        setZone(
          isValidTimeZone(account.timezone)
            ? { status: "ready", timeZone: account.timezone, source: "spa" }
            : {
                status: "error",
                message: `This spa's time zone "${account.timezone}" is not recognised, so times can't be shown safely. Fix it in Receptionist settings.`,
              }
        );
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setZone({
            status: "error",
            message: getApiErrorMessage(err, "Unable to load the spa's time zone."),
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [impersonatedTenantId, effectiveTenantId]);

  const timeZone = zone.status === "ready" ? zone.timeZone : null;
  const today = timeZone ? todayIn(timeZone, now) : null;
  const todayKey = today ? civilDateKey(today) : null;
  const anchorKey = anchor ? civilDateKey(anchor) : null;
  // Keyed on strings so the per-minute clock tick doesn't rebuild the week and refetch.
  const days = useMemo(() => {
    const base = anchor ?? today;
    return timeZone && base ? weekDays(base, timeZone) : [];
  }, [anchorKey, todayKey, timeZone]);

  useEffect(() => {
    if (days.length === 0) return;
    let cancelled = false;
    const { from, to } = fetchWindow(days);
    // Clear first so a previous tenant's or week's rows are never drawn under new headers.
    setWeek(null);
    setLoading(true);
    setError(null);
    fetchAppointmentsInRange(new Date(from).toISOString(), new Date(to).toISOString())
      .then((result) => {
        if (!cancelled) setWeek(result);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(getApiErrorMessage(err, "Unable to load the calendar."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [days, reloadToken]);

  const columns = useMemo(
    () =>
      timeZone
        ? days.map((day) => ({ day, segments: layoutDay(week?.items ?? [], day, timeZone) }))
        : [],
    [days, week, timeZone]
  );
  // Open the day at its first booking whenever a week's data arrives. The grid
  // only mounts once loading ends, so that has to retrigger this too.
  useEffect(() => {
    if (loading || !scrollRef.current) return;
    const minute = initialScrollMinute(columns.flatMap((column) => column.segments));
    scrollRef.current.scrollTop = minute * PX_PER_MINUTE;
  }, [columns, loading]);
  const shownCount = useMemo(
    () => new Set(columns.flatMap((c) => c.segments.map((s) => s.item.id))).size,
    [columns]
  );
  const timeLabel = useMemo(
    () =>
      timeZone
        ? new Intl.DateTimeFormat(undefined, { timeZone, hour: "numeric", minute: "2-digit" })
        : null,
    [timeZone]
  );

  let zoneLabel = "";
  if (timeZone) {
    const abbreviation = new Intl.DateTimeFormat(undefined, { timeZone, timeZoneName: "short" })
      .formatToParts(now)
      .find((part) => part.type === "timeZoneName")?.value;
    zoneLabel = abbreviation ? `${timeZone} (${abbreviation})` : timeZone;
  }
  const usingBrowserZone = zone.status === "ready" && zone.source === "browser";

  const rangeLabel =
    days.length > 0
      ? `${monthDayLabel.format(civilUtc(days[0].date))} – ${fullDateLabel.format(
          civilUtc(days[6].date)
        )}`
      : "Week";
  const shift = (weeks: number) => {
    if (days.length > 0) setAnchor(addCivilDays(days[0].date, weeks * 7));
  };
  const isCurrentWeek = !!today && days.some((day) => sameCivilDate(day.date, today));
  const nowMinute = timeZone ? zonedMinuteOfDay(now, timeZone) : -1;

  const toolbar = (
    <div className="flex flex-wrap items-center gap-2">
      <button
        type="button"
        onClick={() => setAnchor(null)}
        disabled={!timeZone || isCurrentWeek}
        className={`${toolbarButton} px-3 py-1.5 text-xs font-semibold text-slate-200`}
      >
        Today
      </button>
      <button
        type="button"
        aria-label="Previous week"
        onClick={() => shift(-1)}
        disabled={!timeZone}
        className={`${toolbarButton} p-1.5`}
      >
        <ChevronLeft size={14} />
      </button>
      <button
        type="button"
        aria-label="Next week"
        onClick={() => shift(1)}
        disabled={!timeZone}
        className={`${toolbarButton} p-1.5`}
      >
        <ChevronRight size={14} />
      </button>
      <button
        type="button"
        aria-label="Refresh"
        onClick={() => setReloadToken((n) => n + 1)}
        disabled={!timeZone || loading}
        className={`${toolbarButton} p-1.5`}
      >
        <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
      </button>
    </div>
  );

  return (
    <>
      <PageHeader eyebrow={eyebrow} title={title} subtitle={subtitle} />

      <Panel
        title={rangeLabel}
        subtitle={
          timeZone
            ? `${loading ? "Loading" : `${shownCount} appointment${shownCount === 1 ? "" : "s"}`}` +
              ` · times in ${zoneLabel}${usingBrowserZone ? " (your browser)" : ""}`
            : undefined
        }
        actions={toolbar}
        padded={false}
      >
        <StateBlock
          loading={zone.status === "loading" || loading}
          error={zone.status === "error" ? zone.message : error}
        >
          {week && !week.complete && (
            <p className="border-b border-amber-400/20 bg-amber-400/10 px-5 py-2 text-[11px] text-amber-200">
              Some appointments may be missing: loaded {week.items.length} of {week.total}{" "}
              reported. Refresh to try again.
            </p>
          )}
          {usingBrowserZone && (
            <p className="flex items-center gap-2 border-b border-slate-800 px-5 py-2 text-[11px] text-slate-500">
              <Globe2 size={12} /> This workspace has no time zone setting, so times use your
              browser's zone.
            </p>
          )}
          <div ref={scrollRef} className="max-h-[640px] overflow-auto">
            <div className="min-w-[880px]">
              <div className="sticky top-0 z-20 grid grid-cols-[56px_repeat(7,minmax(0,1fr))] border-b border-slate-800 bg-[#091525]">
                <div />
                {days.map((day) => {
                  const isToday = !!today && sameCivilDate(day.date, today);
                  return (
                    <div key={day.key} className="border-l border-slate-800 px-2 py-2 text-center">
                      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
                        {weekdayLabel.format(civilUtc(day.date))}
                      </p>
                      <p
                        className={`mx-auto mt-1 grid h-7 w-7 place-items-center rounded-full text-sm font-semibold ${
                          isToday ? "bg-cyan-400 text-slate-950" : "text-slate-200"
                        }`}
                      >
                        {day.date.day}
                      </p>
                    </div>
                  );
                })}
              </div>

              <div className="grid grid-cols-[56px_repeat(7,minmax(0,1fr))]">
                <div className="relative" style={{ height: GRID_HEIGHT }}>
                  {Array.from({ length: 23 }, (_, i) => (
                    <span
                      key={i}
                      className="absolute right-2 -translate-y-1/2 text-[10px] text-slate-500"
                      style={{ top: (i + 1) * HOUR_HEIGHT }}
                    >
                      {hourLabel.format(Date.UTC(2000, 0, 1, i + 1))}
                    </span>
                  ))}
                </div>
                {columns.map(({ day, segments }) => (
                  <div
                    key={day.key}
                    className="relative border-l border-slate-800"
                    style={{
                      height: GRID_HEIGHT,
                      backgroundImage:
                        "repeating-linear-gradient(to bottom, rgba(51,65,85,.55) 0 1px, transparent 1px 100%)",
                      backgroundSize: `100% ${HOUR_HEIGHT}px`,
                    }}
                  >
                    {segments.map((segment) => (
                      <AppointmentBlock key={segment.item.id} segment={segment} timeLabel={timeLabel} />
                    ))}
                    {today && sameCivilDate(day.date, today) && (
                      <div
                        aria-hidden
                        className="pointer-events-none absolute inset-x-0 z-10 h-0.5 bg-rose-500"
                        style={{ top: nowMinute * PX_PER_MINUTE }}
                      >
                        <span className="absolute -left-1 -top-[3px] h-2 w-2 rounded-full bg-rose-500" />
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
          {week && shownCount === 0 && (
            <p className="border-t border-slate-800 px-5 py-3 text-center text-xs text-slate-500">
              No appointments this week.
            </p>
          )}
        </StateBlock>
      </Panel>
    </>
  );
}

function AppointmentBlock({
  segment,
  timeLabel,
}: {
  segment: DaySegment<Appointment>;
  timeLabel: Intl.DateTimeFormat | null;
}) {
  const { item, startMinute, endMinute, lane, laneCount } = segment;
  const range = timeLabel
    ? `${timeLabel.format(new Date(item.start_time))} – ${timeLabel.format(new Date(item.end_time))}`
    : "";
  const card = item.card_status ? CARD_STATUS_LABELS[item.card_status] : undefined;
  const status = item.status.replace(/_/g, " ");
  const tooltip = [item.title, range, status, card, item.description].filter(Boolean).join("\n");
  const height = (endMinute - startMinute) * PX_PER_MINUTE;

  return (
    <div
      title={tooltip}
      className={`absolute overflow-hidden rounded-md border-l-2 px-1.5 py-1 text-[10px] leading-tight ${
        STATUS_BLOCK_STYLES[item.status] || STATUS_BLOCK_STYLES.completed
      }`}
      style={{
        top: startMinute * PX_PER_MINUTE + 1,
        height: Math.max(height - 2, 14),
        left: `calc(${(lane / laneCount) * 100}% + 2px)`,
        width: `calc(${100 / laneCount}% - 4px)`,
      }}
    >
      <p className="truncate font-semibold">
        {segment.continuesBefore ? "↑ " : ""}
        {range}
      </p>
      {height >= 28 && <p className="truncate">{item.title}</p>}
      {height >= 44 && (
        <p className="truncate opacity-70">
          {status}
          {card ? ` · ${card}` : ""}
        </p>
      )}
    </div>
  );
}
