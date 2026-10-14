import { test } from "node:test";
import assert from "node:assert/strict";
import {
  LOOKBACK_MS,
  MIN_BLOCK_MINUTES,
  calendarDay,
  fetchWindow,
  initialScrollMinute,
  layoutDay,
  weekDays,
} from "../src/lib/calendarLayout.ts";

const CHICAGO = "America/Chicago";
const oct14 = calendarDay({ year: 2026, month: 10, day: 14 }, CHICAGO);
const oct15 = calendarDay({ year: 2026, month: 10, day: 15 }, CHICAGO);

const appt = (id: string, start: string, end: string) => ({ id, start_time: start, end_time: end });

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

test("places a booking at its spa-local time", () => {
  const [seg] = layoutDay([appt("a", "2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z")], oct14, CHICAGO);
  assert.equal(seg.startMinute, 10 * 60);
  assert.equal(seg.endMinute, 11 * 60);
  assert.equal(seg.laneCount, 1);
});

test("a late-evening booking shows on the spa's day, not the UTC day", () => {
  const items = [appt("late", "2026-10-15T03:00:00Z", "2026-10-15T04:00:00Z")];
  assert.equal(layoutDay(items, oct14, CHICAGO).length, 1);
  assert.equal(layoutDay(items, oct15, CHICAGO).length, 0);
});

test("overlapping bookings sit side by side; later ones get the full width", () => {
  const segments = layoutDay(
    [
      appt("a", "2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z"),
      appt("b", "2026-10-14T15:30:00Z", "2026-10-14T16:30:00Z"),
      appt("c", "2026-10-14T18:00:00Z", "2026-10-14T19:00:00Z"),
    ],
    oct14,
    CHICAGO
  );
  const byId = Object.fromEntries(segments.map((s) => [s.item.id, s]));
  assert.deepEqual([byId.a.lane, byId.a.laneCount], [0, 2]);
  assert.deepEqual([byId.b.lane, byId.b.laneCount], [1, 2]);
  assert.deepEqual([byId.c.lane, byId.c.laneCount], [0, 1]);
});

test("back-to-back bookings do not count as overlapping", () => {
  const segments = layoutDay(
    [
      appt("a", "2026-10-14T15:00:00Z", "2026-10-14T16:00:00Z"),
      appt("b", "2026-10-14T16:00:00Z", "2026-10-14T17:00:00Z"),
    ],
    oct14,
    CHICAGO
  );
  assert.ok(segments.every((s) => s.lane === 0 && s.laneCount === 1));
});

test("a booking across local midnight is split between both days", () => {
  const items = [appt("x", "2026-10-15T04:00:00Z", "2026-10-15T06:00:00Z")]; // 11pm-1am CDT
  const [first] = layoutDay(items, oct14, CHICAGO);
  const [second] = layoutDay(items, oct15, CHICAGO);
  assert.deepEqual([first.startMinute, first.endMinute, first.continuesAfter], [23 * 60, 24 * 60, true]);
  assert.deepEqual([second.startMinute, second.endMinute, second.continuesBefore], [0, 60, true]);
});

test("zero-length and malformed rows are handled without crashing", () => {
  const segments = layoutDay(
    [
      appt("zero", "2026-10-14T15:00:00Z", "2026-10-14T15:00:00Z"),
      appt("bad", "not a date", "2026-10-14T15:00:00Z"),
    ],
    oct14,
    CHICAGO
  );
  assert.equal(segments.length, 1);
  assert.equal(segments[0].endMinute - segments[0].startMinute, MIN_BLOCK_MINUTES);
});

test("the day opens at 8am, or just before an earlier booking", () => {
  assert.equal(initialScrollMinute([]), 8 * 60);
  assert.equal(initialScrollMinute([{ startMinute: 10 * 60, continuesBefore: false }]), 8 * 60);
  assert.equal(initialScrollMinute([{ startMinute: 7 * 60, continuesBefore: false }]), 6 * 60 + 30);
  assert.equal(initialScrollMinute([{ startMinute: 10, continuesBefore: false }]), 0);
});

test("an overnight carry-over does not pull the view to midnight", () => {
  assert.equal(initialScrollMinute([{ startMinute: 0, continuesBefore: true }]), 8 * 60);
});
