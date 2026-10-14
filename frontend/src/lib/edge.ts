// Odds check maths. Everything comes from the published curve: no model runs, no smoothing.
import { probAt } from './curve'

export type Market = 'total' | 'home' | 'away'
export type Side = 'over' | 'under'
export type OddsFormat = 'decimal' | 'fractional' | 'american'
export const MARKETS = ['total', 'home', 'away'] as const
export const SIDES = ['over', 'under'] as const
export const ODDS_FORMATS = ['decimal', 'fractional', 'american'] as const

export const MARKET_LABEL: Record<Market, string> = { total: 'Total shots', home: 'Home team shots', away: 'Away team shots' }

export type OddsParse = { ok: true; decimal: number } | { ok: false; error: string }

/** Decimal "1.85", fractional "5/6" or American "-120" / "+150". The preferred format only settles ambiguity ("150"). */
export function parseOdds(text: string, pref: OddsFormat = 'decimal'): OddsParse {
  const t = text.trim().replace(/\s+/g, '')
  if (!t) return { ok: false, error: 'Enter the bookmaker odds.' }
  let dec: number
  if (t.includes('/')) {
    const m = /^(\d+(?:\.\d+)?)\/(\d+(?:\.\d+)?)$/.exec(t)
    if (!m || Number(m[2]) <= 0 || Number(m[1]) <= 0) return { ok: false, error: 'Fractional odds look like 5/6 (both numbers above zero).' }
    dec = 1 + Number(m[1]) / Number(m[2])
  } else if (/^[+-]\d+$/.test(t) || (pref === 'american' && /^\d+$/.test(t))) {
    const v = Number(t)
    if (Math.abs(v) < 100) return { ok: false, error: 'American odds are 100 or more, like -120 or +150.' }
    dec = v > 0 ? 1 + v / 100 : 1 + 100 / Math.abs(v)
  } else if (/^\d+(\.\d+)?$/.test(t)) {
    dec = Number(t)
    if (dec <= 1) return { ok: false, error: 'Decimal odds must be above 1.00, like 1.85.' }
  } else return { ok: false, error: 'Use decimal (1.85), fractional (5/6) or American (-120) odds.' }
  if (!Number.isFinite(dec) || dec > 1000) return { ok: false, error: 'Those odds are outside a believable range (1.01 to 1000).' }
  return { ok: true, decimal: dec }
}

/** Lines are half-lines (24.5): no refund case, so Over 24.5 is exactly "25 or more". */
export const isHalfLine = (x: number): boolean => Number.isFinite(x) && Math.abs(x - Math.floor(x) - 0.5) < 1e-9

export interface EdgeInput { curve: number[] | null | undefined; start: number; line: number; side: Side; decimal: number }
export type EdgeResult =
  | { ok: false; reason: 'range'; lo: number; hi: number }
  | { ok: true; model: number; implied: number; gapPts: number; fair: number | null; ev: number; reading: string }

/** Published range of lines for a curve: the first and last half-line whose "n+1 or more" is on the curve. */
export function lineRange(len: number, start: number): { lo: number; hi: number } {
  return { lo: start - 0.5, hi: start + len - 1.5 }
}

export function evaluate(i: EdgeInput): EdgeResult {
  const n = Math.floor(i.line)
  const over = probAt(i.curve, i.start, n + 1)
  if (over === null || !isHalfLine(i.line)) return { ok: false, reason: 'range', ...lineRange(i.curve?.length ?? 0, i.start) }
  const model = i.side === 'over' ? over : 1 - over
  const implied = 1 / i.decimal
  const gapPts = (model - implied) * 100
  const fair = model > 0 ? 1 / model : null
  const ev = model * (i.decimal - 1) - (1 - model)
  return { ok: true, model, implied, gapPts, fair, ev, reading: readGap(gapPts) }
}

export function readGap(gapPts: number): string {
  const a = Math.abs(gapPts)
  if (a < 1) return 'The model and the price agree to within a point.'
  const w = Math.round(a)
  const pts = `${w} ${w === 1 ? 'point' : 'points'}`
  return gapPts > 0 ? `The model is ${pts} more confident than the price implies.` : `The model is ${pts} less confident than the price implies.`
}

export const fmtPct = (p: number): string => `${(p * 100).toFixed(1)}%`
export const fmtEv = (ev: number): string => `${ev >= 0 ? '+' : '−'}${Math.abs(ev).toFixed(2)}`
export const fmtGap = (g: number): string => `${g >= 0 ? '+' : '−'}${Math.abs(g).toFixed(1)}`

export interface Check {
  id: string
  savedAt: string
  fixtureId: string
  home: string
  away: string
  league: string
  date: string
  market: Market
  side: Side
  line: number
  oddsText: string
  decimal: number
  model: number
}
export const MAX_CHECKS = 200

export type CheckOutcome = 'hit' | 'miss' | 'pending'
/** Over hits when the count is above the line; Under when below. Half-lines cannot push. */
export function settleCheck(c: Pick<Check, 'market' | 'side' | 'line'>, actual: { total: number; home: number; away: number } | null): CheckOutcome {
  if (!actual) return 'pending'
  const v = c.market === 'total' ? actual.total : c.market === 'home' ? actual.home : actual.away
  return (v > c.line) === (c.side === 'over') ? 'hit' : 'miss'
}

const csvCell = (v: string | number): string => {
  const s = String(v)
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}
export function checksCsv(rows: { c: Check; outcome: CheckOutcome }[]): string {
  const head = ['saved_at', 'league', 'date', 'home', 'away', 'market', 'side', 'line', 'odds_as_entered', 'decimal_odds', 'model_probability', 'outcome']
  const lines = rows.map(({ c, outcome }) =>
    [c.savedAt, c.league, c.date, c.home, c.away, MARKET_LABEL[c.market], c.side, c.line, c.oddsText, c.decimal.toFixed(3), c.model.toFixed(4), outcome].map(csvCell).join(','),
  )
  return [head.join(','), ...lines].join('\n')
}
