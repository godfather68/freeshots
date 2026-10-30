// Shared chart kit: a card frame with title, a one-line "How to read this", legend chips and a table alternative,
// and an interactive plot (hover or arrow keys move a crosshair and tooltip). Hand-built SVG, no dependency.
// The SVG keeps role="img" with a title and a full text description; the tooltip is an extra for sighted users,
// the table (or the desc) carries the same numbers for everyone else.
import { useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent, ReactNode } from 'react'

export type SwatchKind = 'line' | 'dash' | 'dot' | 'hollow' | 'square' | 'bar' | 'barhollow' | 'band' | 'area' | 'milestone' | 'diag' | 'stripe'
export interface LegendItem { label: ReactNode; kind: SwatchKind; tone?: 's1' | 's2' | 's3' | 'gold' | 'mut' }

/** Legend chips: shape + colour + words, so identity never rests on colour alone. */
export function Legend({ items, label = 'Legend' }: { items: LegendItem[]; label?: string }) {
  if (items.length === 0) return null
  return (
    <ul className="vlegend" aria-label={label}>
      {items.map((it, i) => (
        <li key={i} className={`lg-item ${it.tone ?? 's1'}`}>
          <span className={`sw ${it.kind}`} aria-hidden="true" />
          <span>{it.label}</span>
        </li>
      ))}
    </ul>
  )
}

/**
 * Chart card: title, "How to read this" line and legend first (visible on landing), then the plot, then notes and
 * an optional table alternative. `as` lets a page keep its own heading level.
 */
export function ChartCard({
  title, howto, legend, children, note, table, tableLabel = 'Show the numbers as a table', className = '', headingLevel = 3, id,
}: {
  title: ReactNode
  howto: ReactNode
  legend?: LegendItem[]
  children: ReactNode
  note?: ReactNode
  table?: ReactNode
  tableLabel?: string
  className?: string
  headingLevel?: 2 | 3
  id?: string
}) {
  const uid = useId()
  const hid = id ?? `${uid}-h`
  const H = headingLevel === 2 ? 'h2' : 'h3'
  return (
    <figure className={`card chartcard ${className}`} aria-labelledby={hid}>
      <figcaption className="cc-head">
        <H id={hid} className="cc-title">{title}</H>
        <p className="howto"><span className="howto-k">How to read this</span> {howto}</p>
        {legend && <Legend items={legend} />}
      </figcaption>
      {children}
      {note && <p className="hint cc-note">{note}</p>}
      {table && (
        <details className="alt">
          <summary>{tableLabel}</summary>
          {table}
        </details>
      )}
    </figure>
  )
}

export interface PlotPoint {
  /** x in viewBox units (crosshair and nearest-point search). */
  x: number
  /** y of the highlighted mark, viewBox units (null: crosshair only). */
  y?: number | null
  tip: ReactNode
}

/** Measured width of a container (for charts that redraw at their real width instead of scaling text). */
export function useWidth(fallback = 560, min = 280): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null)
  const [w, setW] = useState(fallback)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => setW(Math.max(min, Math.round(el.clientWidth || fallback)))
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [fallback, min])
  return [ref, w]
}

/**
 * Interactive SVG plot. Pointer movement or the arrow keys (when the plot has focus) pick the nearest point; a
 * crosshair, a ring and an HTML tooltip follow it. `children` draw the marks.
 */
export function Plot({
  W, H, top = 0, bottom, points, title, desc, children, className = '', label, svgClass = 'viz',
}: {
  W: number
  H: number
  top?: number
  bottom?: number
  points: PlotPoint[]
  title: string
  desc: string
  children: ReactNode
  className?: string
  /** Accessible name of the focusable wrapper (keyboard help). */
  label?: string
  svgClass?: string
}) {
  const uid = useId()
  const svgRef = useRef<SVGSVGElement>(null)
  const [at, setAt] = useState<number | null>(null)
  const pick = (clientX: number) => {
    const svg = svgRef.current
    if (!svg || points.length === 0) return
    const r = svg.getBoundingClientRect()
    if (!r.width) return
    const vx = ((clientX - r.left) / r.width) * W
    let best = 0
    for (let i = 1; i < points.length; i++) if (Math.abs(points[i].x - vx) < Math.abs(points[best].x - vx)) best = i
    setAt(best)
  }
  const onKey = (e: KeyboardEvent) => {
    if (points.length === 0) return
    let next: number | null = null
    if (e.key === 'ArrowRight') next = at === null ? 0 : Math.min(points.length - 1, at + 1)
    else if (e.key === 'ArrowLeft') next = at === null ? points.length - 1 : Math.max(0, at - 1)
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = points.length - 1
    else if (e.key === 'Escape') { setAt(null); return }
    if (next === null) return
    e.preventDefault()
    setAt(next)
  }
  const p = at === null ? null : points[at] ?? null
  const yb = bottom ?? H
  const left = p ? (p.x / W) * 100 : 0
  /* eslint-disable jsx-a11y/no-noninteractive-tabindex, jsx-a11y/no-noninteractive-element-interactions -- the focusable group lets keyboard users step through the points */
  return (
    <div
      className={`plot ${className}`}
      role="group"
      aria-label={label ?? `${title}. Use the left and right arrow keys to read each point.`}
      tabIndex={points.length ? 0 : -1}
      onKeyDown={onKey}
      onPointerMove={(e: PointerEvent) => pick(e.clientX)}
      onPointerLeave={() => setAt(null)}
      onBlur={() => setAt(null)}
    >
      <svg ref={svgRef} className={svgClass} viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${uid}-t ${uid}-d`} preserveAspectRatio="xMidYMid meet">
        <title id={`${uid}-t`}>{title}</title>
        <desc id={`${uid}-d`}>{desc}</desc>
        {children}
        {p && (
          <g className="hoverlayer" aria-hidden="true">
            <line className="crosshair" x1={p.x} x2={p.x} y1={top} y2={yb} />
            {p.y != null && <circle className="ring" cx={p.x} cy={p.y} r={8} />}
          </g>
        )}
      </svg>
      {p && (
        <div className={`vtip${left > 62 ? ' flip' : ''}`} style={{ left: `${left}%` }} aria-live="polite">
          {p.tip}
        </div>
      )}
    </div>
  )
  /* eslint-enable jsx-a11y/no-noninteractive-tabindex, jsx-a11y/no-noninteractive-element-interactions */
}

/** Evenly spaced "nice" ticks from 0 to at least `max`. */
export function niceTicks(max: number, count = 4): number[] {
  if (max <= 0) return [0, 1]
  const raw = max / count
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((k) => k * mag).find((s) => s >= raw) ?? raw
  const out: number[] = []
  for (let v = 0; v <= max + step * 0.001; v += step) out.push(Math.round(v * 1000) / 1000)
  if (out[out.length - 1] < max) out.push(Math.round((out[out.length - 1] + step) * 1000) / 1000)
  return out
}
