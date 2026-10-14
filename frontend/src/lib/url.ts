// Shareable state lives in the hash: #/<route>?date=...&n=...  (hash routing works on any static host).
import { clamp } from './curve'
import type { Meta } from './types'
import { MARKETS, SIDES } from './edge'
import type { Market, Side } from './edge'

export const SORTS = ['time', 'prob', 'above'] as const
export type Sort = (typeof SORTS)[number]
export const CHIPS = ['all', 'p70', 'odds', 'done'] as const
export type Chip = (typeof CHIPS)[number]
export const TABS = ['all', 'fav', 'leagues'] as const
export type ListTab = (typeof TABS)[number]
export const PANEL_TABS = ['pred', 'teams', 'edge', 'about'] as const
export type PanelTab = (typeof PANEL_TABS)[number]
export const RECORDS = ['backfill', 'live'] as const
export type RecordKind = (typeof RECORDS)[number]

export interface ViewState {
  date: string | null // null = "today" (resolved against the data)
  n: number
  tn: number
  p: number // minimum probability, whole percent
  lg: string
  sort: Sort
  chip: Chip
  tab: ListTab
  m: string | null
  pt: PanelTab
  rec: RecordKind
  mk: Market
  sd: Side
  ln: number | null // odds-check line (n + 0.5); null = default for the market
  od: string // odds as typed, shareable
}

export interface Route { path: string; params: URLSearchParams }

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#/, '')
  const q = raw.indexOf('?')
  const path = (q === -1 ? raw : raw.slice(0, q)) || '/'
  const params = new URLSearchParams(q === -1 ? '' : raw.slice(q + 1))
  return { path: path.startsWith('/') ? path : `/${path}`, params }
}

export function buildHash(path: string, params: URLSearchParams | Record<string, string | null | undefined>): string {
  const sp = new URLSearchParams()
  const entries = params instanceof URLSearchParams ? [...params.entries()] : Object.entries(params)
  for (const [k, v] of entries) if (v !== null && v !== undefined && v !== '') sp.set(k, v)
  const s = sp.toString()
  return `#${path}${s ? `?${s}` : ''}`
}

function oneOf<T extends string>(list: readonly T[], v: string | null, fallback: T): T {
  return (list as readonly string[]).includes(v ?? '') ? (v as T) : fallback
}
function int(v: string | null, fallback: number): number {
  if (v === null || !/^-?\d{1,4}$/.test(v)) return fallback
  return Number(v)
}
export const isDate = (s: string | null): s is string => !!s && /^\d{4}-\d{2}-\d{2}$/.test(s) && !Number.isNaN(Date.parse(s))

/** Defaults filled from meta; every bad value is clamped or ignored, never thrown. */
export function parseState(params: URLSearchParams, meta: Pick<Meta, 'thresholds'>): ViewState {
  const [tlo, thi] = meta.thresholds.total
  const [plo, phi] = meta.thresholds.team
  const date = params.get('date')
  const m = params.get('m')
  return {
    date: isDate(date) ? date : null,
    n: clamp(int(params.get('n'), meta.thresholds.default_total), tlo, thi),
    tn: clamp(int(params.get('tn'), meta.thresholds.default_team), plo, phi),
    p: clamp(int(params.get('p'), 0), 0, 95),
    lg: params.get('lg') ?? '',
    sort: oneOf(SORTS, params.get('sort'), 'time'),
    chip: oneOf(CHIPS, params.get('chip'), 'all'),
    tab: oneOf(TABS, params.get('tab'), 'all'),
    m: m && /^[\w-]{1,64}$/.test(m) ? m : null,
    pt: oneOf(PANEL_TABS, params.get('pt'), 'pred'),
    rec: oneOf(RECORDS, params.get('rec'), 'backfill'),
    mk: oneOf(MARKETS, params.get('mk'), 'total'),
    sd: oneOf(SIDES, params.get('sd'), 'over'),
    ln: /^\d{1,2}\.5$/.test(params.get('ln') ?? '') ? Number(params.get('ln')) : null,
    od: /^[\d.+\-/]{1,10}$/.test(params.get('od') ?? '') ? (params.get('od') as string) : '',
  }
}

/** Serialise only what differs from defaults so shared links stay short. */
export function stateToParams(s: ViewState, meta: Pick<Meta, 'thresholds'>): Record<string, string> {
  const out: Record<string, string> = {}
  if (s.date) out.date = s.date
  if (s.n !== meta.thresholds.default_total) out.n = String(s.n)
  if (s.tn !== meta.thresholds.default_team) out.tn = String(s.tn)
  if (s.p > 0) out.p = String(s.p)
  if (s.lg) out.lg = s.lg
  if (s.sort !== 'time') out.sort = s.sort
  if (s.chip !== 'all') out.chip = s.chip
  if (s.tab !== 'all') out.tab = s.tab
  if (s.m) out.m = s.m
  if (s.pt !== 'pred') out.pt = s.pt
  if (s.rec !== 'backfill') out.rec = s.rec
  if (s.mk !== 'total') out.mk = s.mk
  if (s.sd !== 'over') out.sd = s.sd
  if (s.ln !== null) out.ln = s.ln.toFixed(1)
  if (s.od) out.od = s.od
  return out
}
