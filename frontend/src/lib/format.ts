const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function parts(iso: string): { y: number; m: number; d: number; dow: number } {
  const [y, m, d] = iso.split('-').map(Number)
  return { y, m, d, dow: new Date(Date.UTC(y, m - 1, d)).getUTCDay() }
}
export const dayShort = (iso: string): string => DAYS[parts(iso).dow]
export const dayNum = (iso: string): number => parts(iso).d
export const monthShort = (iso: string): string => MONTHS[parts(iso).m - 1]
export function dayLong(iso: string): string {
  const p = parts(iso)
  return `${DAYS[p.dow]} ${p.d} ${MONTHS[p.m - 1]}`
}

/** Local calendar date as YYYY-MM-DD (the visitor's clock). */
export function todayIso(now: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}`
}

export function utcStamp(iso: string | null | undefined): string {
  if (!iso) return 'unknown'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'unknown'
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`
}

export interface Freshness { level: 'ok' | 'stale' | 'failed'; reasons: string[]; ageHours: number | null }
export const STALE_HOURS = 48

export function freshness(
  meta: { built_at: string; last_job: { status: string } | null },
  now: number = Date.now(),
): Freshness {
  const built = Date.parse(meta.built_at)
  const ageHours = Number.isNaN(built) ? null : (now - built) / 3_600_000
  const reasons: string[] = []
  let level: Freshness['level'] = 'ok'
  if (meta.last_job && meta.last_job.status !== 'ok') {
    level = 'failed'
    reasons.push('The last update did not finish cleanly, so some numbers may be old.')
  }
  if (ageHours === null || ageHours > STALE_HOURS) {
    if (level === 'ok') level = 'stale'
    reasons.push(`Last built more than ${STALE_HOURS} hours ago.`)
  }
  return { level, reasons, ageHours }
}

/** Deterministic badge colour from a team name: same name, same colour, no club branding involved. */
export function badgeHue(name: string): number {
  let h = 2166136261
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return (h >>> 0) % 360
}
export function initials(name: string): string {
  const words = name.replace(/[^\p{L}\p{N} ]/gu, '').split(/\s+/).filter(Boolean)
  if (words.length === 0) return '?'
  if (words.length === 1) return words[0].slice(0, 2)
  return (words[0][0] + words[1][0]).toUpperCase()
}
