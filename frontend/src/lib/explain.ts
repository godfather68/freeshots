// Turn the optional `explain` block into rows for the "why this number" list. Display only; nothing is recomputed.
import type { Explain, Volatility } from './types'

export interface WRow { key: string; label: string; detail: string | null; effect: number; side: 'home' | 'away' | 'match' | null; running: number }
export interface Waterfall {
  baseline: { value: number; label: string } | null
  rows: WRow[]
  total: number | null
  maxAbs: number
}

const fin = (x: unknown): x is number => typeof x === 'number' && Number.isFinite(x)

/** null when there is nothing to show (no baseline and no factors). */
export function buildWaterfall(ex: Explain | null | undefined): Waterfall | null {
  if (!ex) return null
  const baseline = ex.baseline && fin(ex.baseline.value) ? { value: ex.baseline.value, label: ex.baseline.label || 'League average' } : null
  const factors = (Array.isArray(ex.factors) ? ex.factors : []).filter((f) => f && fin(f.effect) && typeof f.label === 'string')
  if (!baseline && factors.length === 0) return null
  let running = baseline?.value ?? 0
  const rows: WRow[] = factors.map((f, i) => {
    running += f.effect
    return { key: `${f.id}-${i}`, label: f.label, detail: f.detail ?? null, effect: f.effect, side: f.side ?? null, running }
  })
  if (fin(ex.residual) && Math.abs(ex.residual) >= 0.05) {
    running += ex.residual
    rows.push({ key: 'residual', label: 'Everything else', detail: 'Smaller effects combined', effect: ex.residual, side: null, running })
  }
  const total = fin(ex.expected_mean) ? ex.expected_mean : baseline ? running : null
  const maxAbs = Math.max(0.1, ...rows.map((r) => Math.abs(r.effect)))
  return { baseline, rows, total, maxAbs }
}

export const signedShots = (x: number): string => `${x >= 0 ? '+' : '−'}${Math.abs(x).toFixed(1)}`

export function matchesNote(team: string, n: number | null | undefined): string | null {
  if (typeof n !== 'number') return null
  const base = `${team}: form built on ${n} ${n === 1 ? 'match' : 'matches'}`
  return n < 5 ? `${base}, very few` : n < 10 ? `${base}, a short run` : base
}

export function volatilityNote(team: string, v: Volatility | null | undefined): string | null {
  if (v === 'high') return `${team}: shot counts swing a lot from match to match`
  if (v === 'low') return `${team}: shot counts are steady`
  if (v === 'average') return `${team}: shot counts vary about as much as usual`
  return null
}
