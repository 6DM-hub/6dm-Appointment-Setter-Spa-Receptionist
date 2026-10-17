import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const calendar = readFileSync(new URL("../src/pages/CalendarView.tsx", import.meta.url), "utf8");
const analytics = readFileSync(new URL("../src/pages/AnalyticsView.tsx", import.meta.url), "utf8");
const client = readFileSync(new URL("../src/api/client.ts", import.meta.url), "utf8");

// Evaluate the actual display expressions without needing a browser or provider.
function calendarStatus(status) {
  const expression = calendar.match(/>\s*\{([^{}]*appointment\.status[^{}]*)\}\s*<\/span>/)[1];
  return vm.runInNewContext(expression, { appointment: { status } });
}

test("pending provider request is labelled Awaiting approval, never confirmed", () => {
  assert.equal(calendarStatus("awaiting_provider_approval"), "Awaiting approval");
  for (const status of ["scheduled", "confirmed", "completed", "cancelled", "no_show"]) {
    assert.equal(calendarStatus(status), status.replaceAll("_", " "));
  }
});

test("pending request has a distinct status style and typed API value", () => {
  const styles = vm.runInNewContext(`(${calendar.match(/const STATUS_STYLES[^=]*= (\{[\s\S]*?\});/)[1]})`);
  assert.ok(styles.awaiting_provider_approval, "pending request needs a visible status style");
  assert.notEqual(styles.awaiting_provider_approval, styles.confirmed);
  assert.match(client.match(/export type AppointmentStatus =([\s\S]*?);/)[1], /"awaiting_provider_approval"/);
});

test("analytics counts awaiting approval separately and supports older API responses", () => {
  const panel = analytics.split('<Panel title="Booking status">')[1];
  const expression = panel.match(/rows=\{(\[[\s\S]*?\])\}/)[1];
  const bookings = { scheduled: 2, confirmed: 3, completed: 4, cancelled: 1, no_show: 0 };
  for (const pending of [undefined, 5]) {
    const rows = vm.runInNewContext(expression, { data: { bookings: { ...bookings, awaiting_provider_approval: pending } } });
    assert.equal(rows.find(([label]) => label === "Awaiting approval")?.[1], pending ?? 0);
    assert.equal(rows.find(([label]) => label === "confirmed")[1], 3);
    assert.ok(rows.every(([, count]) => Number.isFinite(count)));
  }
});
