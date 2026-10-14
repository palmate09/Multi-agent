/**
 * Date and time formatting, in one place.
 *
 * These were duplicated in `RunList` and `PipelineTimeline` with two
 * different formats, which is how a console ends up showing "Oct 13 19:16"
 * in one column and "19:16:07" in another. `formatDateTime` is for a run's
 * timestamp (date and time, because a run list spans days); `formatClock` is
 * for an event within one run (time only, because the run's own duration is
 * already on screen).
 *
 * Both are locale-formatted rather than hand-rolled, and both fall back to the
 * raw value rather than rendering "Invalid Date": a run with an unparseable
 * timestamp should still show its status.
 */

function parse(iso: string): Date | null {
  try {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? null : d;
  } catch {
    return null;
  }
}

/** "13 Oct 19:16" — sortable, compact, unambiguous across locales. */
export function formatDateTime(iso: string): string {
  const d = parse(iso);
  if (!d) return iso;
  const date = d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
  const time = d.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  return `${date} ${time}`;
}

/** "19:16:07" — within a single run, so the date is noise. */
export function formatClock(iso: string): string {
  const d = parse(iso);
  if (!d) return iso;
  return d.toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}
