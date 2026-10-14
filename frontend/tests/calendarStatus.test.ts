import { test } from "node:test";
import assert from "node:assert/strict";
import { calendarStatus } from "../src/lib/calendarStatus.ts";

test("a time-zone failure shows its error even though appointments never loaded", () => {
  // Appointments loading starts true and only clears after a fetch, which never
  // runs without a zone; the error must not hide behind a permanent spinner.
  assert.deepEqual(
    calendarStatus({ zoneStatus: "error", zoneError: "Bad zone", loading: true, error: null }),
    { loading: false, error: "Bad zone" }
  );
});

test("the spinner shows while the zone resolves", () => {
  assert.deepEqual(
    calendarStatus({ zoneStatus: "loading", zoneError: null, loading: false, error: null }),
    { loading: true, error: null }
  );
});

test("once the zone is ready, appointment loading and errors pass through", () => {
  assert.deepEqual(
    calendarStatus({ zoneStatus: "ready", zoneError: null, loading: true, error: null }),
    { loading: true, error: null }
  );
  assert.deepEqual(
    calendarStatus({ zoneStatus: "ready", zoneError: null, loading: false, error: "Down" }),
    { loading: false, error: "Down" }
  );
});
