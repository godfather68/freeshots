// How surge candidates turned out: weekly surge rate of settled matches with 1+ boost signal vs assessed matches
// without one (results.json `surge`). Same chart kit as the Track page (components/charts), with a table.
import { dayLong, dayNum, monthShort } from '../lib/format'
import { pc } from '../lib/track'
import { SURGE_SMALL, tallyRate } from '../lib/surge'
import type { OutcomeWeek, Tally } from '../lib/surge'
import { Plot, useWidth } from './charts'

const H = 250, m = { l: 44, r: 16, t: 16, b: 58 }
const wk = (d: string) => `${dayNum(d)} ${monthShort(d)}`
const OFF = 7 // the two series side by side within a week

export const tallyText = (t: Tally): string => (t.n === 0 ? 'none' : `${t.surged} of ${t.n} (${pc(tallyRate(t)!)})`)

function Series({ weeks, pick, cls, dx, x, y }: { weeks: OutcomeWeek[]; pick: (w: OutcomeWeek) => Tally; cls: 'flag' | 'rest'; dx: number; x: (i: number) => number; y: (v: number) => number }) {
  const pts = weeks.map((w, i) => ({ i, t: pick(w) })).filter((p) => p.t.n > 0)
  const path = pts.map((p, j) => `${j === 0 ? 'M' : 'L'}${x(p.i) + dx} ${y(tallyRate(p.t)!)}`).join(' ')
  return (
    <g className={`sseries ${cls}`}>
      {path && <path className="tline" d={path} />}
      {pts.map(({ i, t }) => {
        const cx = x(i) + dx, cy = y(tallyRate(t)!), small = t.n < SURGE_SMALL
        return cls === 'flag'
          ? <circle key={i} className={`tdot${small ? ' hollow' : ''}`} cx={cx} cy={cy} r={5.5} />
          : <rect key={i} className={`tsq${small ? ' hollow' : ''}`} x={cx - 5} y={cy - 5} width={10} height={10} />
      })}
    </g>
  )
}

/** Weekly surge rate: circles = matches with 1+ signal, squares = assessed matches without; hollow = fewer than 10. */
export function SurgeOutcomeChart({ weeks, usual, margin, id }: { weeks: OutcomeWeek[]; usual: number; margin: number; id: string }) {
  const [ref, W] = useWidth(560)
  const k = weeks.length
  const x = (i: number) => m.l + ((i + 0.5) * (W - m.l - m.r)) / k
  const y = (v: number) => m.t + (1 - v) * (H - m.t - m.b)
  const every = Math.max(1, Math.ceil(k / Math.max(1, Math.floor((W - m.l - m.r) / 64))))
  const desc = weeks.map((w) => `Week of ${wk(w.date)}: with a signal ${tallyText(w.flagged)} surged; without ${tallyText(w.other)}`).join('. ')
  return (
    <div ref={ref} className="chartbox" id={id}>
      <Plot
        W={W} H={H} top={m.t} bottom={H - m.b} svgClass="viz trend"
        title={`Weekly share of settled matches that ended ${margin}+ shots above our expected total`}
        desc={`${desc}. The dashed line is the usual share, ${pc(usual)}.`}
        points={weeks.map((w, i) => ({
          x: x(i), y: w.flagged.n ? y(tallyRate(w.flagged)!) - 0 : null,
          tip: <><b>Week of {dayLong(w.date)}</b><span>With a signal: {tallyText(w.flagged)}</span><span>Without: {tallyText(w.other)}</span>{(w.flagged.n > 0 && w.flagged.n < SURGE_SMALL) && <span className="tipwarn">Small sample</span>}</>,
        }))}
      >
        <rect className="pitch" x={m.l} y={m.t} width={W - m.l - m.r} height={H - m.t - m.b} rx={6} />
        {[0, 25, 50, 75, 100].map((t) => (
          <g key={t}>
            <line className="grid" x1={m.l} x2={W - m.r} y1={y(t / 100)} y2={y(t / 100)} />
            <text className="axis" x={m.l - 8} y={y(t / 100) + 4} textAnchor="end">{t}%</text>
          </g>
        ))}
        {weeks.map((w, i) => (i % every !== 0 ? null : (
          <g key={w.date}>
            <text className="axis" x={x(i)} y={H - m.b + 18} textAnchor="middle">{wk(w.date)}</text>
            <text className="axis sub" x={x(i)} y={H - m.b + 31} textAnchor="middle">{w.flagged.n} flagged</text>
            <text className="axis sub" x={x(i)} y={H - m.b + 43} textAnchor="middle">{w.other.n} others</text>
          </g>
        )))}
        <line className="stated" x1={m.l} x2={W - m.r} y1={y(usual)} y2={y(usual)} />
        <text className="axis lab" x={W - m.r - 4} y={y(usual) - 6} textAnchor="end">usual {pc(usual)}</text>
        <Series weeks={weeks} pick={(w) => w.other} cls="rest" dx={OFF} x={x} y={y} />
        <Series weeks={weeks} pick={(w) => w.flagged} cls="flag" dx={-OFF} x={x} y={y} />
      </Plot>
    </div>
  )
}
