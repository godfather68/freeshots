import { useId, useRef, useSyncExternalStore } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { BAND_WORD, band, pct } from '../lib/curve'
import { badgeHue, initials } from '../lib/format'
import { TERMS } from '../lib/glossary'
import type { Store } from '../lib/storage'

export function useStore<T>(s: Store<T>): T {
  return useSyncExternalStore(s.subscribe, s.get, s.get)
}

/** Initial-letter team badge with a deterministic colour. No club crests. */
export function Badge({ name, size = 22 }: { name: string; size?: number }) {
  const h = badgeHue(name)
  return (
    <span
      className="tbadge"
      aria-hidden="true"
      style={{ width: size, height: size, fontSize: size * 0.42, background: `hsl(${h} 42% 30%)` }}
    >
      {initials(name)}
    </span>
  )
}

const TRI: Record<string, { c: string[]; dir: 'v' | 'h'; w?: number[] }> = {
  France: { c: ['#2b4fa8', '#f4f4f0', '#c8323a'], dir: 'v' },
  Italy: { c: ['#2f8a4a', '#f4f4f0', '#c8323a'], dir: 'v' },
  Netherlands: { c: ['#b8353c', '#f4f4f0', '#2b4fa8'], dir: 'h' },
  Germany: { c: ['#222', '#c8323a', '#e8b923'], dir: 'h' },
  Belgium: { c: ['#222', '#e8b923', '#c8323a'], dir: 'v' },
  Spain: { c: ['#b8353c', '#e8b923', '#b8353c'], dir: 'h', w: [1, 2, 1] },
  Portugal: { c: ['#2f7a45', '#c8323a'], dir: 'v', w: [2, 3] },
}

/** Small drawn flags; unknown countries get a neutral pitch glyph instead of a guess. */
export function Flag({ country }: { country: string }) {
  const t = TRI[country]
  const box = { width: 18, height: 12, viewBox: '0 0 18 12', 'aria-hidden': true as const, focusable: false as const, className: 'flag' }
  if (country === 'England') {
    return (
      <svg {...box}><rect width="18" height="12" fill="#f4f4f0" /><rect x="7.5" width="3" height="12" fill="#c8323a" /><rect y="4.5" width="18" height="3" fill="#c8323a" /></svg>
    )
  }
  if (country === 'Scotland') {
    return (
      <svg {...box}><rect width="18" height="12" fill="#2b4fa8" /><path d="M0 0L18 12M18 0L0 12" stroke="#f4f4f0" strokeWidth="2" /></svg>
    )
  }
  if (t) {
    const w = t.w ?? t.c.map(() => 1)
    const sum = w.reduce((a, b) => a + b, 0)
    let at = 0
    return (
      <svg {...box}>
        {t.c.map((c, i) => {
          const len = ((t.dir === 'v' ? 18 : 12) * w[i]) / sum
          const r = t.dir === 'v' ? <rect key={i} x={at} width={len} height="12" fill={c} /> : <rect key={i} y={at} width="18" height={len} fill={c} />
          at += len
          return r
        })}
      </svg>
    )
  }
  return (
    <svg {...box}><rect x=".5" y=".5" width="17" height="11" rx="1.5" fill="none" stroke="currentColor" /><line x1="9" y1=".5" x2="9" y2="11.5" stroke="currentColor" /><circle cx="9" cy="6" r="2.4" fill="none" stroke="currentColor" /></svg>
  )
}

/** Probability pill: number plus a mown-pitch bar with a tick at 70%. State is in the form, not only the colour. */
export function ProbPill({ p, label }: { p: number | null; label?: string }) {
  const b = band(p)
  return (
    <span className={`pill ${b}`} title={p === null ? undefined : `${BAND_WORD[b]}`}>
      <b>{pct(p)}</b>
      <span className="pbar" aria-hidden="true"><i style={{ width: `${Math.round((p ?? 0) * 100)}%` }} /></span>
      {label ? <span className="sr">{label}</span> : null}
    </span>
  )
}

export function Help({ term }: { term: keyof typeof TERMS }) {
  const [name, text] = TERMS[term]
  const ref = useRef<HTMLDetailsElement>(null)
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape' && ref.current?.open) {
      e.stopPropagation()
      ref.current.open = false
      ref.current.querySelector('summary')?.focus()
    }
  }
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <details className="term" ref={ref} onKeyDown={onKey}>
      <summary>
        <span className="sr">What does &ldquo;{name}&rdquo; mean?</span>
        <span aria-hidden="true">?</span>
      </summary>
      <span className="pop" role="note">
        <b>{name}.</b> {text} <a href="#/how">All terms</a>
      </span>
    </details>
  )
}

export interface TabDef<T extends string> { id: T; label: string }
/** ARIA tabs with roving tabindex and arrow-key navigation (automatic activation). */
export function Tabs<T extends string>({
  tabs, value, onChange, label, children, className,
}: { tabs: TabDef<T>[]; value: T; onChange: (id: T) => void; label: string; children: ReactNode; className?: string }) {
  const uid = useId()
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const onKey = (e: KeyboardEvent) => {
    const i = tabs.findIndex((t) => t.id === value)
    let next = -1
    if (e.key === 'ArrowRight') next = (i + 1) % tabs.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = tabs.length - 1
    if (next < 0) return
    e.preventDefault()
    onChange(tabs[next].id)
    refs.current[tabs[next].id]?.focus()
  }
  return (
    <div className={className}>
      {/* eslint-disable-next-line jsx-a11y/interactive-supports-focus -- focus lives on the tab buttons (roving tabindex) */}
      <div role="tablist" aria-label={label} className="tabs" onKeyDown={onKey}>
        {tabs.map((t) => (
          <button
            key={t.id}
            ref={(el) => { refs.current[t.id] = el }}
            type="button"
            role="tab"
            id={`${uid}-t-${t.id}`}
            aria-selected={t.id === value}
            aria-controls={`${uid}-p-${t.id}`}
            tabIndex={t.id === value ? 0 : -1}
            onClick={() => onChange(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`${uid}-p-${value}`} aria-labelledby={`${uid}-t-${value}`} className="tabpanel">
        {children}
      </div>
    </div>
  )
}

/** −/slider/+ control for a whole-number threshold. */
export function Stepper({
  id, label, value, min, max, onChange, suffix = '+', help,
}: { id: string; label: string; value: number; min: number; max: number; onChange: (n: number) => void; suffix?: string; help?: ReactNode }) {
  return (
    <div className="stepper">
      <label htmlFor={id} className="lbl">{label} {help}</label>
      <div className="steprow">
        <button type="button" className="sq" aria-label={`Decrease ${label}`} disabled={value <= min} onClick={() => onChange(value - 1)}>&minus;</button>
        <input id={id} type="range" min={min} max={max} step={1} value={value} onChange={(e) => onChange(Number(e.target.value))} aria-valuetext={`${value}${suffix}`} />
        <button type="button" className="sq" aria-label={`Increase ${label}`} disabled={value >= max} onClick={() => onChange(value + 1)}>+</button>
        <output htmlFor={id}>{value}{suffix}</output>
      </div>
    </div>
  )
}

/** Scrollable table container: focusable so keyboard users can scroll it (axe: scrollable-region-focusable). */
export function ScrollRegion({ label, children }: { label: string; children: ReactNode }) {
  return (
    // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <div className="tablewrap" tabIndex={0} role="region" aria-label={label}>
      {children}
    </div>
  )
}
