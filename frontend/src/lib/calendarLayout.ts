/**
 * Pure layout math for the week calendar: which spa-local days are shown,
 * which UTC window to request, and where each appointment sits in a day
 * column. No React here so it can be unit tested under plain Node.
 *
 * The vertical axis is a sequence of 15-minute wall-clock slots shared by
 * every column in the week. A slot is identified by its wall-clock time AND
 * which occurrence of that time it is, so on a fall-back day the repeated
 * hour (1:00-1:59 CDT, then 1:00-1:59 CST) gets its own rows instead of
 * drawing two different instants in the same place. A spring-forward day
 * simply has no instants in the skipped slots; they are reported as gaps.
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
export const SLOT_MINUTES = 15;
const SLOT_MS = SLOT_MINUTES * 60_000;
/** Shortest block drawn, in slots, so a zero-length booking stays visible. */
export const MIN_BLOCK_SLOTS = 1;
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

export interface GridSlot {
  /** Wall-clock minutes past midnight at the start of the slot. */
  wallMinute: number;
  /** 0 normally; 1 for the second pass through a repeated (fall-back) time. */
  occurrence: number;
  /** One real instant (UTC ms) at the start of this slot, for labelling its offset. */
  sample: number;
}

export interface DayColumn extends CalendarDay {
  /** Axis slot index for each consecutive 15 minutes of this day. */
  slotIndexes: number[];
  /** Axis ranges [from, to) that don't exist on this day (DST gaps or another day's repeated hour). */
  missing: Array<[number, number]>;
  /** Wall-clock minutes that occur twice on this day. */
  repeatedWallMinutes: Set<number>;
}

export interface WeekGrid {
  slots: GridSlot[];
  columns: DayColumn[];
  /** True when some wall-clock time appears twice in the week (fall-back). */
  hasRepeatedTime: boolean;
}

export interface DaySegment<T> {
  item: T;
  /** Position on the axis, in slots (fractional). */
  startSlot: number;
  endSlot: number;
  continuesBefore: boolean;
  continuesAfter: boolean;
  /** Start or end falls in a wall-clock time this day has twice; show the offset. */
  ambiguous: boolean;
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

const slotId = (wallMinute: number, occurrence: number) => `${wallMinute}#${occurrence}`;

/**
 * Build the shared axis for `days`. Each day contributes its own ordered run
 * of slots; the runs are merged so that every slot keeps its order within
 * every day it appears in.
 */
export function buildWeekGrid(days: CalendarDay[], timeZone: string): WeekGrid {
  const perDay = days.map((day) => {
    const seen = new Map<number, number>();
    const ids: string[] = [];
    const repeated = new Set<number>();
    const samples: number[] = [];
    for (let instant = day.start; instant < day.end; instant += SLOT_MS) {
      const wall =
        Math.floor(zonedMinuteOfDay(instant, timeZone) / SLOT_MINUTES) * SLOT_MINUTES;
      const occurrence = seen.get(wall) ?? 0;
      seen.set(wall, occurrence + 1);
      if (occurrence > 0) repeated.add(wall);
      ids.push(slotId(wall, occurrence));
      samples.push(instant);
    }
    return { ids, repeated, samples };
  });

  // Merge each day's run into the axis. A slot already on the axis is matched
  // in order; a new one is inserted just before the next axis slot this day
  // doesn't skip to.
  const axis: string[] = [];
  const meta = new Map<string, GridSlot>();
  for (const { ids, samples } of perDay) {
    let i = 0;
    ids.forEach((id, j) => {
      if (!meta.has(id)) {
        const [wallMinute, occurrence] = id.split("#").map(Number);
        meta.set(id, { wallMinute, occurrence, sample: samples[j] });
        axis.splice(i, 0, id);
        i += 1;
        return;
      }
      i = axis.indexOf(id, i) + 1;
    });
  }

  const index = new Map(axis.map((id, i) => [id, i]));
  const columns = days.map((day, d) => {
    const slotIndexes = perDay[d].ids.map((id) => index.get(id)!);
    const present = new Set(slotIndexes);
    const missing: Array<[number, number]> = [];
    for (let i = 0; i < axis.length; i++) {
      if (present.has(i)) continue;
      const last = missing[missing.length - 1];
      if (last && last[1] === i) last[1] = i + 1;
      else missing.push([i, i + 1]);
    }
    return { ...day, slotIndexes, missing, repeatedWallMinutes: perDay[d].repeated };
  });

  return {
    slots: axis.map((id) => meta.get(id)!),
    columns,
    hasRepeatedTime: perDay.some((p) => p.repeated.size > 0),
  };
}

/**
 * Where `instant` falls on the axis within `column`, in slots. Positions come
 * from elapsed time since the day began, so two instants with the same
 * wall-clock reading on a fall-back day land in different rows. An end that
 * falls exactly on a slot boundary closes the previous slot, so a block never
 * stretches across a gap it doesn't occupy.
 */
export function axisPosition(column: DayColumn, instant: number, isEnd = false): number {
  const count = column.slotIndexes.length;
  const elapsed = Math.min(Math.max((instant - column.start) / SLOT_MS, 0), count);
  const slot = Math.floor(elapsed);
  if (slot >= count || (isEnd && slot > 0 && slot === elapsed)) {
    return column.slotIndexes[slot - 1] + 1;
  }
  return column.slotIndexes[slot] + (elapsed - slot);
}

/** Appointments overlapping `column`'s day, clipped to it and assigned side-by-side lanes. */
export function layoutDay<T extends TimedItem>(
  items: T[],
  column: DayColumn,
  slots: GridSlot[]
): DaySegment<T>[] {
  const segments: Omit<DaySegment<T>, "lane" | "laneCount">[] = [];
  const isRepeated = (position: number) => {
    const slot = slots[Math.min(Math.floor(position), slots.length - 1)];
    return !!slot && column.repeatedWallMinutes.has(slot.wallMinute);
  };

  for (const item of items) {
    const start = Date.parse(item.start_time);
    let end = Date.parse(item.end_time);
    if (Number.isNaN(start) || Number.isNaN(end)) continue;
    if (end < start) end = start;
    const overlaps =
      start < column.end && (end > column.start || (end === start && start >= column.start));
    if (!overlaps) continue;

    const startSlot = Math.min(
      axisPosition(column, Math.max(start, column.start)),
      slots.length - MIN_BLOCK_SLOTS
    );
    const rawEnd = axisPosition(column, Math.min(end, column.end), true);
    const endSlot = Math.min(slots.length, Math.max(rawEnd, startSlot + MIN_BLOCK_SLOTS));
    segments.push({
      item,
      startSlot,
      endSlot,
      continuesBefore: start < column.start,
      continuesAfter: end > column.end,
      ambiguous: isRepeated(startSlot) || isRepeated(Math.max(startSlot, rawEnd - 0.001)),
    });
  }

  segments.sort((a, b) => a.startSlot - b.startSlot || b.endSlot - a.endSlot);

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
    if (seg.startSlot >= clusterEnd) closeCluster();
    let lane = laneEnds.findIndex((laneEnd) => laneEnd <= seg.startSlot);
    if (lane === -1) {
      lane = laneEnds.length;
      laneEnds.push(seg.endSlot);
    } else {
      laneEnds[lane] = seg.endSlot;
    }
    cluster.push({ ...seg, lane, laneCount: 1 });
    clusterEnd = Math.max(clusterEnd, seg.endSlot);
  }
  closeCluster();
  return laid;
}

/**
 * Axis slot the scrollable day should open at: 8am, or a little before the
 * earliest booking that actually starts that day (a carry-over from the
 * previous night starts at midnight and shouldn't drag the view there).
 */
export function initialScrollSlot(
  slots: GridSlot[],
  segments: { startSlot: number; continuesBefore: boolean }[],
  defaultMinute = 8 * 60,
  leadSlots = 2
): number {
  const found = slots.findIndex((s) => s.occurrence === 0 && s.wallMinute >= defaultMinute);
  const defaultSlot = found === -1 ? 0 : found;
  let earliest = defaultSlot;
  for (const seg of segments) {
    if (!seg.continuesBefore) earliest = Math.min(earliest, seg.startSlot);
  }
  return Math.max(0, Math.floor(earliest) - (earliest < defaultSlot ? leadSlots : 0));
}
