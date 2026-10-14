/** Shape of the API's `Page[T]` envelope (`backend/app/schemas/common.py`). */
export interface PageResult<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
}

export interface CollectedPages<T> {
  items: T[];
  /** `total` the API reported on the last page read. */
  total: number;
  /** False when rows may be missing: page cap hit, or offset paging skipped
   *  rows because the data changed between requests. Never silently trust. */
  complete: boolean;
}

/**
 * Read every page of an offset-paginated list. Rows are de-duplicated by id,
 * and the result is only marked complete when the number of distinct rows
 * reaches the reported total.
 */
export async function collectAllPages<T extends { id: string }>(
  fetchPage: (page: number, size: number) => Promise<PageResult<T>>,
  { size = 100, maxPages = 50 }: { size?: number; maxPages?: number } = {}
): Promise<CollectedPages<T>> {
  const byId = new Map<string, T>();
  let total = 0;
  let finished = false;

  for (let page = 1; page <= maxPages; page++) {
    const result = await fetchPage(page, size);
    total = result.total;
    for (const item of result.items) byId.set(item.id, item);
    if (result.items.length < size || page * size >= result.total) {
      finished = true;
      break;
    }
  }

  return {
    items: [...byId.values()],
    total,
    complete: finished && byId.size >= total,
  };
}
