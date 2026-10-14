// Predictions against what happened: pure helpers for the Results page and the Track record chart.
// Hit/miss and averages are counted from published rows; probabilities are only read from the published curves.
import { isHit, probAt } from './curve'
import type { Period, Result } from './types'

/** A "70% call" is a match where the published chance of N+ shots was at least this. */
export const CALL_BAR = 0.7
/** Fewer settled calls than this and a point is drawn hollow and flagged as a small sample. */
export const SMALL_SAMPLE = 10

export interface Verdict {
  r: Result
  /** Published chance of n+ shots, or null when n is outside the published range. */
  p: number | null
  /** The match reached n+ shots. */
  hit: boolean
  /** The published chance was 70% or more (a "call"). */
  call: boolean
  /** The actual total reached the '80% sure' floor. */
  floorHeld: boolean
  /** actual total minus expected total (positive: more shots than expected). */
  error: number
}

export function verdict(r: Result, n: number, totalStart: number): Verdict {
  const p = probAt(r.curve, totalStart, n)
  return {
    r, p, hit: isHit(r.actual.total, n), call: p !== null && p >= CALL_BAR,
    floorHeld: r.actual.total >= r.lb['80'], error: r.actual.total - r.expected,
  }
}

export interface Recap {
  matches: number
  calls: number
  callHits: number
  reached: number
  floorHeld: number
  /** Mean of |actual - expected| in shots; null for an empty set. */
  mae: number | null
  /** Mean of (actual - expected); null for an empty set. */
  bias: number | null
}

export function recap(vs: Verdict[]): Recap {
  const calls = vs.filter((v) => v.call)
  return {
    matches: vs.length,
    calls: calls.length,
    callHits: calls.filter((v) => v.hit).length,
    reached: vs.filter((v) => v.hit).length,
    floorHeld: vs.filter((v) => v.floorHeld).length,
    mae: vs.length ? vs.reduce((a, v) => a + Math.abs(v.error), 0) / vs.length : null,
    bias: vs.length ? vs.reduce((a, v) => a + v.error, 0) / vs.length : null,
  }
}

export interface DayGroup { date: string; verdicts: Verdict[]; recap: Recap }

/** Finished matches grouped by day, newest day first; within a day, league then home team for a stable order. */
export function byDay(results: Result[], n: number, totalStart: number): DayGroup[] {
  const days = new Map<string, Verdict[]>()
  for (const r of results) {
    const list = days.get(r.date) ?? []
    list.push(verdict(r, n, totalStart))
    days.set(r.date, list)
  }
  return [...days.entries()]
    .sort((a, b) => b[0].localeCompare(a[0]))
    .map(([date, verdicts]) => {
      verdicts.sort((a, b) => a.r.league.localeCompare(b.r.league) || a.r.home.localeCompare(b.r.home))
      return { date, verdicts, recap: recap(verdicts) }
    })
}

/** Wilson 95% interval for hits out of picks. */
export function wilson(hits: number, picks: number, z = 1.96): [number, number] {
  if (picks <= 0) return [0, 1]
  const p = hits / picks
  const d = 1 + (z * z) / picks
  const c = (p + (z * z) / (2 * picks)) / d
  const h = (z * Math.sqrt((p * (1 - p)) / picks + (z * z) / (4 * picks * picks))) / d
  return [Math.max(0, c - h), Math.min(1, c + h)]
}

export interface WeekPoint {
  date: string
  matches: number
  mae: number | null
  bias: number | null
  floor80: number | null
  picks: number
  hits: number
  rate: number | null
  lo: number
  hi: number
  small: boolean
}

/** Weekly points (oldest first) for threshold n. `usedN` is the published threshold used (nearest to n; `exact` says if equal). */
export function weeklyPoints(weekly: Period[] | undefined, n: number): { points: WeekPoint[]; usedN: number | null; exact: boolean } {
  const rows = [...(weekly ?? [])].filter((w) => w && typeof w.date === 'string').sort((a, b) => a.date.localeCompare(b.date))
  const keys = new Set<number>()
  for (const w of rows) for (const k of Object.keys(w.by_n ?? {})) if (Number.isFinite(Number(k))) keys.add(Number(k))
  let usedN: number | null = null
  for (const k of [...keys].sort((a, b) => a - b)) if (usedN === null || Math.abs(k - n) < Math.abs(usedN - n)) usedN = k
  const points = rows.map((w) => {
    const e = usedN === null ? undefined : w.by_n?.[String(usedN)]
    const picks = e?.picks ?? 0
    const hits = e?.hits ?? 0
    const [lo, hi] = wilson(hits, picks)
    return {
      date: w.date, matches: w.matches, mae: w.mae ?? null, bias: w.bias ?? null, floor80: w.floor80_held ?? null,
      picks, hits, rate: picks > 0 ? hits / picks : null, lo, hi, small: picks < SMALL_SAMPLE,
    }
  })
  return { points, usedN, exact: usedN === n }
}
