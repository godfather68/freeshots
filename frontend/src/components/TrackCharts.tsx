// Track record charts: calibration (reliability) and per-league accuracy.
import { pc } from '../lib/track'
import { wilson } from '../lib/recap'
import type { ReliabilityBand } from '../lib/types'
import { Plot, useWidth } from './charts'

/** Fewer matches than this in a league or band: drawn hollow and labelled as a small sample. */
export const SMALL_N = 30

/** "We said X%, it happened Y%": one bubble per probability band (size = matches), whiskers = 95% range. */
export function ReliabilityChart({ bands, n }: { bands: ReliabilityBand[]; n: number }) {
  const [ref, w] = useWidth(420)
  const W = Math.min(w, 460)
  const H = W
  const m = { l: 46, r: 14, t: 14, b: 44 }
  const lo = Math.max(0, Math.floor((Math.min(...bands.map((b) => Math.min(b.stated, b.realised)), 1) - 0.08) * 10) / 10)
  const x = (p: number) => m.l + ((p - lo) / (1 - lo)) * (W - m.l - m.r)
  const y = (p: number) => m.t + (1 - (p - lo) / (1 - lo)) * (H - m.t - m.b)
  const maxN = Math.max(...bands.map((b) => b.n), 1)
  const ticks: number[] = []
  for (let v = lo; v <= 1.0001; v += lo >= 0.5 ? 0.1 : 0.2) ticks.push(Math.round(v * 100) / 100)
  const summary = bands.map((b) => `${pc(b.band[0])} to ${pc(b.band[1])}: said ${pc(b.stated, 1)}, happened ${pc(b.realised, 1)} (${b.n} matches)`).join('. ')
  const rng = (b: ReliabilityBand) => wilson(Math.round(b.realised * b.n), b.n)
  // the diagonal label sits along the line, rotated by its on-screen angle
  const ang = (Math.atan2(y(1) - y(lo), x(1) - x(lo)) * 180) / Math.PI
  const lx = x(lo + (1 - lo) * 0.3), ly = y(lo + (1 - lo) * 0.3)
  return (
    <div ref={ref} className="chartbox relbox">
      <Plot
        W={W} H={H} top={m.t} bottom={H - m.b} svgClass="viz relchart"
        title={`Reliability at ${n}+ shots: what the model said against what happened`}
        desc={`${summary}. Points on the diagonal mean perfectly calibrated.`}
        points={[...bands].sort((a, b) => a.stated - b.stated).map((b) => {
          const [l, h] = rng(b)
          return {
            x: x(b.stated), y: y(b.realised),
            tip: <><b>Said {pc(b.band[0])}–{pc(b.band[1])}</b><span>Said {pc(b.stated, 1)} on average</span><span>Happened <b>{pc(b.realised, 1)}</b> (95% range {pc(l)}–{pc(h)})</span><span>{b.n.toLocaleString('en-GB')} matches</span>{b.n < SMALL_N && <span className="tipwarn">Small sample</span>}</>,
          }
        })}
      >
        <rect className="pitch" x={m.l} y={m.t} width={W - m.l - m.r} height={H - m.t - m.b} rx={6} />
        <path className="overzone" d={`M${x(lo)} ${y(lo)} L${x(lo)} ${y(1)} L${x(1)} ${y(1)} Z`} />
        {ticks.map((p) => (
          <g key={p}>
            <line className="grid" x1={x(lo)} x2={x(1)} y1={y(p)} y2={y(p)} />
            <line className="grid" x1={x(p)} x2={x(p)} y1={y(lo)} y2={y(1)} />
            <text className="axis" x={m.l - 8} y={y(p) + 4} textAnchor="end">{Math.round(p * 100)}%</text>
            <text className="axis" x={x(p)} y={H - m.b + 16} textAnchor="middle">{Math.round(p * 100)}%</text>
          </g>
        ))}
        <text className="axis zone" x={x(lo) + 8} y={y(1) + 16}>happened more often than said</text>
        <text className="axis zone" x={x(1) - 8} y={y(lo) - 8} textAnchor="end">less often than said</text>
        <line className="diag" x1={x(lo)} y1={y(lo)} x2={x(1)} y2={y(1)} />
        <text className="axis diaglabel" transform={`translate(${lx} ${ly - 7}) rotate(${ang})`} textAnchor="middle">perfectly calibrated</text>
        {bands.map((b) => {
          const [l, h] = rng(b)
          return <line key={`w${b.band[0]}`} className="whisk" x1={x(b.stated)} x2={x(b.stated)} y1={y(Math.max(lo, l))} y2={y(h)} />
        })}
        {bands.map((b) => (
          <circle key={b.band[0]} className={`dot a${b.n < SMALL_N ? ' hollow' : ''}`} cx={x(b.stated)} cy={y(b.realised)} r={4 + 9 * Math.sqrt(b.n / maxN)} />
        ))}
        <text className="axis lab" x={(m.l + W - m.r) / 2} y={H - 8} textAnchor="middle">what the model said</text>
        <text className="axis lab" transform={`translate(12 ${(m.t + H - m.b) / 2}) rotate(-90)`} textAnchor="middle">what happened</text>
      </Plot>
    </div>
  )
}

export interface LeagueRow { id: string; name: string; n: number; hit_rate: number; stated: number }

/** Hit rate per league, highest first, with what the model said as a tick and the sample size on every row. */
export function LeagueBars({ rows, n }: { rows: LeagueRow[]; n: number }) {
  const sorted = [...rows].sort((a, b) => b.hit_rate - a.hit_rate || b.n - a.n || a.name.localeCompare(b.name))
  const label = `Hit rate of 70%+ calls by league at ${n}+ shots, highest first. ${sorted.map((r) => `${r.name}: ${pc(r.hit_rate, 1)} of ${r.n} calls, said ${pc(r.stated, 1)}`).join('. ')}.`
  return (
    <div className="lbars" role="img" aria-label={label}>
      <div className="lb-scale" aria-hidden="true"><span>0%</span><span>50%</span><span>100%</span></div>
      <ul aria-hidden="true">
        {sorted.map((r) => {
          const small = r.n < SMALL_N
          const above = r.hit_rate >= r.stated
          return (
            <li key={r.id} className={small ? 'small' : ''} title={`${r.name}: ${pc(r.hit_rate, 1)} hit of ${r.n} calls; said ${pc(r.stated, 1)}`}>
              <span className="lb-name">{r.name}</span>
              <span className="lb-track">
                <i className={`lb-fill${above ? ' ok' : ' under'}`} style={{ width: `${r.hit_rate * 100}%` }} />
                <i className="lb-said" style={{ left: `${r.stated * 100}%` }} />
              </span>
              <span className="lb-val"><b>{pc(r.hit_rate)}</b> <small>{above ? '▲' : '▼'} n={r.n}{small ? ' · small' : ''}</small></span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
