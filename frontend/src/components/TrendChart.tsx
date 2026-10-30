import { dayLong, dayShort, dayNum, monthShort } from '../lib/format'
import { pc } from '../lib/track'
import type { WeekPoint } from '../lib/recap'
import type { CumPoint } from '../lib/history'
import { ROUTE_LABEL, dayIndex } from '../lib/history'
import type { ModelMilestone } from '../lib/types'
import { Plot, niceTicks, useWidth } from './charts'

const H = 250
const M = { l: 44, r: 16, t: 16, b: 58 }
const wk = (d: string) => `${dayNum(d)} ${monthShort(d)}`
const band = (i: number, k: number, W: number) => M.l + ((i + 0.5) * (W - M.l - M.r)) / k

function Axis({ pts, W, yTicks, y, unit, sub }: { pts: WeekPoint[]; W: number; yTicks: number[]; y: (v: number) => number; unit: string; sub: (p: WeekPoint) => [string, string] }) {
  const k = pts.length
  const every = Math.max(1, Math.ceil(k / Math.max(1, Math.floor((W - M.l - M.r) / 64))))
  return (
    <>
      <rect className="pitch" x={M.l} y={M.t} width={W - M.l - M.r} height={H - M.t - M.b} rx={6} />
      {yTicks.map((t) => (
        <g key={t}>
          <line className="grid" x1={M.l} x2={W - M.r} y1={y(t)} y2={y(t)} />
          <text className="axis" x={M.l - 8} y={y(t) + 4} textAnchor="end">{t}{unit}</text>
        </g>
      ))}
      {pts.map((p, i) => (i % every !== 0 ? null : (
        <g key={p.date}>
          <text className="axis" x={band(i, k, W)} y={H - M.b + 18} textAnchor="middle">{wk(p.date)}</text>
          <text className="axis sub" x={band(i, k, W)} y={H - M.b + 31} textAnchor="middle">{sub(p)[0]}</text>
          <text className="axis sub" x={band(i, k, W)} y={H - M.b + 43} textAnchor="middle">{sub(p)[1]}</text>
        </g>
      )))}
    </>
  )
}

/** Weekly hit rate of 70%+ calls with its 95% range, against what the model said on average. Hollow points: small samples. */
export function HitRateChart({ points, stated, n, id }: { points: WeekPoint[]; stated: number | null; n: number; id: string }) {
  const [ref, W] = useWidth(520)
  const y = (v: number) => M.t + (1 - v) * (H - M.t - M.b)
  const k = points.length
  const x = (i: number) => band(i, k, W)
  const drawn = points.filter((p) => p.rate !== null)
  const path = drawn.map((p, j) => `${j === 0 ? 'M' : 'L'}${x(points.indexOf(p))} ${y(p.rate!)}`).join(' ')
  const desc = points.map((p) => `Week of ${wk(p.date)}: ${p.rate === null ? 'no 70%+ calls' : `${p.hits} of ${p.picks} hit, ${pc(p.rate)}, 95% range ${pc(p.lo)} to ${pc(p.hi)}`}`).join('. ')
  return (
    <div ref={ref} className="chartbox" id={id}>
      <Plot
        W={W} H={H} top={M.t} bottom={H - M.b}
        title={`Weekly hit rate of 70%+ calls at ${n}+ shots`}
        desc={`${desc}. The dashed line is what the model said on average${stated === null ? ' (70% minimum)' : `, ${pc(stated)}`}.`}
        svgClass="viz trend"
        points={points.map((p, i) => ({
          x: x(i), y: p.rate === null ? null : y(p.rate),
          tip: <><b>Week of {dayLong(p.date)}</b><span>{p.rate === null ? 'No 70%+ calls' : `${p.hits} of ${p.picks} hit: ${pc(p.rate)}`}</span>{p.rate !== null && <span>95% range {pc(p.lo)}–{pc(p.hi)}</span>}{p.small && p.picks > 0 && <span className="tipwarn">Small sample</span>}</>,
        }))}
      >
        <Axis pts={points} W={W} yTicks={[0, 25, 50, 75, 100]} y={(t) => y(t / 100)} unit="%" sub={(p) => [`${p.picks} calls`, `${p.matches} matches`]} />
        {stated !== null && (
          <g>
            <line className="stated" x1={M.l} x2={W - M.r} y1={y(stated)} y2={y(stated)} />
            <text className="axis lab" x={W - M.r - 4} y={y(stated) - 6} textAnchor="end">said {pc(stated)}</text>
          </g>
        )}
        {path && <path className="tline" d={path} />}
        {points.map((p, i) => p.rate === null ? null : (
          <g key={p.date}>
            <line className="whisk" x1={x(i)} x2={x(i)} y1={y(p.lo)} y2={y(p.hi)} />
            <circle className={`tdot${p.small ? ' hollow' : ''}`} cx={x(i)} cy={y(p.rate)} r={5.5} />
          </g>
        ))}
      </Plot>
    </div>
  )
}

/** Weekly average miss in shots (bars). */
export function MissChart({ points, id }: { points: WeekPoint[]; id: string }) {
  const [ref, W] = useWidth(520)
  const vals = points.map((p) => p.mae ?? 0)
  const top = Math.max(6, Math.ceil(Math.max(...vals, 0)))
  const y = (v: number) => M.t + (1 - v / top) * (H - M.t - M.b)
  const k = points.length
  const x = (i: number) => band(i, k, W)
  const bw = Math.min(46, ((W - M.l - M.r) / Math.max(k, 1)) * 0.6)
  const ticks = [0, Math.round(top / 2), top]
  const desc = points.map((p) => `Week of ${wk(p.date)}: ${p.mae === null ? 'no figure' : `${p.mae.toFixed(1)} shots`} over ${p.matches} matches`).join('. ')
  return (
    <div ref={ref} className="chartbox" id={id}>
      <Plot
        W={W} H={H} top={M.t} bottom={H - M.b}
        title="Weekly average miss in shots" desc={`${desc}.`} svgClass="viz trend"
        points={points.map((p, i) => ({
          x: x(i), y: p.mae === null ? null : y(p.mae),
          tip: <><b>Week of {dayLong(p.date)}</b><span>{p.mae === null ? 'No figure' : `Average miss ${p.mae.toFixed(1)} shots`}</span><span>{p.matches} matches</span>{p.matches < 10 && <span className="tipwarn">Small sample</span>}</>,
        }))}
      >
        <Axis pts={points} W={W} yTicks={ticks} y={y} unit="" sub={(p) => [`${p.matches} matches`, '']} />
        {points.map((p, i) => p.mae === null ? null : (
          <g key={p.date}>
            <rect className={`tbar${p.matches < 10 ? ' hollow' : ''}`} x={x(i) - bw / 2} y={y(p.mae)} width={bw} height={Math.max(1, y(0) - y(p.mae))} rx={4} />
            <text className="dotlabel" x={x(i)} y={y(p.mae) - 6} textAnchor="middle">{p.mae.toFixed(1)}</text>
          </g>
        ))}
      </Plot>
    </div>
  )
}

export const weekLabel = (d: string) => `${dayShort(d)} ${wk(d)}`

// ----------------------------------------------------------------------------- growth of the record

const TH = 230
const TM = { l: 46, r: 18, t: 30, b: 30 }

/** A shared time scale for the growth charts: from the first point (or milestone in range) to the last point. */
export function timeScale(points: CumPoint[], W: number) {
  const d0 = dayIndex(points[0].date) - 1
  const d1 = dayIndex(points[points.length - 1].date) + 1
  const span = Math.max(1, d1 - d0)
  const x = (iso: string) => TM.l + ((dayIndex(iso) - d0) / span) * (W - TM.l - TM.r)
  const inRange = (iso: string) => dayIndex(iso) >= d0 && dayIndex(iso) <= d1
  const step = Math.max(1, Math.ceil(span / Math.max(2, Math.floor((W - TM.l - TM.r) / 70))))
  const ticks: string[] = []
  for (let d = d0 + 1; d <= d1; d += step) ticks.push(new Date(d * 86_400_000).toISOString().slice(0, 10))
  return { x, inRange, ticks }
}

function Milestones({ ms, x, inRange, top, bottom, numbered }: { ms: ModelMilestone[]; x: (iso: string) => number; inRange: (iso: string) => boolean; top: number; bottom: number; numbered: boolean }) {
  return (
    <g className="milestones">
      {ms.map((m, i) => {
        if (!inRange(m.date)) return null
        // milestones on the same day: badges side by side, one line
        const k = ms.slice(0, i).filter((o) => o.date === m.date).length
        return (
        <g key={`${m.date}-${m.route}-${m.model}`}>
          {k === 0 && <line className="mline" x1={x(m.date)} x2={x(m.date)} y1={top} y2={bottom} />}
          {numbered && (
            <g transform={`translate(${x(m.date) + k * 20} ${top - 12})`}>
              <rect className="mbadge" x={-9} y={-9} width={18} height={18} rx={9} />
              <text className="mnum" y={4} textAnchor="middle">{i + 1}</text>
            </g>
          )}
        </g>
        )
      })}
    </g>
  )
}

/** Cumulative hit rate of 70%+ calls with its Wilson 95% band, what the model said, the backfill, and milestones. */
export function CumulativeRateChart({ points, stated, backfill, milestones, n, grain }: {
  points: CumPoint[]; stated: number | null; backfill: number | null; milestones: ModelMilestone[]; n: number; grain: 'day' | 'week'
}) {
  const [ref, W] = useWidth(640)
  const { x, inRange, ticks } = timeScale(points, W)
  const drawn = points.filter((p) => p.rate !== null)
  const lows = [...drawn.map((p) => p.lo), stated ?? 1, backfill ?? 1]
  const yMin = Math.max(0, Math.floor((Math.min(...lows) - 0.05) * 10) / 10)
  const y = (v: number) => TM.t + (1 - (v - yMin) / (1 - yMin)) * (TH - TM.t - TM.b)
  const yt: number[] = []
  for (let v = yMin; v <= 1.0001; v += yMin <= 0.4 ? 0.2 : 0.1) yt.push(Math.round(v * 100) / 100)
  const line = drawn.map((p, i) => `${i ? 'L' : 'M'}${x(p.date).toFixed(1)} ${y(p.rate!).toFixed(1)}`).join(' ')
  const area = drawn.length
    ? `M${drawn.map((p) => `${x(p.date).toFixed(1)} ${y(p.hi).toFixed(1)}`).join(' L')} L${[...drawn].reverse().map((p) => `${x(p.date).toFixed(1)} ${y(p.lo).toFixed(1)}`).join(' L')} Z`
    : ''
  const last = drawn[drawn.length - 1]
  const per = grain === 'day' ? 'day' : 'week'
  const desc = `${drawn.map((p) => `${dayLong(p.date)}: ${p.cumHits} of ${p.cumPicks} hit so far, ${pc(p.rate!, 1)} (95% range ${pc(p.lo)} to ${pc(p.hi)})`).join('. ')}.`
    + `${stated !== null ? ` The model said ${pc(stated, 1)} on average.` : ''}${backfill !== null ? ` Backfill season: ${pc(backfill, 1)}.` : ''}`
    + `${milestones.length ? ` Milestones: ${milestones.map((m) => `${dayLong(m.date)} ${m.label}`).join('; ')}.` : ''}`
  return (
    <div ref={ref} className="chartbox">
      <Plot
        W={W} H={TH} top={TM.t} bottom={TH - TM.b}
        title={`Cumulative hit rate of 70%+ calls at ${n}+ shots`} desc={desc}
        points={drawn.map((p) => ({
          x: x(p.date), y: y(p.rate!),
          tip: <><b>{grain === 'day' ? dayLong(p.date) : `Week of ${dayLong(p.date)}`}</b><span>So far: {p.cumHits} of {p.cumPicks} hit, <b>{pc(p.rate!, 1)}</b></span><span>95% range {pc(p.lo)}–{pc(p.hi)}</span><span>This {per}: {p.picks === 0 ? 'no calls' : `${p.hits} of ${p.picks}`}</span></>,
        }))}
      >
        <rect className="pitch" x={TM.l} y={TM.t} width={W - TM.l - TM.r} height={TH - TM.t - TM.b} rx={6} />
        {yt.map((t) => (
          <g key={t}>
            <line className="grid" x1={TM.l} x2={W - TM.r} y1={y(t)} y2={y(t)} />
            <text className="axis" x={TM.l - 8} y={y(t) + 4} textAnchor="end">{Math.round(t * 100)}%</text>
          </g>
        ))}
        {ticks.map((t) => <text key={t} className="axis" x={x(t)} y={TH - TM.b + 18} textAnchor="middle">{wk(t)}</text>)}
        <Milestones ms={milestones} x={x} inRange={inRange} top={TM.t} bottom={TH - TM.b} numbered />
        {area && <path className="wband" d={area} />}
        {backfill !== null && <line className="backref" x1={TM.l} x2={W - TM.r} y1={y(backfill)} y2={y(backfill)} />}
        {stated !== null && <line className="stated" x1={TM.l} x2={W - TM.r} y1={y(stated)} y2={y(stated)} />}
        {line && <path className="cline" d={line} />}
        {drawn.map((p) => <circle key={p.date} className={`cdot${p.cumPicks < 30 ? ' hollow' : ''}`} cx={x(p.date)} cy={y(p.rate!)} r={3.5} />)}
        {last && <text className="dotlabel" x={x(last.date) - 6} y={y(last.rate!) - 10} textAnchor="end">{pc(last.rate!, 1)}</text>}
      </Plot>
    </div>
  )
}

/** Settled 70%+ calls, cumulative (how much evidence the record holds), with the same milestones. */
export function CumulativeCallsChart({ points, milestones, n }: { points: CumPoint[]; milestones: ModelMilestone[]; n: number }) {
  const [ref, W] = useWidth(640)
  const h = 180
  const { x, inRange, ticks } = timeScale(points, W)
  const max = Math.max(1, ...points.map((p) => p.cumPicks))
  const yt = niceTicks(max, 3)
  const top = yt[yt.length - 1]
  const y = (v: number) => TM.t + (1 - v / top) * (h - TM.t - TM.b)
  const pts = points.map((p) => `${x(p.date).toFixed(1)} ${y(p.cumPicks).toFixed(1)}`)
  const area = pts.length ? `M${x(points[0].date).toFixed(1)} ${y(0)} L${pts.join(' L')} L${x(points[points.length - 1].date).toFixed(1)} ${y(0)} Z` : ''
  const last = points[points.length - 1]
  const desc = `${points.map((p) => `${dayLong(p.date)}: ${p.cumPicks} settled calls (${p.cumHits} hits)`).join('. ')}.`
  return (
    <div ref={ref} className="chartbox">
      <Plot
        W={W} H={h} top={TM.t} bottom={h - TM.b}
        title={`Settled 70%+ calls at ${n}+ shots, cumulative`} desc={desc}
        points={points.map((p) => ({
          x: x(p.date), y: y(p.cumPicks),
          tip: <><b>{dayLong(p.date)}</b><span>{p.cumPicks} calls settled so far</span><span>{p.cumHits} hits, {p.cumPicks - p.cumHits} misses</span></>,
        }))}
      >
        <rect className="pitch" x={TM.l} y={TM.t} width={W - TM.l - TM.r} height={h - TM.t - TM.b} rx={6} />
        {yt.map((t) => (
          <g key={t}>
            <line className="grid" x1={TM.l} x2={W - TM.r} y1={y(t)} y2={y(t)} />
            <text className="axis" x={TM.l - 8} y={y(t) + 4} textAnchor="end">{t}</text>
          </g>
        ))}
        {ticks.map((t) => <text key={t} className="axis" x={x(t)} y={h - TM.b + 18} textAnchor="middle">{wk(t)}</text>)}
        <Milestones ms={milestones} x={x} inRange={inRange} top={TM.t} bottom={h - TM.b} numbered={false} />
        {area && <path className="carea" d={area} />}
        {pts.length > 0 && <path className="cline s2" d={`M${pts.join(' L')}`} />}
        {last && <text className="dotlabel" x={x(last.date) - 6} y={y(last.cumPicks) - 8} textAnchor="end">{last.cumPicks}</text>}
      </Plot>
    </div>
  )
}

/** Numbered milestone list under the growth charts (the numbers match the chart badges). */
export function MilestoneList({ ms, inRange }: { ms: ModelMilestone[]; inRange: (iso: string) => boolean }) {
  if (ms.length === 0) return null
  return (
    <ol className="mlist" aria-label="Model milestones">
      {ms.map((m, i) => (
        <li key={`${m.date}-${m.route}-${m.model}`}>
          <span className="mnum-chip" aria-hidden="true">{i + 1}</span>
          <div>
            <p className="mhead"><b>{m.label}</b> <span className="mdate">{dayLong(m.date)}{!inRange(m.date) && ' (before the chart)'}</span> <span className="tag route">{ROUTE_LABEL[m.route] ?? m.route}</span></p>
            <p className="hint">{m.detail} <code>{m.model}</code></p>
          </div>
        </li>
      ))}
    </ol>
  )
}
