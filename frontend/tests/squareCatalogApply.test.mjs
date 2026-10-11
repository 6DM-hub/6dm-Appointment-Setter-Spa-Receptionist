import assert from "node:assert/strict";
import test from "node:test";

import {
  buildSquareCatalogApplyPlan,
  includeOmittedSquareCatalogResults,
} from "../src/pages/spa/squareCatalogApply.ts";

test("Square catalog apply omits skip choices without losing result counts", () => {
  const skippedItems = Array.from({ length: 1_000 }, (_, index) => ({
    square_variation_id: `skip-${index}`,
    action: index === 0 ? "conflict" : "unchanged",
  }));
  const items = [
    { square_variation_id: "add-1", action: "add" },
    { square_variation_id: "update-1", action: "update" },
    ...skippedItems,
  ];
  const plan = buildSquareCatalogApplyPlan(items, {
    "add-1": { action: "add", categoryId: "category-1" },
    "update-1": { action: "update", categoryId: "ignored-category" },
  });

  assert.deepEqual(plan.selections, [
    {
      square_variation_id: "add-1",
      action: "add",
      category_id: "category-1",
    },
    { square_variation_id: "update-1", action: "update" },
  ]);
  assert.equal(plan.omittedSkipped, 1_000);
  assert.equal(plan.omittedConflicts, 1);
  assert.deepEqual(
    includeOmittedSquareCatalogResults(
      { created: 1, updated: 1, skipped: 0, conflicts: 0 },
      plan,
    ),
    { created: 1, updated: 1, skipped: 1_000, conflicts: 1 },
  );
});
