// Shared app context: dataset, derived items and the URL-backed view state.
import { createContext, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore } from 'react'
import type { ReactNode } from 'react'
import { buildItems, availableDays, resolveDate } from './items'
import { buildHash, parseHash, parseState, stateToParams } from './url'
import type { Route, ViewState } from './url'
import { todayIso } from './format'
import type { Dataset, Item, League } from './types'

const NAV_EVENT = 'shots:nav'

function subscribe(cb: () => void): () => void {
  window.addEventListener('hashchange', cb)
  window.addEventListener(NAV_EVENT, cb)
  return () => {
    window.removeEventListener('hashchange', cb)
    window.removeEventListener(NAV_EVENT, cb)
  }
}
const snapshot = () => window.location.hash

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, snapshot, () => '')
  return useMemo(() => parseHash(hash), [hash])
}

export function go(hash: string, replace = false): void {
  if (replace) {
    window.history.replaceState(null, '', hash)
    window.dispatchEvent(new Event(NAV_EVENT))
  } else window.location.hash = hash
}

export interface Ctx {
  ds: Dataset
  items: Item[]
  itemById: Map<string, Item>
  leagues: Map<string, League>
  days: string[]
  today: string
  route: Route
  state: ViewState
  date: string // resolved day being shown
  totalStart: number
  teamStart: number
  /** Change view state. Filters use replace (no history spam); picking a match or a day pushes. */
  set(patch: Partial<ViewState>, opts?: { replace?: boolean; path?: string }): void
  href(patch: Partial<ViewState>, path?: string): string
}

const AppCtx = createContext<Ctx | null>(null)
export function useApp(): Ctx {
  const c = useContext(AppCtx)
  if (!c) throw new Error('useApp outside provider')
  return c
}

export function AppProvider({ ds, today, children }: { ds: Dataset; today?: string; children: ReactNode }) {
  const route = useRoute()
  const [todayStr, setToday] = useState(today ?? todayIso())
  useEffect(() => {
    if (today) return
    const t = setInterval(() => setToday(todayIso()), 60_000 * 30)
    return () => clearInterval(t)
  }, [today])

  const items = useMemo(() => buildItems(ds), [ds])
  const itemById = useMemo(() => new Map(items.map((i) => [i.id, i])), [items])
  const leagues = useMemo(() => new Map(ds.meta.leagues.map((l) => [l.id, l])), [ds])
  const days = useMemo(() => availableDays(ds.meta, items), [ds, items])
  const state = useMemo(() => parseState(route.params, ds.meta), [route.params, ds.meta])
  const date = resolveDate(state.date, days, todayStr)

  const hrefFor = useCallback(
    (patch: Partial<ViewState>, path?: string) => buildHash(path ?? route.path, stateToParams({ ...state, ...patch }, ds.meta)),
    [route.path, state, ds.meta],
  )
  const set = useCallback<Ctx['set']>(
    (patch, opts) => go(hrefFor(patch, opts?.path), opts?.replace ?? false),
    [hrefFor],
  )

  const value = useMemo<Ctx>(
    () => ({
      ds, items, itemById, leagues, days, today: todayStr, route, state, date,
      totalStart: ds.meta.thresholds.total[0], teamStart: ds.meta.thresholds.team[0], set, href: hrefFor,
    }),
    [ds, items, itemById, leagues, days, todayStr, route, state, date, set, hrefFor],
  )
  return <AppCtx.Provider value={value}>{children}</AppCtx.Provider>
}

/** matchMedia as state (false in environments without it, e.g. jsdom). */
export function useMedia(query: string): boolean {
  const sub = useCallback(
    (cb: () => void) => {
      const m = window.matchMedia?.(query)
      m?.addEventListener('change', cb)
      return () => m?.removeEventListener('change', cb)
    },
    [query],
  )
  return useSyncExternalStore(sub, () => window.matchMedia?.(query).matches ?? false, () => false)
}
