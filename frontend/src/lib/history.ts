// Growth of a record over time (track.json daily / weekly points) and the model milestones (meta.model_history).
import { wilson } from './recap'
import type { ModelMilestone, Period, TrackRecord } from './types'

export interface CumPoint {
  date: string
  /** 70%+ calls settled in this period, and how many hit. */
  picks: number
  hits: number
  matches: number
  cumPicks: number
  cumHits: number
  /** Cumulative hit rate (null until the first call) and its Wilson 95% range. */
  rate: number | null
  lo: number
  hi: number
}

const sumPicks = (rows: Period[] | undefined, n: number) =>
  (rows ?? []).reduce((a, r) => a + (r.by_n?.[String(n)]?.picks ?? 0), 0)

/** The published threshold nearest to n among the record's periods (null when none). */
export function periodN(rec: TrackRecord, n: number): number | null {
  const keys = new Set<number>()
  for (const r of [...(rec.daily ?? []), ...(rec.weekly ?? [])]) for (const k of Object.keys(r.by_n ?? {})) if (Number.isFinite(Number(k))) keys.add(Number(k))
  let best: number | null = null
  for (const k of [...keys].sort((a, b) => a - b)) if (best === null || Math.abs(k - n) < Math.abs(best - n)) best = k
  return best
}

/**
 * Cumulative series of the record at threshold n. Daily points are used when they cover every settled call
 * (the export keeps the latest 30 days only); otherwise weekly points, which go back a year.
 */
export function cumulative(rec: TrackRecord | undefined, n: number): { points: CumPoint[]; grain: 'day' | 'week'; usedN: number | null } {
  if (!rec) return { points: [], grain: 'day', usedN: null }
  const usedN = periodN(rec, n)
  if (usedN === null) return { points: [], grain: 'day', usedN }
  const daily = rec.daily ?? []
  const weekly = rec.weekly ?? []
  const useDaily = daily.length > 0 && (weekly.length === 0 || sumPicks(daily, usedN) === sumPicks(weekly, usedN))
  const rows = [...(useDaily ? daily : weekly)].filter((r) => r && typeof r.date === 'string').sort((a, b) => a.date.localeCompare(b.date))
  let cp = 0, ch = 0
  const points = rows.map((r) => {
    const e = r.by_n?.[String(usedN)]
    const picks = e?.picks ?? 0
    const hits = e?.hits ?? 0
    cp += picks
    ch += hits
    const [lo, hi] = wilson(ch, cp)
    return { date: r.date, picks, hits, matches: r.matches, cumPicks: cp, cumHits: ch, rate: cp > 0 ? ch / cp : null, lo, hi }
  })
  return { points, grain: useDaily ? 'day' : 'week', usedN }
}

export const ROUTE_LABEL: Record<ModelMilestone['route'], string> = {
  all: 'All routes', odds: 'Odds route', noodds: 'No-odds route', cross_border: 'European cups',
}

/** Milestones relevant to a record: the official record skips the European-cup ones and vice versa. */
export function milestonesFor(history: ModelMilestone[] | undefined, kind: 'live' | 'experimental'): ModelMilestone[] {
  const ok = (m: ModelMilestone) => (kind === 'live' ? m.route !== 'cross_border' : m.route === 'all' || m.route === 'cross_border')
  return [...(history ?? [])].filter((m) => m && typeof m.date === 'string' && ok(m)).sort((a, b) => a.date.localeCompare(b.date))
}

/** Day number (UTC) of an ISO date, for linear time scales. */
export const dayIndex = (iso: string): number => Math.round(Date.parse(`${iso}T00:00:00Z`) / 86_400_000)
