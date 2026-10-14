import { dayShort, dayNum, monthShort } from '../lib/format'
import { pc } from '../lib/track'
import type { WeekPoint } from '../lib/recap'

const W = 520, H = 250, m = { l: 42, r: 14, t: 16, b: 56 }
const x = (i: number, k: number) => m.l + ((i + 0.5) * (W - m.l - m.r)) / k
const wk = (d: string) => `${dayNum(d)} ${monthShort(d)}`

function Axis({ pts, yTicks, y, unit }: { pts: WeekPoint[]; yTicks: number[]; y: (v: number) => number; unit: string }) {
  return (
    <>
      <rect className="pitch" x={m.l} y={m.t} width={W - m.l - m.r} height={H - m.t - m.b} />
      {yTicks.map((t) => (
        <g key={t}>
          <line className="grid" x1={m.l} x2={W - m.r} y1={y(t)} y2={y(t)} />
          <text className="axis" x={m.l - 6} y={y(t) + 4} textAnchor="end">{t}{unit}</text>
        </g>
      ))}
      {pts.map((p, i) => (
        <g key={p.date}>
          <text className="axis" x={x(i, pts.length)} y={H - m.b + 20} textAnchor="middle">{wk(p.date)}</text>
          <text className="axis sub" x={x(i, pts.length)} y={H - m.b + 33} textAnchor="middle">{p.picks} calls</text>
          <text className="axis sub" x={x(i, pts.length)} y={H - m.b + 45} textAnchor="middle">{p.matches} matches</text>
        </g>
      ))}
    </>
  )
}

/** Weekly hit rate of 70%+ calls with its 95% range, against what the model said on average. Hollow points: small samples. */
export function HitRateChart({ points, stated, n, id }: { points: WeekPoint[]; stated: number | null; n: number; id: string }) {
  const y = (v: number) => m.t + (1 - v) * (H - m.t - m.b)
  const k = points.length
  const drawn = points.filter((p) => p.rate !== null)
  const path = drawn.map((p, j) => `${j === 0 ? 'M' : 'L'}${x(points.indexOf(p), k)} ${y(p.rate!)}`).join(' ')
  const desc = points.map((p) => `Week of ${wk(p.date)}: ${p.rate === null ? 'no 70%+ calls' : `${p.hits} of ${p.picks} hit, ${pc(p.rate)}, 95% range ${pc(p.lo)} to ${pc(p.hi)}`}`).join('. ')
  return (
    <svg className="trend" viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${id}-t ${id}-d`}>
      <title id={`${id}-t`}>Weekly hit rate of 70%+ calls at {n}+ shots</title>
      <desc id={`${id}-d`}>{desc}. The dashed line is what the model said on average{stated === null ? ' (70% minimum)' : `, ${pc(stated)}`}.</desc>
      <Axis pts={points} yTicks={[0, 25, 50, 75, 100]} y={(t) => y(t / 100)} unit="%" />
      {stated !== null && (
        <g>
          <line className="stated" x1={m.l} x2={W - m.r} y1={y(stated)} y2={y(stated)} />
          <text className="axis lab" x={W - m.r - 2} y={y(stated) - 5} textAnchor="end">said {pc(stated)}</text>
        </g>
      )}
      {path && <path className="tline" d={path} />}
      {points.map((p, i) => p.rate === null ? null : (
        <g key={p.date}>
          <line className="whisk" x1={x(i, k)} x2={x(i, k)} y1={y(p.lo)} y2={y(p.hi)} />
          <circle className={`tdot${p.small ? ' hollow' : ''}`} cx={x(i, k)} cy={y(p.rate)} r={5.5} />
        </g>
      ))}
    </svg>
  )
}

/** Weekly average miss in shots (bars). */
export function MissChart({ points, id }: { points: WeekPoint[]; id: string }) {
  const vals = points.map((p) => p.mae ?? 0)
  const top = Math.max(6, Math.ceil(Math.max(...vals, 0)))
  const y = (v: number) => m.t + (1 - v / top) * (H - m.t - m.b)
  const k = points.length
  const bw = Math.min(46, ((W - m.l - m.r) / Math.max(k, 1)) * 0.6)
  const ticks = [0, Math.round(top / 2), top]
  const desc = points.map((p) => `Week of ${wk(p.date)}: ${p.mae === null ? 'no figure' : `${p.mae.toFixed(1)} shots`} over ${p.matches} matches`).join('. ')
  return (
    <svg className="trend" viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${id}-t ${id}-d`}>
      <title id={`${id}-t`}>Weekly average miss in shots</title>
      <desc id={`${id}-d`}>{desc}.</desc>
      <Axis pts={points} yTicks={ticks} y={y} unit="" />
      {points.map((p, i) => p.mae === null ? null : (
        <g key={p.date}>
          <rect className={`tbar${p.matches < 10 ? ' hollow' : ''}`} x={x(i, k) - bw / 2} y={y(p.mae)} width={bw} height={Math.max(1, y(0) - y(p.mae))} rx={3} />
          <text className="dotlabel" x={x(i, k)} y={y(p.mae) - 5} textAnchor="middle">{p.mae.toFixed(1)}</text>
        </g>
      ))}
    </svg>
  )
}

export const weekLabel = (d: string) => `${dayShort(d)} ${wk(d)}`
