// Data layer: normalise board + results into list items, then filter / sort / group them.
import { probAt } from './curve'
import type { Dataset, Item, League } from './types'
import type { Chip, Sort } from './url'
import { kickoffView } from './time'
import type { TimeMode } from './time'

export interface TimeView { zone: string; mode: TimeMode }

/** Without a `view`, times are the venue's own (the published `date` and `kickoff_local`). */
export function buildItems(ds: Pick<Dataset, 'meta' | 'board' | 'results'>, view: TimeView = { zone: 'UTC', mode: 'venue' }): Item[] {
  const items: Item[] = []
  const seen = new Set<string>()
  for (const r of ds.results?.results ?? []) {
    seen.add(r.id)
    items.push({
      id: r.id, league: r.league, date: r.date, time: null, venueTime: false, utc: null, venueDate: r.date, venueClock: null, home: r.home, away: r.away,
      expected: r.expected, curve: r.curve, lb: r.lb, odds: r.odds, coldStart: false, experimental: r.experimental === true, dataQuality: 'full',
      aboveLeague: null, teamExpected: null, finished: true, actual: r.actual, fixture: null, result: r,
    })
  }
  for (const f of ds.board.fixtures) {
    if (seen.has(f.id)) continue
    const k = kickoffView(f, view.zone, view.mode)
    items.push({
      id: f.id, league: f.league, date: k.date, time: k.time, venueTime: k.venueTime, utc: k.utc, venueDate: k.venueDate, venueClock: k.venueClock, home: f.home, away: f.away,
      expected: f.expected, curve: f.curve, lb: f.lb, odds: f.odds, coldStart: f.cold_start,
      experimental: f.experimental === true, dataQuality: f.data_quality ?? 'full',
      aboveLeague: f.above_league,
      teamExpected: f.team ? [f.team.home.mean, f.team.away.mean] : null,
      finished: false, actual: null, fixture: f, result: null,
    })
  }
  return items
}

/** Every date we can show: upcoming days from meta plus dates of finished matches. Sorted ascending. */
export function availableDays(meta: Pick<Dataset['meta'], 'days'>, items: Item[], includeMeta = true): string[] {
  // meta.days are venue dates: only valid as-is when times are shown at the venue.
  const s = new Set<string>(includeMeta ? meta.days : [])
  for (const it of items) s.add(it.date)
  return [...s].sort()
}

/** The day to show when the URL has none (or an unknown one): today, else the next day with matches, else the last. */
export function resolveDate(requested: string | null, days: string[], today: string): string {
  if (requested) return requested
  if (days.length === 0) return today
  if (days.includes(today)) return today
  return days.find((d) => d >= today) ?? days[days.length - 1]
}

export function shiftDay(days: string[], current: string, dir: -1 | 1): string | null {
  const idx = days.findIndex((d) => d >= current)
  if (dir === 1) {
    const next = days.find((d) => d > current)
    return next ?? null
  }
  const prev = [...days].reverse().find((d) => d < current)
  return idx === -1 && days.length ? days[days.length - 1] : (prev ?? null)
}

export interface Filters {
  date: string | null // null = all dates (favourites tab)
  chip: Chip
  minProb: number // whole percent
  league: string
  n: number
  totalStart: number
  favTeams?: ReadonlySet<string>
  favMatches?: ReadonlySet<string>
  onlyFavs?: boolean
}

export function itemProb(it: Item, n: number, totalStart: number): number | null {
  return probAt(it.curve, totalStart, n)
}

export function filterItems(items: Item[], f: Filters): Item[] {
  return items.filter((it) => {
    if (f.date !== null && it.date !== f.date) return false
    if (f.league && it.league !== f.league) return false
    if (f.onlyFavs && !(f.favMatches?.has(it.id) || f.favTeams?.has(it.home) || f.favTeams?.has(it.away))) return false
    if (f.chip === 'done' && !it.finished) return false
    if (f.chip === 'odds' && !it.odds) return false
    const p = itemProb(it, f.n, f.totalStart)
    if (f.chip === 'p70' && !(p !== null && p >= 0.7)) return false
    if (f.minProb > 0 && !(p !== null && p >= f.minProb / 100)) return false
    return true
  })
}

/** Kick-off order: the UTC instant when both are known, else the displayed clock time. */
export const byKick = (a: Item, b: Item): number =>
  a.utc !== null && b.utc !== null ? a.utc - b.utc : (a.time ?? '').localeCompare(b.time ?? '')

export function sortItems(items: Item[], sort: Sort, n: number, totalStart: number): Item[] {
  const byTime = (a: Item, b: Item) =>
    a.date.localeCompare(b.date) ||
    Number(a.finished) - Number(b.finished) ||
    byKick(a, b) ||
    a.home.localeCompare(b.home)
  const out = [...items]
  if (sort === 'prob') {
    out.sort((a, b) => (itemProb(b, n, totalStart) ?? -1) - (itemProb(a, n, totalStart) ?? -1) || byTime(a, b))
  } else if (sort === 'above') {
    out.sort((a, b) => (b.aboveLeague ?? -Infinity) - (a.aboveLeague ?? -Infinity) || byTime(a, b))
  } else out.sort(byTime)
  return out
}

export interface Group { league: League; items: Item[] }

/** Featured competitions first, then tier, then name (stable, so the list does not jump when sorting). */
export const byLeagueOrder = (a: League, b: League): number =>
  Number(b.featured === true) - Number(a.featured === true) || a.tier - b.tier || a.name.localeCompare(b.name)

export function groupByLeague(items: Item[], leagues: League[]): Group[] {
  const byId = new Map(leagues.map((l) => [l.id, l]))
  const groups = new Map<string, Group>()
  for (const it of items) {
    let g = groups.get(it.league)
    if (!g) {
      g = { league: byId.get(it.league) ?? { id: it.league, name: it.league, country: '', tier: 99, fixtures: 0 }, items: [] }
      groups.set(it.league, g)
    }
    g.items.push(it)
  }
  return [...groups.values()].sort((a, b) => byLeagueOrder(a.league, b.league))
}

const byChance = (n: number, totalStart: number) => (a: Item, b: Item) =>
  (itemProb(b, n, totalStart) ?? 0) - (itemProb(a, n, totalStart) ?? 0) || byKick(a, b)

/** Upcoming fixtures on a day with a probability at n, best first. */
function dayCandidates(items: Item[], date: string, n: number, totalStart: number): Item[] {
  return items.filter((it) => !it.finished && it.date === date && itemProb(it, n, totalStart) !== null).sort(byChance(n, totalStart))
}

/**
 * Highest-probability upcoming fixtures on a day at threshold n. Experimental fixtures are left out, unless the day
 * has nothing else; then they are returned and `onlyExperimental` is true so the caller can label them.
 */
export function topPicksInfo(items: Item[], date: string, n: number, totalStart: number, limit = 5): { picks: Item[]; onlyExperimental: boolean } {
  const all = dayCandidates(items, date, n, totalStart)
  const solid = all.filter((it) => !it.experimental)
  if (solid.length === 0 && all.length > 0) return { picks: all.slice(0, limit), onlyExperimental: true }
  return { picks: solid.slice(0, limit), onlyExperimental: false }
}

export function topPicks(items: Item[], date: string, n: number, totalStart: number, limit = 5): Item[] {
  return topPicksInfo(items, date, n, totalStart, limit).picks
}

export interface DaySummary { matches: number; upcoming: number; clear70: number; best: Item | null; bestExperimental: boolean }

/** Numbers for the hero strip: everything on the day, how many clear 70% at n, and the best pick. */
export function summariseDay(items: Item[], date: string, n: number, totalStart: number): DaySummary {
  const day = items.filter((it) => it.date === date)
  const clear70 = day.filter((it) => (itemProb(it, n, totalStart) ?? 0) >= 0.7).length
  const { picks, onlyExperimental } = topPicksInfo(items, date, n, totalStart, 1)
  return { matches: day.length, upcoming: day.filter((it) => !it.finished).length, clear70, best: picks[0] ?? null, bestExperimental: onlyExperimental }
}

export function countLabel(shown: number, groups: number): string {
  if (shown === 0) return 'No matches match these filters.'
  return `${shown} ${shown === 1 ? 'match' : 'matches'} in ${groups} ${groups === 1 ? 'league' : 'leagues'}.`
}
