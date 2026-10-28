// Surge Watch: read-only helpers over surge.json and the fixtures' `surge` labels (docs/static-api.md).
// Nothing here changes a probability: the labels are shown next to the published numbers, never folded into them.
import { probAt } from './curve'
import type { SurgeSort } from './url'
import type { Fixture, Item, Result, SeasonPhase, StackCell, Stacking, Surge, SurgeLabels, SurgeRoute, SurgeSignal, TableContext, TableSide } from './types'

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

export interface Candidate { item: Item; boosts: SurgeSignal[]; dampers: SurgeSignal[]; p: number | null; strong: boolean }

/** A strong surge candidate: the export sets `strong` only where the catalogue's stacking rule allows it. */
export const isStrong = (labels: SurgeLabels | null | undefined): boolean => labels?.strong === true

/** Upcoming fixtures with at least one known boost signal: most signals first, then our P(n+), then kick-off. */
export function surgeCandidates(items: Item[], cat: Catalog | null, n: number, totalStart: number): Candidate[] {
  if (!cat) return []
  const out: Candidate[] = []
  for (const item of items) {
    if (item.finished) continue
    const got = labelsOf(item.fixture?.surge, cat)
    if (!got || got.boosts.length === 0) continue
    out.push({ item, ...got, p: probAt(item.curve, totalStart, n), strong: isStrong(item.fixture?.surge) })
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

// ---------------------------------------------------------------------------- table context (fixture `context`)

export const PHASES: SeasonPhase[] = ['early', 'middle', 'late', 'run_in']
export const PHASE_LABEL: Record<SeasonPhase, string> = { early: 'Early season', middle: 'Mid-season', late: 'Late season', run_in: 'Run-in' }

const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v)
const isSide = (t: unknown): t is TableSide => {
  const x = t as TableSide
  return !!x && typeof x === 'object' && isInt(x.of) && isInt(x.pts) && isInt(x.gp) && isInt(x.to_top) && isInt(x.to_safety)
    && (x.pos === null || isInt(x.pos)) && (x.left === null || isInt(x.left))
}

/** The fixture's table context, or null when absent or not in the published shape (never thrown). */
export function contextOf(f: Pick<Fixture, 'context'> | null | undefined): TableContext | null {
  const c = f?.context
  if (!c || typeof c !== 'object' || !isSide(c.home) || !isSide(c.away)) return null
  if (c.phase !== null && !PHASES.includes(c.phase)) return null
  return c
}

export function ordinal(n: number): string {
  const t = n % 100
  const s = t >= 11 && t <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' } as Record<number, string>)[n % 10] ?? 'th'
  return `${n}${s}`
}

const pl = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`

/** "3rd of 20 · 21 pts · 2 behind the leader" (plus the drop zone when it is close). */
export function sideLine(t: TableSide): string {
  if (t.pos === null) return `No league game yet · ${t.of} teams`
  const parts = [`${ordinal(t.pos)} of ${t.of}`, pl(t.pts, 'pt', 'pts')]
  if (t.to_top === 0) parts.push(t.pos === 1 ? 'top of the table' : 'level on points with the leader')
  else parts.push(`${t.to_top} behind the leader`)
  if (t.to_safety <= 0) parts.push('in the drop zone')
  else if (t.to_safety <= 3) parts.push(`${pl(t.to_safety, 'pt', 'pts')} above the drop zone`)
  return parts.join(' · ')
}

/** Fewest games left of the two teams (null for play-off formats). */
export function minLeft(c: TableContext): number | null {
  const l = [c.home.left, c.away.left].filter((x): x is number => x !== null)
  return l.length ? Math.min(...l) : null
}

/** Both teams' games left are unknown: a league that ends in play-off groups (Belgium, Greece). */
export const isPlayoffFormat = (c: TableContext): boolean => c.home.left === null && c.away.left === null

/** "Run-in: 5 games left", "Mid-season", ...; null when progress is unknown. Same definition as the signals. */
export function phaseText(c: TableContext): string | null {
  if (c.phase === null) return null
  if (c.phase === 'run_in') {
    const k = minLeft(c)
    return k === null ? PHASE_LABEL.run_in : `Run-in: ${pl(k, 'game', 'games')} left`
  }
  return PHASE_LABEL[c.phase]
}

/** Compact "3rd v 17th of 20" for list rows. */
export function positionsShort(c: TableContext): string {
  const p = (t: TableSide) => (t.pos === null ? 'no games' : ordinal(t.pos))
  return `${p(c.home)} v ${p(c.away)} of ${c.home.of}`
}

// ---------------------------------------------------------------------------- candidate sorting / phase filter

export const SURGE_SORT_LABEL: Record<SurgeSort, string> = { signals: 'Most signals', time: 'Kick-off', prob: 'Our chance' }

const kickoff = (a: Item, b: Item) =>
  a.date.localeCompare(b.date) || (a.utc !== null && b.utc !== null ? a.utc - b.utc : (a.time ?? '').localeCompare(b.time ?? '')) || a.home.localeCompare(b.home)

/** Re-sort candidates: by signals (the default order of surgeCandidates), kick-off, or our P(n+). */
export function sortCandidates(cs: Candidate[], sort: SurgeSort): Candidate[] {
  const bySignals = (a: Candidate, b: Candidate) => b.boosts.length - a.boosts.length
  const byP = (a: Candidate, b: Candidate) => (b.p ?? -1) - (a.p ?? -1)
  const cmp = {
    signals: (a: Candidate, b: Candidate) => bySignals(a, b) || byP(a, b) || kickoff(a.item, b.item),
    time: (a: Candidate, b: Candidate) => kickoff(a.item, b.item) || bySignals(a, b),
    prob: (a: Candidate, b: Candidate) => byP(a, b) || bySignals(a, b) || kickoff(a.item, b.item),
  }[sort]
  return [...cs].sort(cmp)
}

export const phaseOf = (c: Candidate): SeasonPhase | null => contextOf(c.item.fixture)?.phase ?? null

// ---------------------------------------------------------------------------- how candidates turned out (results.json `surge`)

/** Fewer settled matches than this in a group: hollow point and a small-sample note; overall: no chart yet. */
export const SURGE_SMALL = 10

export interface Settled { r: Result; boosts: string[]; dampers: string[]; surged: boolean; gap: number }
export interface Tally { n: number; surged: number }
export interface OutcomeWeek { date: string; flagged: Tally; other: Tally }

/** Settled ordinary results that were assessed (carry `surge`), newest first: surged = actual >= expected + margin. */
export function settledAssessed(results: Result[], margin: number): Settled[] {
  const out: Settled[] = []
  for (const r of results) {
    const s = r.surge
    if (!s || r.experimental || !Array.isArray(s.signals) || !Array.isArray(s.against)) continue
    const gap = r.actual.total - r.expected
    out.push({ r, boosts: s.signals.map(String), dampers: s.against.map(String), surged: gap >= margin, gap })
  }
  return out.sort((a, b) => b.r.date.localeCompare(a.r.date) || a.r.home.localeCompare(b.r.home))
}

export const tallyRate = (t: Tally): number | null => (t.n ? t.surged / t.n : null)
const add = (t: Tally, s: Settled) => { t.n += 1; t.surged += Number(s.surged) }

/** Monday of an ISO date's week (venue dates, as everywhere). */
export function weekOf(d: string): string {
  const t = new Date(`${d}T00:00:00Z`)
  t.setUTCDate(t.getUTCDate() - ((t.getUTCDay() + 6) % 7))
  return t.toISOString().slice(0, 10)
}

/** Per week (Monday, oldest first): matches with 1+ boost signal vs assessed matches without one. */
export function weeklyOutcomes(xs: Settled[]): OutcomeWeek[] {
  const by = new Map<string, OutcomeWeek>()
  for (const s of xs) {
    const k = weekOf(s.r.date)
    const w = by.get(k) ?? { date: k, flagged: { n: 0, surged: 0 }, other: { n: 0, surged: 0 } }
    add(s.boosts.length > 0 ? w.flagged : w.other, s)
    by.set(k, w)
  }
  return [...by.values()].sort((a, b) => a.date.localeCompare(b.date))
}

export function totals(xs: Settled[]): { flagged: Tally; other: Tally } {
  const t = { flagged: { n: 0, surged: 0 }, other: { n: 0, surged: 0 } }
  for (const s of xs) add(s.boosts.length > 0 ? t.flagged : t.other, s)
  return t
}

/** Settled counts per catalogue signal (catalogue order); ids the catalogue does not list are skipped. */
export function signalOutcomes(xs: Settled[], surge: Surge): { signal: SurgeSignal; tally: Tally }[] {
  return surge.signals.map((signal) => {
    const tally = { n: 0, surged: 0 }
    for (const s of xs) if ((signal.direction === 'boost' ? s.boosts : s.dampers).includes(signal.id)) add(tally, s)
    return { signal, tally }
  })
}

// ---------------------------------------------------------------------------- stacking (surge.json `stacking`)

export const stackingOf = (surge: Surge | null | undefined): Stacking | null => {
  const st = surge?.stacking
  return st && typeof st === 'object' && st.routes && st.routes.odds && st.routes.noodds ? st : null
}

/** Rows of the "By number of signals" table: one group per row ("0", "1", "2", "3+"; "2+" last), both routes. */
export function stackRows(st: Stacking): { group: string; cells: Record<SurgeRoute, StackCell | null> }[] {
  const groups = st.groups.filter((g) => g !== '2+').concat(st.groups.includes('2+') ? ['2+'] : [])
  return groups.map((group) => ({ group, cells: { odds: st.routes.odds.by_count[group] ?? null, noodds: st.routes.noodds.by_count[group] ?? null } }))
}

export const groupLabel = (g: string): string => (g === '0' ? 'No signal' : g === '1' ? '1 signal' : `${g} signals`)

/** Settled assessed matches by the season phase recorded with them (result `context`), flagged vs not. */
export function phaseOutcomes(xs: Settled[]): { phase: SeasonPhase; flagged: Tally; other: Tally }[] {
  const out = PHASES.map((phase) => ({ phase, flagged: { n: 0, surged: 0 }, other: { n: 0, surged: 0 } }))
  for (const s of xs) {
    const ph = contextOf(s.r)?.phase
    const row = out.find((o) => o.phase === ph)
    if (row) add(s.boosts.length > 0 ? row.flagged : row.other, s)
  }
  return out
}
