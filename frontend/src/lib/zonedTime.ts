/**
 * Wall-clock arithmetic in an IANA time zone using only `Intl`.
 *
 * The API returns UTC instants; a spa's calendar must be drawn in the spa's
 * own zone, not the browser's. Civil dates (year/month/day) are handled as
 * plain numbers and converted to instants only at day boundaries, so DST
 * days of 23 or 25 hours fall out naturally.
 */

export interface CivilDate {
  year: number;
  /** 1-12 */
  month: number;
  day: number;
}

export interface ZonedParts extends CivilDate {
  hour: number;
  minute: number;
  second: number;
  /** 0 = Sunday … 6 = Saturday */
  weekday: number;
}

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;

const partsFormatters = new Map<string, Intl.DateTimeFormat>();

function partsFormatter(timeZone: string): Intl.DateTimeFormat {
  let formatter = partsFormatters.get(timeZone);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat("en-US", {
      timeZone,
      hourCycle: "h23",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
    partsFormatters.set(timeZone, formatter);
  }
  return formatter;
}

export function isValidTimeZone(timeZone: string | null | undefined): timeZone is string {
  if (!timeZone) return false;
  try {
    partsFormatter(timeZone);
    return true;
  } catch {
    return false;
  }
}

export function browserTimeZone(): string {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

export function zonedParts(instant: number | Date, timeZone: string): ZonedParts {
  const values: Record<string, number> = {};
  for (const part of partsFormatter(timeZone).formatToParts(instant)) {
    if (part.type !== "literal") values[part.type] = Number(part.value);
  }
  const civil = { year: values.year, month: values.month, day: values.day };
  return {
    ...civil,
    hour: values.hour,
    minute: values.minute,
    second: values.second,
    weekday: weekdayOf(civil),
  };
}

/** Offset of `timeZone` from UTC at `instant`, in ms (e.g. -5h for CDT). */
function offsetAt(instant: number, timeZone: string): number {
  const p = zonedParts(instant, timeZone);
  const asUtc = Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute, p.second);
  return asUtc - Math.floor(instant / 1000) * 1000;
}

/**
 * The first instant whose wall-clock date in `timeZone` is `date`.
 * Handles zones whose DST change skips local midnight.
 */
export function startOfZonedDay(date: CivilDate, timeZone: string): number {
  const wallMidnight = Date.UTC(date.year, date.month - 1, date.day);
  let instant = wallMidnight - offsetAt(wallMidnight, timeZone);
  instant = wallMidnight - offsetAt(instant, timeZone);
  // If midnight does not exist locally the estimate can land on the previous
  // day; step forward to the first hour that belongs to `date`.
  for (let i = 0; i < 3 && !sameCivilDate(zonedParts(instant, timeZone), date); i++) {
    instant += HOUR_MS;
  }
  return instant;
}

export function zonedCivilDate(instant: number | Date, timeZone: string): CivilDate {
  const { year, month, day } = zonedParts(instant, timeZone);
  return { year, month, day };
}

export function addCivilDays(date: CivilDate, days: number): CivilDate {
  const d = new Date(Date.UTC(date.year, date.month - 1, date.day + days));
  return { year: d.getUTCFullYear(), month: d.getUTCMonth() + 1, day: d.getUTCDate() };
}

export function weekdayOf(date: CivilDate): number {
  return new Date(Date.UTC(date.year, date.month - 1, date.day)).getUTCDay();
}

export function sameCivilDate(a: CivilDate, b: CivilDate): boolean {
  return a.year === b.year && a.month === b.month && a.day === b.day;
}

export function civilDateKey(date: CivilDate): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.year}-${pad(date.month)}-${pad(date.day)}`;
}

/** Minutes past local midnight of `instant` on the wall clock in `timeZone`. */
export function zonedMinuteOfDay(instant: number, timeZone: string): number {
  const p = zonedParts(instant, timeZone);
  return p.hour * 60 + p.minute;
}
