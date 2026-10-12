import { describe, expect, it } from 'vitest'
import { band, isHit, pct, probAt } from './curve'
import { availableDays, buildItems, countLabel, filterItems, groupByLeague, resolveDate, shiftDay, sortItems, topPicks } from './items'
import type { Filters } from './items'
import { badgeHue, freshness, initials, todayIso } from './format'
import { buildHash, parseHash, parseState, stateToParams } from './url'
import { entryFor } from './track'
import { dataset, meta, results, track } from '../test-fixtures'
import { loadDataset } from './data'

const START = meta.thresholds.total[0]
const base: Filters = { date: '2026-10-12', chip: 'all', minProb: 0, league: '', n: 24, totalStart: START }

describe('curve lookup', () => {
  const curve = [1, 0.9, 0.5, 0.1]
  it('indexes from the curve start', () => {
    expect(probAt(curve, 10, 10)).toBe(1)
    expect(probAt(curve, 10, 12)).toBe(0.5)
    expect(probAt(curve, 10, 13)).toBe(0.1)
  })
  it('is null outside the published range or for missing curves', () => {
    expect(probAt(curve, 10, 9)).toBeNull()
    expect(probAt(curve, 10, 14)).toBeNull()
    expect(probAt(null, 10, 10)).toBeNull()
    expect(probAt([Number.NaN], 10, 10)).toBeNull()
  })
  it('never rounds near-certainty up to 100%', () => {
    expect(pct(0.9999)).toBe('99%+')
    expect(pct(1)).toBe('100%')
    expect(pct(0.001)).toBe('<1%')
    expect(pct(0.764)).toBe('76%')
    expect(pct(null)).toBe('–')
  })
  it('bands at the 70% bar and 50%', () => {
    expect([band(0.7), band(0.69), band(0.49), band(null)]).toEqual(['hi', 'mid', 'lo', 'lo'])
  })
  it('hit means actual total at or above the line', () => {
    expect(isHit(24, 24)).toBe(true)
    expect(isHit(23, 24)).toBe(false)
  })
})

describe('items: build, filter, sort, group', () => {
  const items = buildItems(dataset)
  it('merges upcoming fixtures and finished results', () => {
    expect(items.filter((i) => i.finished)).toHaveLength(results.results.length)
    expect(items.filter((i) => !i.finished)).toHaveLength(dataset.board.fixtures.length)
    expect(items.filter((i) => i.finished).every((i) => i.actual && i.time === null)).toBe(true)
  })
  it('prefers the finished record if an id is in both files', () => {
    const r = results.results[0]
    const dup = { ...dataset, board: { ...dataset.board, fixtures: [{ ...dataset.board.fixtures[0], id: r.id }] } }
    expect(buildItems(dup).filter((i) => i.id === r.id)).toHaveLength(1)
    expect(buildItems(dup).find((i) => i.id === r.id)!.finished).toBe(true)
  })
  it('filters by date and league', () => {
    const day = filterItems(items, base)
    expect(day.length).toBeGreaterThan(0)
    expect(day.every((i) => i.date === '2026-10-12')).toBe(true)
    const lg = filterItems(items, { ...base, league: day[0].league })
    expect(lg.every((i) => i.league === day[0].league)).toBe(true)
  })
  it('70%+ keeps exactly the items whose curve value at N is >= 0.7', () => {
    for (const n of [20, 24, 30]) {
      const f = filterItems(items, { ...base, date: null, chip: 'p70', n })
      expect(f.every((i) => i.curve[n - START] >= 0.7)).toBe(true)
      const all = filterItems(items, { ...base, date: null, n })
      expect(f).toHaveLength(all.filter((i) => i.curve[n - START] >= 0.7).length)
    }
  })
  it('a higher threshold can only shrink the 70%+ set', () => {
    const a = filterItems(items, { ...base, date: null, chip: 'p70', n: 22 }).length
    const b = filterItems(items, { ...base, date: null, chip: 'p70', n: 30 }).length
    expect(b).toBeLessThanOrEqual(a)
  })
  it('odds and finished chips and the minimum-probability slider', () => {
    expect(filterItems(items, { ...base, date: null, chip: 'odds' }).every((i) => i.odds)).toBe(true)
    expect(filterItems(items, { ...base, date: null, chip: 'done' }).every((i) => i.finished)).toBe(true)
    const strict = filterItems(items, { ...base, date: null, minProb: 90 })
    expect(strict.every((i) => i.curve[24 - START] >= 0.9)).toBe(true)
  })
  it('favourites filter matches by match id or team', () => {
    const it0 = items.find((i) => !i.finished)!
    const byMatch = filterItems(items, { ...base, date: null, onlyFavs: true, favMatches: new Set([it0.id]), favTeams: new Set() })
    expect(byMatch.map((i) => i.id)).toEqual([it0.id])
    const byTeam = filterItems(items, { ...base, date: null, onlyFavs: true, favMatches: new Set(), favTeams: new Set([it0.home]) })
    expect(byTeam.every((i) => i.home === it0.home || i.away === it0.home)).toBe(true)
    expect(filterItems(items, { ...base, date: null, onlyFavs: true })).toHaveLength(0)
  })
  it('sorts by time, probability and above-league', () => {
    const day = filterItems(items, { ...base, date: '2026-10-17' })
    const t = sortItems(day, 'time', 24, START)
    expect(t.map((i) => i.time)).toEqual([...t.map((i) => i.time)].sort())
    const p = sortItems(day, 'prob', 24, START).map((i) => i.curve[24 - START])
    expect(p).toEqual([...p].sort((a, b) => b - a))
    const a = sortItems(day, 'above', 24, START).map((i) => i.aboveLeague ?? -Infinity)
    expect(a).toEqual([...a].sort((x, y) => y - x))
  })
  it('groups by league, top tier first', () => {
    const groups = groupByLeague(filterItems(items, { ...base, date: null }), meta.leagues)
    const tiers = groups.map((g) => g.league.tier)
    expect(tiers).toEqual([...tiers].sort((a, b) => a - b))
    expect(groups.reduce((n, g) => n + g.items.length, 0)).toBe(filterItems(items, { ...base, date: null }).length)
  })
  it('top picks are upcoming, on the day, ordered by chance', () => {
    const picks = topPicks(items, '2026-10-17', 24, START, 3)
    expect(picks.length).toBeLessThanOrEqual(3)
    const ps = picks.map((i) => i.curve[24 - START])
    expect(ps).toEqual([...ps].sort((a, b) => b - a))
    expect(picks.every((i) => !i.finished && i.date === '2026-10-17')).toBe(true)
  })
  it('count label for the live region', () => {
    expect(countLabel(0, 0)).toMatch(/No matches/)
    expect(countLabel(1, 1)).toBe('1 match in 1 league.')
    expect(countLabel(5, 2)).toBe('5 matches in 2 leagues.')
  })
})

describe('days', () => {
  const days = ['2026-10-04', '2026-10-12', '2026-10-13', '2026-10-17']
  it('union of meta days and result dates, sorted', () => {
    const d = availableDays(meta, buildItems(dataset))
    expect(d).toEqual([...d].sort())
    expect(d).toContain('2026-10-04')
    expect(d).toContain('2026-10-18')
  })
  it('today, else next day with data, else the last', () => {
    expect(resolveDate(null, days, '2026-10-12')).toBe('2026-10-12')
    expect(resolveDate(null, days, '2026-10-14')).toBe('2026-10-17')
    expect(resolveDate(null, days, '2027-01-01')).toBe('2026-10-17')
    expect(resolveDate('2026-10-13', days, '2026-10-12')).toBe('2026-10-13')
    expect(resolveDate(null, [], '2026-10-12')).toBe('2026-10-12')
  })
  it('prev/next skip gaps and stop at the ends', () => {
    expect(shiftDay(days, '2026-10-13', 1)).toBe('2026-10-17')
    expect(shiftDay(days, '2026-10-12', -1)).toBe('2026-10-04')
    expect(shiftDay(days, '2026-10-17', 1)).toBeNull()
    expect(shiftDay(days, '2026-10-04', -1)).toBeNull()
    expect(shiftDay(days, '2026-10-14', -1)).toBe('2026-10-13')
  })
})

describe('URL state', () => {
  it('parses hash routes', () => {
    expect(parseHash('')).toMatchObject({ path: '/' })
    expect(parseHash('#/track?n=26').params.get('n')).toBe('26')
    expect(parseHash('#/match/abc').path).toBe('/match/abc')
  })
  it('defaults come from meta', () => {
    const s = parseState(new URLSearchParams(), meta)
    expect(s).toMatchObject({ n: 24, tn: 12, p: 0, sort: 'time', chip: 'all', tab: 'all', m: null, date: null })
  })
  it('clamps or ignores bad values, never throws', () => {
    const s = parseState(new URLSearchParams('n=999&tn=-4&p=500&sort=bogus&chip=x&tab=%3Cb%3E&m=../../etc&date=2026-13-45&pt=zz&rec=q'), meta)
    expect(s.n).toBe(45)
    expect(s.tn).toBe(3)
    expect(s.p).toBe(95)
    expect(s).toMatchObject({ sort: 'time', chip: 'all', tab: 'all', m: null, date: null, pt: 'pred', rec: 'backfill' })
    expect(parseState(new URLSearchParams('n=abc'), meta).n).toBe(24)
    expect(parseState(new URLSearchParams('n=1'), meta).n).toBe(10)
  })
  it('round-trips and omits defaults', () => {
    const s = parseState(new URLSearchParams('date=2026-10-13&n=27&p=70&lg=spain-la-liga&sort=prob&chip=p70&m=fx001&pt=teams&tn=10'), meta)
    const params = stateToParams(s, meta)
    expect(Object.keys(params).sort()).toEqual(['chip', 'date', 'lg', 'm', 'n', 'p', 'pt', 'sort', 'tn'])
    expect(parseState(new URLSearchParams(params), meta)).toEqual(s)
    expect(stateToParams(parseState(new URLSearchParams(), meta), meta)).toEqual({})
    expect(buildHash('/', {})).toBe('#/')
    expect(buildHash('/', { n: '25', x: '' })).toBe('#/?n=25')
  })
})

describe('format and track helpers', () => {
  it('freshness warns when stale (> 48h) or when the last job failed', () => {
    const built = Date.parse(meta.built_at)
    expect(freshness(meta, built + 3600_000).level).toBe('ok')
    expect(freshness(meta, built + 49 * 3600_000).level).toBe('stale')
    const failed = freshness({ ...meta, last_job: { status: 'failed' } }, built + 3600_000)
    expect(failed.level).toBe('failed')
    expect(failed.reasons.join(' ')).toMatch(/last update/i)
    expect(freshness({ built_at: 'garbage', last_job: null }).level).toBe('stale')
  })
  it('badges are deterministic and crest-free', () => {
    expect(badgeHue('Girona')).toBe(badgeHue('Girona'))
    expect(initials('Man United')).toBe('MU')
    expect(initials('Girona')).toBe('Gi')
    expect(initials('')).toBe('?')
    expect(todayIso(new Date(2026, 9, 5))).toBe('2026-10-05')
  })
  it('track entry falls back to the nearest published threshold', () => {
    expect(entryFor(track.records.backfill, 24)).toMatchObject({ n: 24, exact: true })
    expect(entryFor(track.records.backfill, 25)).toMatchObject({ n: 24, exact: false })
    expect(entryFor(track.records.live, 24)).toBeNull()
    expect(entryFor(undefined, 24)).toBeNull()
  })
})

describe('loading', () => {
  const ok = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))
  it('loads all four files', async () => {
    const f = (async (u: string) => ok({ 'meta.json': meta, 'board.json': dataset.board, 'results.json': dataset.results, 'track.json': track }[u.split('/').pop()!])) as unknown as typeof fetch
    const ds = await loadDataset(f)
    expect(ds.board.fixtures.length).toBe(dataset.board.fixtures.length)
    expect(ds.results).not.toBeNull()
  })
  it('degrades when results/track are missing but fails on board', async () => {
    const mk = (missing: string[]) => (async (u: string) => {
      const name = u.split('/').pop()!
      if (missing.includes(name)) return new Response('no', { status: 404 })
      return new Response(JSON.stringify({ 'meta.json': meta, 'board.json': dataset.board }[name] ?? { schema: 1 }), { status: 200 })
    }) as unknown as typeof fetch
    expect((await loadDataset(mk(['results.json', 'track.json']))).results).toBeNull()
    await expect(loadDataset(mk(['board.json']))).rejects.toThrow(/board.json/)
  })
  it('rejects html served in place of json', async () => {
    const f = (async () => new Response('<!doctype html>', { status: 200 })) as unknown as typeof fetch
    await expect(loadDataset(f)).rejects.toThrow(/not valid shots data/)
  })
})
