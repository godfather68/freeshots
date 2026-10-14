// Kick-off time helpers. Everything is derived from the UTC instant with Intl.DateTimeFormat (no manual offset
// arithmetic, so DST changes are handled by the platform's tz database). The zone is always injectable.
export type TimeMode = 'mine' | 'venue'

export interface KickoffSource { date: string; kickoff_local?: string | null; kickoff_utc?: string | null }
export interface Kickoff {
  /** Calendar date to group by (YYYY-MM-DD) in the active view. */
  date: string
  /** 24-hour HH:mm in the active view, or null when no time is published. */
  time: string | null
  /** True when `time` is the venue's local time (venue mode, or no UTC instant to convert). */
  venueTime: boolean
  /** UTC instant in ms, or null when unknown. */
  utc: number | null
  venueDate: string
  venueClock: string | null
}

export function browserZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

export function isValidZone(zone: string): boolean {
  try {
    new Intl.DateTimeFormat('en-GB', { timeZone: zone })
    return true
  } catch {
    return false
  }
}

const cache = new Map<string, Intl.DateTimeFormat>()
function fmt(zone: string, kind: 'wall' | 'offset'): Intl.DateTimeFormat {
  const key = `${kind}|${zone}`
  let f = cache.get(key)
  if (!f) {
    f = kind === 'wall'
      ? new Intl.DateTimeFormat('en-GB', { timeZone: zone, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
      : new Intl.DateTimeFormat('en-GB', { timeZone: zone, timeZoneName: 'longOffset', hour: '2-digit' })
    cache.set(key, f)
  }
  return f
}

export interface Wall { date: string; time: string; month: number; day: number }
/** Wall-clock date and 24-hour time of a UTC instant in a zone. */
export function wallClock(utcMs: number, zone: string): Wall {
  const p: Record<string, string> = {}
  for (const x of fmt(zone, 'wall').formatToParts(new Date(utcMs))) p[x.type] = x.value
  const hour = p.hour === '24' ? '00' : p.hour
  return { date: `${p.year}-${p.month}-${p.day}`, time: `${hour}:${p.minute}`, month: Number(p.month), day: Number(p.day) }
}

/** "UTC+1", "UTC-4", "UTC+5:30", "UTC" for the zone at that instant. */
export function offsetLabel(utcMs: number, zone: string): string {
  const name = fmt(zone, 'offset').formatToParts(new Date(utcMs)).find((x) => x.type === 'timeZoneName')?.value ?? ''
  const m = /([+\u2212-])(\d{1,2})(?::(\d{2}))?/.exec(name)
  if (!m) return 'UTC'
  const sign = m[1] === '+' ? '+' : '-'
  const h = Number(m[2])
  const min = m[3] && m[3] !== '00' ? `:${m[3]}` : ''
  return h === 0 && !min ? 'UTC' : `UTC${sign}${h}${min}`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
/** "13 Oct 18:45 UTC+1" for a UTC instant in a zone ("unknown" if missing or invalid). */
export function stampIn(iso: string | null | undefined, zone: string): string {
  const ms = iso ? Date.parse(iso) : NaN
  if (Number.isNaN(ms)) return 'unknown'
  const w = wallClock(ms, zone)
  return `${w.day} ${MONTHS[w.month - 1]} ${w.time} ${offsetLabel(ms, zone)}`
}

/** The visitor's calendar date for an instant. */
export const dateIn = (utcMs: number, zone: string): string => wallClock(utcMs, zone).date

const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/
export function kickoffView(src: KickoffSource, zone: string, mode: TimeMode): Kickoff {
  const utc = src.kickoff_utc ? Date.parse(src.kickoff_utc) : NaN
  const ms = Number.isNaN(utc) ? null : utc
  const clock = src.kickoff_local && CLOCK.test(src.kickoff_local) ? src.kickoff_local : null
  if (ms === null || mode === 'venue') {
    return { date: src.date, time: clock, venueTime: clock !== null, utc: ms, venueDate: src.date, venueClock: clock }
  }
  const w = wallClock(ms, zone)
  return { date: w.date, time: w.time, venueTime: false, utc: ms, venueDate: src.date, venueClock: clock }
}
