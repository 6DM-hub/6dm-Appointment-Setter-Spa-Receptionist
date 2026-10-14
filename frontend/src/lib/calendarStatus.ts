/**
 * What the calendar panel should show: a spinner, an error, or the grid.
 * A time-zone failure wins over everything: without a usable zone the
 * appointments request never starts, so its loading flag never clears.
 */
export function calendarStatus({
  zoneStatus,
  zoneError,
  loading,
  error,
}: {
  zoneStatus: "loading" | "ready" | "error";
  zoneError: string | null;
  loading: boolean;
  error: string | null;
}): { loading: boolean; error: string | null } {
  if (zoneStatus === "error") {
    return { loading: false, error: zoneError ?? "Unable to load the spa's time zone." };
  }
  if (zoneStatus === "loading") return { loading: true, error: null };
  return { loading, error };
}
