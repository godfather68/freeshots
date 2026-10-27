// Surge Watch: read-only helpers over surge.json and the fixtures' `surge` labels (docs/static-api.md).
// Nothing here changes a probability: the labels are shown next to the published numbers, never folded into them.
import { probAt } from './curve'
import type { Item, Surge, SurgeLabels, SurgeRoute, SurgeSignal } from './types'

export type Catalog = Map<string, SurgeSignal>

export function catalogOf(surge: Surge | null | undefined): Catalog | null {
  if (!surge || !Array.isArray(surge.signals)) return null
  return new Map(surge.signals.map((s) => [s.id, s]))
}

export const routeOf = (it: Pick<Item, 'odds'>): SurgeRoute => (it.odds ? 'odds' : 'noodds')

/** The fixture's labels resolved against the catalogue; unknown ids (a newer export) are skipped. */
export function labelsOf(labels: SurgeLabels | null | undefined, cat: Catalog | null): { boosts: SurgeSignal[]; dampers: SurgeSignal[] } | null {
  if (!labels || !cat) return null
  const pick = (ids: unknown, dir: SurgeSignal['direction']) =>
    (Array.isArray(ids) ? ids : []).map((id) => cat.get(String(id))).filter((s): s is SurgeSignal => !!s && s.direction === dir)
  return { boosts: pick(labels.signals, 'boost'), dampers: pick(labels.against, 'damper') }
}

/** Whole percent, or one decimal when that is what tells two close rates apart. */
export function rate(x: number, digits = 0): string {
  const k = 10 ** digits
  return `${(Math.round(x * 100 * k) / k).toFixed(digits)}%` // half up on the decimal value (0.2195 -> 22.0%)
}

/** The usual surge share, both routes together (they differ by a few tenths of a point). */
export function usualRate(surge: Surge): number {
  const r = Object.values(surge.routes).map((x) => x.base_rate)
  return r.reduce((a, b) => a + b, 0) / Math.max(r.length, 1)
}

export interface Candidate { item: Item; boosts: SurgeSignal[]; dampers: SurgeSignal[]; p: number | null }

/** Upcoming fixtures with at least one known boost signal: most signals first, then our P(n+), then kick-off. */
export function surgeCandidates(items: Item[], cat: Catalog | null, n: number, totalStart: number): Candidate[] {
  if (!cat) return []
  const out: Candidate[] = []
  for (const item of items) {
    if (item.finished) continue
    const got = labelsOf(item.fixture?.surge, cat)
    if (!got || got.boosts.length === 0) continue
    out.push({ item, ...got, p: probAt(item.curve, totalStart, n) })
  }
  const kick = (a: Item, b: Item) =>
    a.date.localeCompare(b.date) || (a.utc !== null && b.utc !== null ? a.utc - b.utc : (a.time ?? '').localeCompare(b.time ?? '')) || a.home.localeCompare(b.home)
  return out.sort((a, b) => b.boosts.length - a.boosts.length || (b.p ?? -1) - (a.p ?? -1) || kick(a.item, b.item))
}

/** Signals by direction, boosts first (catalogue order within each). */
export function byDirection(surge: Surge): { boosts: SurgeSignal[]; dampers: SurgeSignal[] } {
  return { boosts: surge.signals.filter((s) => s.direction === 'boost'), dampers: surge.signals.filter((s) => s.direction === 'damper') }
}

/** Routes in display order: most of the board is scored without odds. */
export const ROUTES: SurgeRoute[] = ['noodds', 'odds']
