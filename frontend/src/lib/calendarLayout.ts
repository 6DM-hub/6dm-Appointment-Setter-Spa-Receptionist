/**
 * Pure layout math for the week calendar: which spa-local days are shown,
 * which UTC window to request, and where each appointment sits in a day
 * column. No React here so it can be unit tested under plain Node.
 */
import {
  addCivilDays,
  civilDateKey,
  startOfZonedDay,
  weekdayOf,
  zonedCivilDate,
  zonedMinuteOfDay,
  type CivilDate,
} from "./zonedTime.ts";

export const MINUTES_PER_DAY = 24 * 60;
/** Shortest block drawn, so a zero-length or tiny booking stays clickable. */
export const MIN_BLOCK_MINUTES = 15;
/**
 * The API filters on `start_time` only, so a booking that starts the evening
 * before the week and runs past midnight would be missed. Spa appointments
 * are far shorter than a day; look back this far and clip client side.
 */
export const LOOKBACK_MS = 24 * 60 * 60 * 1000;

export interface TimedItem {
  id: string;
  start_time: string;
  end_time: string;
}

export interface CalendarDay {
  date: CivilDate;
  key: string;
  /** First instant of the local day (UTC ms). */
  start: number;
  /** First instant of the next local day (UTC ms); 23h/25h on DST days. */
  end: number;
}

export interface DaySegment<T> {
  item: T;
  /** Minutes past local midnight, on the wall clock. */
  startMinute: number;
  endMinute: number;
  continuesBefore: boolean;
  continuesAfter: boolean;
  lane: number;
  laneCount: number;
}

export function calendarDay(date: CivilDate, timeZone: string): CalendarDay {
  return {
    date,
    key: civilDateKey(date),
    start: startOfZonedDay(date, timeZone),
    end: startOfZonedDay(addCivilDays(date, 1), timeZone),
  };
}

/** The 7 spa-local days of the week containing `anchor`. Sunday-first by default. */
export function weekDays(anchor: CivilDate, timeZone: string, weekStartsOn = 0): CalendarDay[] {
  const offset = (weekdayOf(anchor) - weekStartsOn + 7) % 7;
  const first = addCivilDays(anchor, -offset);
  return Array.from({ length: 7 }, (_, i) => calendarDay(addCivilDays(first, i), timeZone));
}

export function todayIn(timeZone: string, now: number = Date.now()): CivilDate {
  return zonedCivilDate(now, timeZone);
}

/**
 * Query bounds for `GET /appointments`. Both bounds are inclusive and apply
 * to `start_time`, so the upper bound stops 1 ms before the next week.
 */
export function fetchWindow(days: CalendarDay[]): { from: number; to: number } {
  return { from: days[0].start - LOOKBACK_MS, to: days[days.length - 1].end - 1 };
}

/** Appointments overlapping `day`, clipped to it and assigned side-by-side lanes. */
export function layoutDay<T extends TimedItem>(
  items: T[],
  day: CalendarDay,
  timeZone: string
): DaySegment<T>[] {
  const segments: Omit<DaySegment<T>, "lane" | "laneCount">[] = [];

  for (const item of items) {
    const start = Date.parse(item.start_time);
    let end = Date.parse(item.end_time);
    if (Number.isNaN(start) || Number.isNaN(end)) continue;
    if (end < start) end = start;
    const overlaps = start < day.end && (end > day.start || (end === start && start >= day.start));
    if (!overlaps) continue;

    const segStart = Math.max(start, day.start);
    const segEnd = Math.min(end, day.end);
    const startMinute = segStart === day.start ? 0 : zonedMinuteOfDay(segStart, timeZone);
    let endMinute = segEnd === day.end ? MINUTES_PER_DAY : zonedMinuteOfDay(segEnd, timeZone);
    // Fall-back DST hours can make the wall-clock end precede the start.
    endMinute = Math.min(MINUTES_PER_DAY, Math.max(endMinute, startMinute + MIN_BLOCK_MINUTES));
    segments.push({
      item,
      startMinute: Math.min(startMinute, MINUTES_PER_DAY - MIN_BLOCK_MINUTES),
      endMinute,
      continuesBefore: start < day.start,
      continuesAfter: end > day.end,
    });
  }

  segments.sort((a, b) => a.startMinute - b.startMinute || b.endMinute - a.endMinute);

  // Greedy lane packing within each cluster of mutually overlapping blocks.
  const laid: DaySegment<T>[] = [];
  let cluster: DaySegment<T>[] = [];
  let laneEnds: number[] = [];
  let clusterEnd = -1;
  const closeCluster = () => {
    for (const seg of cluster) seg.laneCount = laneEnds.length;
    laid.push(...cluster);
    cluster = [];
    laneEnds = [];
  };

  for (const seg of segments) {
    if (seg.startMinute >= clusterEnd) closeCluster();
    let lane = laneEnds.findIndex((laneEnd) => laneEnd <= seg.startMinute);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(seg.endMinute);
    } else {
      laneEnds[lane] = seg.endMinute;
    }
    cluster.push({ ...seg, lane, laneCount: 1 });
    clusterEnd = Math.max(clusterEnd, seg.endMinute);
  }
  closeCluster();
  return laid;
}

/**
 * Where the scrollable day should open: 8am, or a little before the earliest
 * booking that actually starts that day (a carry-over from the previous
 * night starts at midnight and shouldn't drag the view there).
 */
export function initialScrollMinute(
  segments: { startMinute: number; continuesBefore: boolean }[],
  defaultMinute = 8 * 60,
  leadMinutes = 30
): number {
  let earliest = defaultMinute;
  for (const seg of segments) {
    if (!seg.continuesBefore) earliest = Math.min(earliest, seg.startMinute);
  }
  return Math.max(0, earliest - (earliest < defaultMinute ? leadMinutes : 0));
}
