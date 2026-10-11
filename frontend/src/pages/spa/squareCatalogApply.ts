import type {
  SquareCatalogApplyAction,
  SquareCatalogApplyResult,
  SquareCatalogPreviewItem,
  SquareCatalogSelection,
} from "../../api/client";

export type SquareCatalogChoice = {
  action: SquareCatalogApplyAction;
  categoryId: string;
};

export type SquareCatalogApplyPlan = {
  selections: SquareCatalogSelection[];
  omittedSkipped: number;
  omittedConflicts: number;
};

export const buildSquareCatalogApplyPlan = (
  items: SquareCatalogPreviewItem[],
  choices: Record<string, SquareCatalogChoice>,
): SquareCatalogApplyPlan => {
  const selections: SquareCatalogSelection[] = [];
  let omittedSkipped = 0;
  let omittedConflicts = 0;

  items.forEach((item) => {
    const choice = choices[item.square_variation_id];
    if (!choice || choice.action === "skip") {
      omittedSkipped += 1;
      if (item.action === "conflict") omittedConflicts += 1;
      return;
    }

    selections.push({
      square_variation_id: item.square_variation_id,
      action: choice.action,
      ...(choice.action === "add" && choice.categoryId
        ? { category_id: choice.categoryId }
        : {}),
    });
  });

  return { selections, omittedSkipped, omittedConflicts };
};

export const includeOmittedSquareCatalogResults = (
  result: SquareCatalogApplyResult,
  plan: Pick<SquareCatalogApplyPlan, "omittedSkipped" | "omittedConflicts">,
): SquareCatalogApplyResult => ({
  ...result,
  skipped: result.skipped + plan.omittedSkipped,
  conflicts: result.conflicts + plan.omittedConflicts,
});
