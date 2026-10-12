import type { TrackNEntry, TrackRecord } from './types'

/** The published entry for threshold n, or the nearest published one (flagged as not exact). */
export function entryFor(rec: TrackRecord | undefined, n: number): { n: number; entry: TrackNEntry; exact: boolean } | null {
  if (!rec) return null
  const keys = Object.keys(rec.by_n).map(Number).filter((k) => Number.isFinite(k))
  if (keys.length === 0) return null
  const best = keys.reduce((a, b) => (Math.abs(b - n) < Math.abs(a - n) || (Math.abs(b - n) === Math.abs(a - n) && b < a) ? b : a))
  return { n: best, entry: rec.by_n[String(best)], exact: best === n }
}

export function pc(x: number, digits = 0): string {
  return `${(x * 100).toFixed(digits)}%`
}
