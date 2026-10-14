import { describe, expect, it } from 'vitest'
import { checksCsv, evaluate, isHalfLine, lineRange, parseOdds, readGap, settleCheck } from './edge'
import type { Check } from './edge'
import { buildWaterfall, matchesNote, signedShots } from './explain'
import { parseState, stateToParams } from './url'
import { EXPLAIN_FULL, EXPLAIN_PARTIAL, meta } from '../test-fixtures'

const curve = Array.from({ length: 36 }, (_, i) => 1 - i / 40) // start 10: P(>=25) = curve[15] = 0.625

describe('parseOdds', () => {
  it('reads decimal, fractional and American odds', () => {
    expect(parseOdds('1.85')).toEqual({ ok: true, decimal: 1.85 })
    const frac = parseOdds('5/6')
    expect(frac.ok && frac.decimal).toBeCloseTo(1.8333, 3)
    const neg = parseOdds('-120')
    expect(neg.ok && neg.decimal).toBeCloseTo(1.8333, 3)
    expect(parseOdds('+150')).toEqual({ ok: true, decimal: 2.5 })
    expect(parseOdds(' 2 ')).toEqual({ ok: true, decimal: 2 })
  })
  it('uses the preferred format only for ambiguous bare numbers', () => {
    expect(parseOdds('150', 'american')).toEqual({ ok: true, decimal: 2.5 })
    expect(parseOdds('150', 'decimal')).toEqual({ ok: true, decimal: 150 })
  })
  it('gives plain errors for bad input', () => {
    for (const bad of ['', 'abc', '1', '0.9', '5/0', '0/5', '-50', '1/2/3', '5000', '1e3']) {
      const r = parseOdds(bad)
      expect(r.ok, bad).toBe(false)
      if (!r.ok) expect(r.error.length).toBeGreaterThan(5)
    }
  })
})

describe('evaluate', () => {
  it('Over N.5 is P(total >= N+1), straight from the curve', () => {
    const r = evaluate({ curve, start: 10, line: 24.5, side: 'over', decimal: 2 })
    expect(r.ok && r.model).toBeCloseTo(0.625, 10)
  })
  it('Under is the complement', () => {
    const r = evaluate({ curve, start: 10, line: 24.5, side: 'under', decimal: 2 })
    expect(r.ok && r.model).toBeCloseTo(0.375, 10)
  })
  it('computes implied probability, gap, fair odds and expected value', () => {
    const r = evaluate({ curve, start: 10, line: 24.5, side: 'over', decimal: 2 })
    if (!r.ok) throw new Error('expected ok')
    expect(r.implied).toBe(0.5)
    expect(r.gapPts).toBeCloseTo(12.5, 8)
    expect(r.fair).toBeCloseTo(1.6, 8)
    expect(r.ev).toBeCloseTo(0.625 * 1 - 0.375, 8) // p * (odds - 1) - (1 - p)
    expect(r.reading).toBe('The model is 13 points more confident than the price implies.')
  })
  it('says less confident when the price is shorter than the model', () => {
    const r = evaluate({ curve, start: 10, line: 24.5, side: 'over', decimal: 1.25 })
    if (!r.ok) throw new Error('expected ok')
    expect(r.gapPts).toBeCloseTo(-17.5, 8)
    expect(r.reading).toContain('less confident')
    expect(r.ev).toBeLessThan(0)
  })
  it('refuses lines outside the published range instead of extrapolating', () => {
    expect(evaluate({ curve, start: 10, line: 9.5, side: 'over', decimal: 2 }).ok).toBe(true) // P(>=10) is the first point on the curve
    expect(evaluate({ curve, start: 10, line: 8.5, side: 'over', decimal: 2 }).ok).toBe(false)
    expect(evaluate({ curve, start: 10, line: 45.5, side: 'over', decimal: 2 }).ok).toBe(false)
    expect(evaluate({ curve, start: 10, line: 44.5, side: 'over', decimal: 2 }).ok).toBe(true)
    expect(evaluate({ curve, start: 10, line: 24, side: 'over', decimal: 2 }).ok).toBe(false) // whole lines are not offered
    expect(evaluate({ curve: null, start: 10, line: 24.5, side: 'over', decimal: 2 }).ok).toBe(false)
  })
  it('has no fair odds when the model chance is zero', () => {
    const r = evaluate({ curve: [1, 0.5, 0], start: 10, line: 11.5, side: 'over', decimal: 3 })
    expect(r.ok && r.fair).toBeNull()
  })
  it('range helpers', () => {
    expect(lineRange(36, 10)).toEqual({ lo: 9.5, hi: 44.5 })
    expect(isHalfLine(24.5)).toBe(true)
    expect(isHalfLine(24)).toBe(false)
    expect(readGap(0.4)).toContain('agree')
    expect(readGap(-1)).toBe('The model is 1 point less confident than the price implies.')
  })
})

describe('checks', () => {
  const c: Check = {
    id: 'a', savedAt: '2026-10-12T10:00:00.000Z', fixtureId: 'fx1', home: 'A, FC', away: 'B', league: 'Premier League', date: '2026-10-12',
    market: 'total', side: 'over', line: 24.5, oddsText: '5/6', decimal: 1.8333, model: 0.625,
  }
  it('settles over/under on the half-line, never a push', () => {
    const a = { total: 25, home: 14, away: 11 }
    expect(settleCheck(c, a)).toBe('hit')
    expect(settleCheck(c, { ...a, total: 24 })).toBe('miss')
    expect(settleCheck({ ...c, side: 'under' }, { ...a, total: 24 })).toBe('hit')
    expect(settleCheck({ ...c, market: 'home', line: 13.5 }, a)).toBe('hit')
    expect(settleCheck({ ...c, market: 'away', side: 'under', line: 11.5 }, a)).toBe('hit')
    expect(settleCheck(c, null)).toBe('pending')
  })
  it('exports quoted CSV', () => {
    const csv = checksCsv([{ c, outcome: 'pending' }])
    const [head, row] = csv.split('\n')
    expect(head.startsWith('saved_at,league')).toBe(true)
    expect(row).toContain('"A, FC"')
    expect(row.endsWith(',pending')).toBe(true)
  })
})

describe('odds check URL state', () => {
  it('round-trips market, side, line and odds and ignores junk', () => {
    const s = parseState(new URLSearchParams('mk=home&sd=under&ln=23.5&od=-120'), meta)
    expect(s).toMatchObject({ mk: 'home', sd: 'under', ln: 23.5, od: '-120' })
    expect(stateToParams(s, meta)).toMatchObject({ mk: 'home', sd: 'under', ln: '23.5', od: '-120' })
    const bad = parseState(new URLSearchParams('mk=x&sd=y&ln=24&od=%3Cscript%3E&pt=edge'), meta)
    expect(bad).toMatchObject({ mk: 'total', sd: 'over', ln: null, od: '', pt: 'edge' })
    expect(stateToParams(parseState(new URLSearchParams(''), meta), meta)).toEqual({})
  })
})

describe('buildWaterfall', () => {
  it('builds running totals, keeps negative effects and a residual row', () => {
    const w = buildWaterfall(EXPLAIN_FULL)!
    expect(w.baseline?.value).toBe(27.8)
    expect(w.rows.map((r) => r.effect)).toEqual([2.9, -1.2, 1.0, 2.1])
    expect(w.rows[1].running).toBeCloseTo(29.5, 8)
    expect(w.total).toBe(32.6)
    expect(w.maxAbs).toBe(2.9)
  })
  it('works without baseline or signals and hides when empty', () => {
    const w = buildWaterfall(EXPLAIN_PARTIAL)!
    expect(w.baseline).toBeNull()
    expect(w.total).toBeNull()
    expect(w.rows).toHaveLength(1)
    expect(buildWaterfall(undefined)).toBeNull()
    expect(buildWaterfall({})).toBeNull()
    expect(buildWaterfall({ factors: [{ id: 'x', label: 'bad', effect: Number.NaN }] })).toBeNull()
  })
  it('formats', () => {
    expect(signedShots(-1.24)).toBe('−1.2')
    expect(signedShots(0)).toBe('+0.0')
    expect(matchesNote('Inter', 4)).toContain('very few')
    expect(matchesNote('Inter', 38)).toBe('Inter: form built on 38 matches')
    expect(matchesNote('Inter', null)).toBeNull()
  })
})
