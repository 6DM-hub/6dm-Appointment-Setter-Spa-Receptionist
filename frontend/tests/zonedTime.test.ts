import { test } from "node:test";
import assert from "node:assert/strict";
import {
  addCivilDays,
  isValidTimeZone,
  startOfZonedDay,
  zonedCivilDate,
  zonedMinuteOfDay,
  zonedParts,
} from "../src/lib/zonedTime.ts";

const HOUR = 3_600_000;
const CHICAGO = "America/Chicago";

test("reads wall-clock parts in the spa zone, not the machine's", () => {
  const instant = Date.parse("2026-10-14T15:30:00Z");
  assert.deepEqual(
    { ...zonedParts(instant, CHICAGO) },
    { year: 2026, month: 10, day: 14, hour: 10, minute: 30, second: 0, weekday: 3 }
  );
  assert.equal(zonedParts(instant, "Asia/Tokyo").hour, 0);
  assert.equal(zonedParts(instant, "Asia/Tokyo").day, 15);
});

test("an evening UTC instant belongs to the previous spa-local day", () => {
  // 03:00 UTC on the 15th is 10pm on the 14th in Chicago.
  assert.deepEqual(zonedCivilDate(Date.parse("2026-10-15T03:00:00Z"), CHICAGO), {
    year: 2026,
    month: 10,
    day: 14,
  });
});

test("day boundaries follow DST: 23h in spring, 25h in fall", () => {
  const spring = { year: 2026, month: 3, day: 8 };
  assert.equal(startOfZonedDay(spring, CHICAGO), Date.parse("2026-03-08T06:00:00Z"));
  assert.equal(
    startOfZonedDay(addCivilDays(spring, 1), CHICAGO) - startOfZonedDay(spring, CHICAGO),
    23 * HOUR
  );
  const fall = { year: 2026, month: 11, day: 1 };
  assert.equal(startOfZonedDay(fall, CHICAGO), Date.parse("2026-11-01T05:00:00Z"));
  assert.equal(
    startOfZonedDay(addCivilDays(fall, 1), CHICAGO) - startOfZonedDay(fall, CHICAGO),
    25 * HOUR
  );
});

test("start of day is the first instant of that date even when midnight is skipped", () => {
  // Chile springs forward at midnight, so 00:00 does not exist on that date.
  const zone = "America/Santiago";
  for (let offset = 0; offset < 366; offset++) {
    const date = addCivilDays({ year: 2026, month: 1, day: 1 }, offset);
    const start = startOfZonedDay(date, zone);
    assert.deepEqual(zonedCivilDate(start, zone), date, `start of ${JSON.stringify(date)}`);
    assert.notDeepEqual(zonedCivilDate(start - 1, zone), date);
  }
});

test("civil day arithmetic crosses month and year ends", () => {
  assert.deepEqual(addCivilDays({ year: 2026, month: 12, day: 30 }, 3), {
    year: 2027,
    month: 1,
    day: 2,
  });
  assert.deepEqual(addCivilDays({ year: 2028, month: 3, day: 1 }, -1), {
    year: 2028,
    month: 2,
    day: 29,
  });
});

test("minute of day uses the spa's wall clock", () => {
  assert.equal(zonedMinuteOfDay(Date.parse("2026-10-14T15:30:00Z"), CHICAGO), 10 * 60 + 30);
});

test("rejects unknown or missing zones instead of guessing", () => {
  assert.equal(isValidTimeZone(CHICAGO), true);
  assert.equal(isValidTimeZone("Mars/Olympus_Mons"), false);
  assert.equal(isValidTimeZone(""), false);
  assert.equal(isValidTimeZone(null), false);
});
