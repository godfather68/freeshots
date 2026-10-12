// Curve lookups. Probabilities are the models' calibrated values; this module only indexes them.
export function clamp(x: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, x))
}

/** P(shots >= n) from a curve that starts at `start`. null when n is outside the published range. */
export function probAt(curve: number[] | null | undefined, start: number, n: number): number | null {
  if (!curve) return null
  const i = n - start
  if (!Number.isInteger(i) || i < 0 || i >= curve.length) return null
  const v = curve[i]
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/** Whole-percent label that never claims certainty it does not have. */
export function pct(p: number | null): string {
  if (p === null) return '–'
  if (p >= 0.995 && p < 1) return '99%+'
  if (p > 0 && p < 0.005) return '<1%'
  return `${Math.round(p * 100)}%`
}

export type Band = 'hi' | 'mid' | 'lo'
export function band(p: number | null): Band {
  if (p === null) return 'lo'
  return p >= 0.7 ? 'hi' : p >= 0.5 ? 'mid' : 'lo'
}
export const BAND_WORD: Record<Band, string> = { hi: 'clears the 70% bar', mid: 'below the 70% bar', lo: 'unlikely' }

/** Hit/miss for a finished match at threshold n. */
export function isHit(actualTotal: number, n: number): boolean {
  return actualTotal >= n
}
