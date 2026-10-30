import type { TimeMode } from './time'
import type { Check, OddsFormat } from './edge'
// Tiny persisted stores (localStorage in try/catch, in-memory fallback) usable with useSyncExternalStore.
export interface Store<T> {
  get(): T
  set(v: T): void
  subscribe(cb: () => void): () => void
  /** Drop the cached value and re-read from localStorage (used by tests). */
  reload(): void
}

export function persisted<T>(key: string, initial: T): Store<T> {
  let cache: T | undefined
  const subs = new Set<() => void>()
  const read = (): T => {
    try {
      const raw = localStorage.getItem(key)
      if (raw !== null) return JSON.parse(raw) as T
    } catch {
      /* storage blocked or corrupt: fall through to default */
    }
    return initial
  }
  return {
    get() {
      if (cache === undefined) cache = read()
      return cache
    },
    set(v) {
      cache = v
      try {
        localStorage.setItem(key, JSON.stringify(v))
      } catch {
        /* private mode: keep it in memory for this visit */
      }
      subs.forEach((cb) => cb())
    },
    subscribe(cb) {
      subs.add(cb)
      return () => subs.delete(cb)
    },
    reload() {
      cache = undefined
      subs.forEach((cb) => cb())
    },
  }
}

export interface Favourites { teams: string[]; matches: string[] }
export const favouritesStore = persisted<Favourites>('shots.fav', { teams: [], matches: [] })
export const collapsedStore = persisted<string[]>('shots.collapsed', [])
export const timeModeStore = persisted<TimeMode>('shots.timemode', 'mine')
export const oddsFormatStore = persisted<OddsFormat>('shots.oddsformat', 'decimal')
export const checksStore = persisted<Check[]>('shots.checks', [])
export const guideStore = persisted<{ dismissed: boolean }>('shots.guide', { dismissed: false })
/** Colour theme: 'system' follows prefers-color-scheme; 'light' / 'dark' are the visitor's explicit choice. */
export type Theme = 'system' | 'light' | 'dark'
export const themeStore = persisted<Theme>('shots.theme', 'system')

/** Stamp the theme on <html data-theme> (absent = follow the system). Safe outside a browser. */
export function applyTheme(t: Theme): void {
  if (typeof document === 'undefined') return
  const el = document.documentElement
  if (t === 'light' || t === 'dark') el.dataset.theme = t
  else delete el.dataset.theme
}

export function toggleIn(list: string[], v: string): string[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v]
}

export function resetStores(): void {
  for (const s of [favouritesStore, collapsedStore, guideStore, timeModeStore, oddsFormatStore, checksStore, themeStore]) s.reload()
}
