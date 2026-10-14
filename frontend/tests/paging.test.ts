import { test } from "node:test";
import assert from "node:assert/strict";
import { collectAllPages, type PageResult } from "../src/lib/paging.ts";

type Row = { id: string };
const rows = (n: number, prefix = "r") => Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i}` }));

function pager(all: Row[]) {
  const requested: number[] = [];
  const fetchPage = async (page: number, size: number): Promise<PageResult<Row>> => {
    requested.push(page);
    return { items: all.slice((page - 1) * size, page * size), total: all.length, page, size };
  };
  return { fetchPage, requested };
}

test("reads every page instead of stopping at the first", async () => {
  const { fetchPage, requested } = pager(rows(250));
  const result = await collectAllPages(fetchPage, { size: 100 });
  assert.equal(result.items.length, 250);
  assert.equal(result.total, 250);
  assert.equal(result.complete, true);
  assert.deepEqual(requested, [1, 2, 3]);
});

test("an exact multiple of the page size needs no extra request", async () => {
  const { fetchPage, requested } = pager(rows(200));
  const result = await collectAllPages(fetchPage, { size: 100 });
  assert.equal(result.complete, true);
  assert.deepEqual(requested, [1, 2]);
});

test("an empty range is complete", async () => {
  const result = await collectAllPages(pager([]).fetchPage);
  assert.deepEqual(result, { items: [], total: 0, complete: true });
});

test("rows shifted between pages are de-duplicated and flagged incomplete", async () => {
  // A row inserted mid-read pushes r1 onto page 2 as well, and r3 is never served.
  const pages: Record<number, Row[]> = { 1: [{ id: "r0" }, { id: "r1" }], 2: [{ id: "r1" }, { id: "r2" }] };
  const result = await collectAllPages(
    async (page, size) => ({ items: pages[page] ?? [], total: 4, page, size }),
    { size: 2 }
  );
  assert.deepEqual(result.items.map((r) => r.id), ["r0", "r1", "r2"]);
  assert.equal(result.complete, false);
});

test("hitting the page cap is reported, not silently truncated", async () => {
  const result = await collectAllPages(pager(rows(500)).fetchPage, { size: 100, maxPages: 2 });
  assert.equal(result.items.length, 200);
  assert.equal(result.total, 500);
  assert.equal(result.complete, false);
});

test("a failed page rejects instead of returning partial data", async () => {
  await assert.rejects(
    collectAllPages(async (page, size) => {
      if (page === 2) throw new Error("network down");
      return { items: rows(size), total: 300, page, size };
    }),
    /network down/
  );
});
