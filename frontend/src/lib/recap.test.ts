import { describe, expect, it } from 'vitest'
import { byDay, isProvisional, providerName, provisionalNote, recap, verdict, weeklyPoints, wilson } from './recap'
import { WEEKLY, results } from '../test-fixtures'
import type { Result } from './types'

const START = 10
const mk = (id: string, date: string, total: number, expected: number, lb80: number, p24: number): Result => {
  const curve = Array.from({ length: 36 }, (_, i) => (i + START === 24 ? p24 : 0.5))
  return { id, league: 'l', date, home: id, away: 'x', actual: { total, home: total - 10, away: 10 }, predicted_at: '', expected, lb: { '70': lb80 - 2, '80': lb80, '90': lb80 - 4 }, odds: false, curve }
}

describe('verdict and recap', () => {
  it('marks hit, call and floor from the published rows', () => {
    const v = verdict(mk('a', '2026-10-13', 24, 28, 25, 0.8), 24, START)
    expect(v).toMatchObject({ p: 0.8, hit: true, call: true, floorHeld: false, error: -4 })
    expect(verdict(mk('b', '2026-10-13', 23, 20, 20, 0.69), 24, START)).toMatchObject({ hit: false, call: false, floorHeld: true })
  })
  it('counts picks, hits, floor and average miss', () => {
    const vs = [mk('a', 'd', 30, 26, 22, 0.8), mk('b', 'd', 20, 26, 22, 0.75), mk('c', 'd', 25, 25, 22, 0.4)].map((r) => verdict(r, 24, START))
    expect(recap(vs)).toMatchObject({ matches: 3, calls: 2, callHits: 1, reached: 2, floorHeld: 2 })
    expect(recap(vs).mae).toBeCloseTo(10 / 3)
    expect(recap(vs).bias).toBeCloseTo(-2 / 3)
    expect(recap([])).toMatchObject({ matches: 0, mae: null, bias: null })
  })
  it('groups newest day first and changes with the threshold', () => {
    const rows = [mk('a', '2026-10-10', 30, 26, 22, 0.8), mk('b', '2026-10-12', 20, 26, 22, 0.8), mk('c', '2026-10-12', 31, 26, 22, 0.8)]
    const g = byDay(rows, 24, START)
    expect(g.map((d) => d.date)).toEqual(['2026-10-12', '2026-10-10'])
    expect(g[0].recap.reached).toBe(1)
    expect(byDay(rows, 19, START)[0].recap.reached).toBe(2)
  })
  it('fixture results split cleanly into official and experimental', () => {
    const xp = results.results.filter((r) => r.experimental === true)
    expect(xp.length).toBeGreaterThan(0)
    expect(xp.every((r) => r.result_source === 'manual' && !!r.source_url)).toBe(true)
    const league = results.results.filter((r) => r.experimental !== true)
    expect(league.filter((r) => r.result_status !== 'provisional').every((r) => r.result_source === undefined)).toBe(true)
    expect(league.filter((r) => r.result_status === 'provisional').map((r) => r.result_source)).toEqual(['espn'])
  })
  it('names the provider of a provisional result', () => {
    expect(isProvisional({ result_status: 'provisional' })).toBe(true)
    expect(isProvisional({ result_status: 'confirmed' })).toBe(false)
    expect(isProvisional({})).toBe(false)
    expect(provisionalNote({ result_source: 'espn' })).toBe('Entered by hand from ESPN; replaced by the official data when it arrives.')
    expect(providerName('whoscored')).toBe('Whoscored')
    expect(providerName(undefined)).toBe('a match page')
  })
})

describe('weekly points', () => {
  it('wilson is wide for small samples and bounded', () => {
    const [a, b] = wilson(2, 4)
    expect(a).toBeGreaterThan(0.1)
    expect(b).toBeLessThan(0.9)
    expect(b - a).toBeGreaterThan(0.5)
    expect(wilson(0, 0)).toEqual([0, 1])
    const [c, d] = wilson(30, 30)
    expect(d).toBe(1)
    expect(c).toBeGreaterThan(0.85)
  })
  it('reads the threshold, flags small samples, copes with no calls', () => {
    const w = weeklyPoints(WEEKLY, 24)
    expect(w.usedN).toBe(24)
    expect(w.exact).toBe(true)
    expect(w.points.map((p) => p.small)).toEqual([false, false, true, true])
    expect(w.points[0].rate).toBeCloseTo(19 / 24)
    expect(w.points[3].rate).toBeNull()
  })
  it('falls back to the nearest published threshold and survives empty or odd input', () => {
    expect(weeklyPoints(WEEKLY, 30)).toMatchObject({ usedN: 24, exact: false })
    expect(weeklyPoints(undefined, 24)).toEqual({ points: [], usedN: null, exact: false })
    const odd = weeklyPoints([{ date: '2026-10-05', matches: 2, mae: null, bias: null, floor80_held: null, by_n: {} }], 24)
    expect(odd.points[0]).toMatchObject({ picks: 0, rate: null, mae: null })
  })
})
