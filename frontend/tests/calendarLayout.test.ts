import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LOOKBACK_MS,
  MIN_BLOCK_SLOTS,
  axisPosition,
  buildWeekGrid,
  fetchWindow,
  initialScrollSlot,
  layoutDay,
  weekDays,
} from "../src/lib/calendarLayout.ts";
import { zonedMinuteOfDay } from "../src/lib/zonedTime.ts";

const CHICAGO = "America/Chicago";
const HOUR_SLOTS = 4;

const appt = (id: string, start: string, end: string) => ({ id, start_time: start, end_time: end });

/** Week containing `day` (2026-MM-DD) and that day's column. */
function gridFor(month: number, day: number, zone = CHICAGO) {
  const days = weekDays({ year: 2026, month, day }, zone);
  const grid = buildWeekGrid(days, zone);
  const column = grid.columns.find((c) => c.date.month === month && c.date.day === day)!;
  return { days, grid, column };
}

test("week runs Sunday to Saturday around the anchor, with contiguous days", () => {
  const days = weekDays({ year: 2026, month: 10, day: 14 }, CHICAGO);
  assert.deepEqual(
    days.map((d) => d.key),
    ["2026-10-11", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17"]
  );
  for (let i = 1; i < days.length; i++) assert.equal(days[i].start, days[i - 1].end);
});

test("a week anchored on Sunday or Saturday stays in that week", () => {
  assert.equal(weekDays({ year: 2026, month: 10, day: 11 }, CHICAGO)[0].key, "2026-10-11");
  assert.equal(weekDays({ year: 2026, month: 10, day: 17 }, CHICAGO)[0].key, "2026-10-11");
});

test("fetch window covers the week, looks back for overnight bookings, and stops before next week", () => {
  const days = weekDays({ year: 2026, month: 10, day: 14 }, CHICAGO);
  const { from, to } = fetchWindow(days);
  assert.equal(from, days[0].start - LOOKBACK_MS);
  assert.equal(to, days[6].end - 1);
  assert.equal(new Date(days[6].end).toISOString(), "2026-10-18T05:00:00.000Z");
});

test("an ordinary week has a plain 24-hour axis with no gaps", () => {
  const { grid } = gridFor(10, 14);
  assert.equal(grid.slots.length, 24 * HOUR_SLOTS);
  assert.equal(grid.hasRepeatedTime, false);
  assert.ok(grid.columns.every((c) => c.missing.length === 0));
});

test("places a booking at its spa-local time", () => {
  const { grid, column } = gridFor(10, 14);
  const [seg] = layoutDay([appt("a", "2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z")], column, grid.slots);
  assert.equal(seg.startSlot, 10 * HOUR_SLOTS);
  assert.equal(seg.endSlot, 11 * HOUR_SLOTS);
  assert.equal(seg.laneCount, 1);
  assert.equal(seg.ambiguous, false);
});

test("a late-evening booking shows on the spa's day, not the UTC day", () => {
  const { grid } = gridFor(10, 14);
  const [oct14, oct15] = [grid.columns[3], grid.columns[4]];
  const items = [appt("late", "2026-10-15T03:00:00Z", "2026-10-15T04:00:00Z")];
  assert.equal(layoutDay(items, oct14, grid.slots).length, 1);
  assert.equal(layoutDay(items, oct15, grid.slots).length, 0);
});

test("overlapping bookings sit side by side; later ones get the full width", () => {
  const { grid, column } = gridFor(10, 14);
  const segments = layoutDay(
    [
      appt("a", "2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z"),
      appt("b", "2026-10-14T15:30:00Z", "2026-10-14T16:30:00Z"),
      appt("c", "2026-10-14T18:00:00Z", "2026-10-14T19:00:00Z"),
    ],
    column,
    grid.slots
  );
  const byId = Object.fromEntries(segments.map((s) => [s.item.id, s]));
  assert.deepEqual([byId.a.lane, byId.a.laneCount], [0, 2]);
  assert.deepEqual([byId.b.lane, byId.b.laneCount], [1, 2]);
  assert.deepEqual([byId.c.lane, byId.c.laneCount], [0, 1]);
});

test("back-to-back bookings do not count as overlapping", () => {
  const { grid, column } = gridFor(10, 14);
  const segments = layoutDay(
    [
      appt("a", "2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z"),
      appt("b", "2026-10-14T16:00:00Z", "2026-10-14T17:00:00Z"),
    ],
    column,
    grid.slots
  );
  assert.ok(segments.every((s) => s.lane === 0 && s.laneCount === 1));
});

test("a booking across local midnight is split between both days", () => {
  const { grid } = gridFor(10, 14);
  const items = [appt("x", "2026-10-15T04:00:00Z", "2026-10-15T06:00:00Z")]; // 11pm-1am CDT
  const [first] = layoutDay(items, grid.columns[3], grid.slots);
  const [second] = layoutDay(items, grid.columns[4], grid.slots);
  assert.deepEqual(
    [first.startSlot, first.endSlot, first.continuesAfter],
    [23 * HOUR_SLOTS, 24 * HOUR_SLOTS, true]
  );
  assert.deepEqual(
    [second.startSlot, second.endSlot, second.continuesBefore],
    [0, 1 * HOUR_SLOTS, true]
  );
});

test("zero-length and malformed rows are handled without crashing", () => {
  const { grid, column } = gridFor(10, 14);
  const segments = layoutDay(
    [
      appt("zero", "2026-10-14T15:00:00Z", "2026-10-14T15:00:00Z"),
      appt("bad", "not a date", "2026-10-14T15:00:00Z"),
    ],
    column,
    grid.slots
  );
  assert.equal(segments.length, 1);
  assert.equal(segments[0].endSlot - segments[0].startSlot, MIN_BLOCK_SLOTS);
});

// --- DST: fall back (Sun Nov 1 2026, 2:00 CDT -> 1:00 CST) ---

test("fall-back week gets a second 1 AM hour on the axis, after the first", () => {
  const { grid } = gridFor(11, 1);
  assert.equal(grid.slots.length, 25 * HOUR_SLOTS);
  assert.equal(grid.hasRepeatedTime, true);
  const oneAm = grid.slots
    .map((s, i) => ({ ...s, i }))
    .filter((s) => s.wallMinute === 60);
  assert.deepEqual(oneAm.map((s) => [s.i, s.occurrence]), [[4, 0], [8, 1]]);
  assert.equal(oneAm[1].sample - oneAm[0].sample, 3_600_000);
  assert.equal(grid.slots[12].wallMinute, 120); // 2 AM follows the repeated hour
});

test("the two 1:15 AMs on Nov 1 are placed an hour apart, not side by side", () => {
  const { grid, column } = gridFor(11, 1);
  const segments = layoutDay(
    [
      appt("cdt", "2026-11-01T06:15:00Z", "2026-11-01T06:45:00Z"), // 1:15 AM CDT
      appt("cst", "2026-11-01T07:15:00Z", "2026-11-01T07:45:00Z"), // 1:15 AM CST
    ],
    column,
    grid.slots
  );
  const byId = Object.fromEntries(segments.map((s) => [s.item.id, s]));
  assert.equal(byId.cdt.startSlot, 5);
  assert.equal(byId.cst.startSlot, 9);
  assert.equal(byId.cst.startSlot - byId.cdt.startSlot, HOUR_SLOTS);
  assert.ok(segments.every((s) => s.laneCount === 1 && s.lane === 0));
  assert.ok(segments.every((s) => s.ambiguous));
});

test("a booking spanning the fall-back change is drawn at its true 2-hour length", () => {
  const { grid, column } = gridFor(11, 1);
  // 12:30 AM CDT to 1:30 AM CST is two real hours.
  const [seg] = layoutDay([appt("x", "2026-11-01T05:30:00Z", "2026-11-01T07:30:00Z")], column, grid.slots);
  assert.equal(seg.endSlot - seg.startSlot, 2 * HOUR_SLOTS);
});

test("other days in the fall-back week show the extra hour as a gap", () => {
  const { grid } = gridFor(11, 1);
  assert.deepEqual(grid.columns[0].missing, []);
  for (const column of grid.columns.slice(1)) assert.deepEqual(column.missing, [[8, 12]]);
  // 2 AM on Monday lines up with 2 AM on Sunday.
  const monday = grid.columns[1];
  assert.equal(axisPosition(monday, Date.parse("2026-11-02T08:00:00Z")), 12);
});

// --- DST: spring forward (Sun Mar 8 2026, 2:00 CST -> 3:00 CDT) ---

test("spring-forward day keeps the shared axis and marks 2 AM as skipped", () => {
  const { grid, column } = gridFor(3, 8);
  assert.equal(grid.slots.length, 24 * HOUR_SLOTS);
  assert.equal(grid.hasRepeatedTime, false);
  assert.deepEqual(column.missing, [[8, 12]]);
  for (const other of grid.columns.filter((c) => c !== column)) assert.deepEqual(other.missing, []);
});

test("bookings after the spring-forward jump line up with the same wall time on other days", () => {
  const { grid, column } = gridFor(3, 8);
  const [seg] = layoutDay([appt("x", "2026-03-08T09:00:00Z", "2026-03-08T10:00:00Z")], column, grid.slots);
  assert.equal(seg.startSlot, 4 * HOUR_SLOTS); // 4:00 AM CDT
  assert.equal(seg.endSlot, 5 * HOUR_SLOTS);
});

test("a booking ending exactly at the jump does not stretch over the skipped hour", () => {
  const { grid, column } = gridFor(3, 8);
  // 1:00-2:00 AM CST ends at the instant the clock reads 3:00 AM CDT.
  const [seg] = layoutDay([appt("x", "2026-03-08T07:00:00Z", "2026-03-08T08:00:00Z")], column, grid.slots);
  assert.deepEqual([seg.startSlot, seg.endSlot], [4, 8]);
});

test("every week of the year keeps each day in order and on its own wall clock", () => {
  // Chicago, Chile (DST at midnight), Lord Howe (30-minute DST), and no-DST zones.
  for (const zone of [CHICAGO, "America/Santiago", "Australia/Lord_Howe", "Asia/Kolkata", "UTC"]) {
    for (let week = 0; week < 53; week++) {
      const days = weekDays({ year: 2026, month: 1, day: 1 + week * 7 }, zone);
      // Full check on every week with a clock change, plus a sample of ordinary weeks.
      const hasChange = days.some((d) => d.end - d.start !== 24 * 3_600_000);
      if (!hasChange && week % 13 !== 0) continue;
      const grid = buildWeekGrid(days, zone);
      for (const column of grid.columns) {
        const where = `${zone} ${column.key}`;
        column.slotIndexes.forEach((axisIndex, k) => {
          if (k > 0) assert.ok(axisIndex > column.slotIndexes[k - 1], `order ${where}`);
          const instant = column.start + k * 15 * 60_000;
          const wall = Math.floor(zonedMinuteOfDay(instant, zone) / 15) * 15;
          assert.equal(grid.slots[axisIndex].wallMinute, wall, `wall ${where} slot ${k}`);
        });
        const covered = column.slotIndexes.length +
          column.missing.reduce((n, [a, b]) => n + b - a, 0);
        assert.equal(covered, grid.slots.length, `coverage ${where}`);
      }
    }
  }
});

// --- scroll position ---

test("the day opens at 8am, or just before an earlier booking", () => {
  const { grid } = gridFor(10, 14);
  assert.equal(initialScrollSlot(grid.slots, []), 32);
  assert.equal(initialScrollSlot(grid.slots, [{ startSlot: 40, continuesBefore: false }]), 32);
  assert.equal(initialScrollSlot(grid.slots, [{ startSlot: 28, continuesBefore: false }]), 26);
  assert.equal(initialScrollSlot(grid.slots, [{ startSlot: 1, continuesBefore: false }]), 0);
});

test("an overnight carry-over does not pull the view to midnight", () => {
  const { grid } = gridFor(10, 14);
  assert.equal(initialScrollSlot(grid.slots, [{ startSlot: 0, continuesBefore: true }]), 32);
});

test("8am is found correctly in a fall-back week with the extra hour", () => {
  const { grid } = gridFor(11, 1);
  assert.equal(grid.slots[initialScrollSlot(grid.slots, [])].wallMinute, 8 * 60);
});
