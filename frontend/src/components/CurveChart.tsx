import { useEffect, useId, useRef, useState } from 'react'
import { pct, probAt } from '../lib/curve'

export interface Series { key: string; label: string; curve: number[]; start: number; cls: 'a' | 'b' }

function useWidth(): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement | null>(null)
  const [w, setW] = useState(480)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => setW(Math.max(260, Math.round(el.clientWidth || 480)))
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, w]
}

/**
 * P(shots >= n) drawn as a curve over a mown-pitch background. `marker` is the threshold being asked about;
 * `guides` are the 70/80/90% confidence lines. A data table with the same numbers sits beside it (see Panel).
 */
export function CurveChart({
  series, marker, guides = [], ticks, summary, title,
}: { series: Series[]; marker: number; guides?: number[]; ticks?: { x: number; label: string }[]; summary: string; title: string }) {
  const uid = useId()
  const gid = uid.replace(/[^a-zA-Z0-9]/g, '')
  const [ref, W] = useWidth()
  const H = 230
  const m = { l: 40, r: 38, t: 14, b: 32 }
  const first = series[0]
  const lo = first.start
  const hi = first.start + first.curve.length - 1
  const x = (n: number) => m.l + ((n - lo) / (hi - lo)) * (W - m.l - m.r)
  const y = (p: number) => m.t + (1 - p) * (H - m.t - m.b)
  const step = hi - lo > 25 ? 5 : 2
  const xt: number[] = []
  for (let n = Math.ceil(lo / step) * step; n <= hi; n += step) xt.push(n)
  const stripes: number[] = []
  for (let n = lo; n < hi; n += 2 * step) stripes.push(n)

  return (
    <div className="chart" ref={ref}>
      <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${uid}-t ${uid}-d`}>
        <defs>
          {series.map((sr) => (
            <linearGradient key={sr.key} id={`${gid}-${sr.key}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" className={`areastop ${sr.cls}`} stopOpacity={series.length > 1 ? 0.22 : 0.34} />
              <stop offset="1" className={`areastop ${sr.cls}`} stopOpacity="0.02" />
            </linearGradient>
          ))}
        </defs>
        <title id={`${uid}-t`}>{title}</title>
        <desc id={`${uid}-d`}>{summary}</desc>
        {stripes.map((n) => (
          <rect key={n} className="mow" x={x(n)} y={m.t} width={Math.min(x(n + step), x(hi)) - x(n)} height={H - m.t - m.b} />
        ))}
        <rect className="pitch" x={m.l} y={m.t} width={W - m.l - m.r} height={H - m.t - m.b} />
        {[0, 0.25, 0.5, 0.75, 1].map((p) => (
          <g key={p}>
            <line className="grid" x1={m.l} x2={W - m.r} y1={y(p)} y2={y(p)} />
            <text className="axis" x={m.l - 6} y={y(p) + 4} textAnchor="end">{Math.round(p * 100)}%</text>
          </g>
        ))}
        {xt.map((n) => (
          <text key={n} className="axis" x={x(n)} y={H - m.b + 16} textAnchor="middle">{n}</text>
        ))}
        <text className="axis" x={(m.l + W - m.r) / 2} y={H - 4} textAnchor="middle">shots (at least)</text>
        {guides.map((g) => (
          <g key={g}>
            <line className="guide" x1={m.l} x2={W - m.r} y1={y(g)} y2={y(g)} />
            <text className="guidelabel" x={W - m.r + 4} y={y(g) + 4}>{Math.round(g * 100)}%</text>
          </g>
        ))}
        {ticks?.map((t) => (
          <g key={t.label}>
            <line className="expected" x1={x(t.x)} x2={x(t.x)} y1={H - m.b - 8} y2={H - m.b} />
            <text className="axis" x={x(t.x)} y={H - m.b - 12} textAnchor="middle">{t.label}</text>
          </g>
        ))}
        {marker >= lo && marker <= hi && <line className="marker" x1={x(marker)} x2={x(marker)} y1={m.t} y2={H - m.b} />}
        {series.map((s) => (
          <path
            key={`${s.key}-area`}
            className="area"
            fill={`url(#${gid}-${s.key})`}
            d={`M${x(s.start).toFixed(1)},${y(0)} ${s.curve.map((p, i) => `L${x(s.start + i).toFixed(1)},${y(p).toFixed(1)}`).join('')} L${x(s.start + s.curve.length - 1).toFixed(1)},${y(0)} Z`}
          />
        ))}
        {series.map((s) => (
          <path
            key={s.key}
            className={`line ${s.cls}`}
            d={s.curve.map((p, i) => `${i ? 'L' : 'M'}${x(s.start + i).toFixed(1)},${y(p).toFixed(1)}`).join('')}
          />
        ))}
        {series.map((s) => {
          const p = probAt(s.curve, s.start, marker)
          if (p === null) return null
          return (
            <g key={s.key}>
              <circle className={`halo ${s.cls}`} cx={x(marker)} cy={y(p)} r={11} />
              <circle className={`dot ${s.cls}`} cx={x(marker)} cy={y(p)} r={5.5} />
              <text className={`dotlabel ${s.cls}`} x={x(marker) + (marker > (lo + hi) / 2 ? -9 : 9)} y={y(p) - 9} textAnchor={marker > (lo + hi) / 2 ? 'end' : 'start'}>
                {series.length > 1 ? `${s.label} ` : ''}{pct(p)}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}
