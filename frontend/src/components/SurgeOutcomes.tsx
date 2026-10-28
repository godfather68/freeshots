// How surge candidates turned out: weekly surge rate of settled matches with 1+ boost signal vs assessed matches
// without one (results.json `surge`). Same chart style as the Track page (components/TrendChart), with a table.
import { dayNum, monthShort } from '../lib/format'
import { pc } from '../lib/track'
import { SURGE_SMALL, tallyRate } from '../lib/surge'
import type { OutcomeWeek, Tally } from '../lib/surge'

const W = 520, H = 250, m = { l: 42, r: 14, t: 16, b: 56 }
const x = (i: number, k: number) => m.l + ((i + 0.5) * (W - m.l - m.r)) / k
const y = (v: number) => m.t + (1 - v) * (H - m.t - m.b)
const wk = (d: string) => `${dayNum(d)} ${monthShort(d)}`
const OFF = 7 // the two series side by side within a week

export const tallyText = (t: Tally): string => (t.n === 0 ? 'none' : `${t.surged} of ${t.n} (${pc(tallyRate(t)!)})`)

function Series({ weeks, pick, cls, dx }: { weeks: OutcomeWeek[]; pick: (w: OutcomeWeek) => Tally; cls: 'flag' | 'rest'; dx: number }) {
  const k = weeks.length
  const pts = weeks.map((w, i) => ({ i, t: pick(w) })).filter((p) => p.t.n > 0)
  const path = pts.map((p, j) => `${j === 0 ? 'M' : 'L'}${x(p.i, k) + dx} ${y(tallyRate(p.t)!)}`).join(' ')
  return (
    <g className={`sseries ${cls}`}>
      {path && <path className="tline" d={path} />}
      {pts.map(({ i, t }) => {
        const cx = x(i, k) + dx, cy = y(tallyRate(t)!), small = t.n < SURGE_SMALL
        return cls === 'flag'
          ? <circle key={i} className={`tdot${small ? ' hollow' : ''}`} cx={cx} cy={cy} r={5.5} />
          : <rect key={i} className={`tsq${small ? ' hollow' : ''}`} x={cx - 5} y={cy - 5} width={10} height={10} />
      })}
    </g>
  )
}

/** Weekly surge rate: circles = matches with 1+ signal, squares = assessed matches without; hollow = fewer than 10. */
export function SurgeOutcomeChart({ weeks, usual, margin, id }: { weeks: OutcomeWeek[]; usual: number; margin: number; id: string }) {
  const desc = weeks.map((w) => `Week of ${wk(w.date)}: with a signal ${tallyText(w.flagged)} surged; without ${tallyText(w.other)}`).join('. ')
  return (
    <svg className="trend" viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={`${id}-t ${id}-d`}>
      <title id={`${id}-t`}>Weekly share of settled matches that ended {margin}+ shots above our expected total</title>
      <desc id={`${id}-d`}>{desc}. The dashed line is the usual share, {pc(usual)}.</desc>
      <rect className="pitch" x={m.l} y={m.t} width={W - m.l - m.r} height={H - m.t - m.b} />
      {[0, 25, 50, 75, 100].map((t) => (
        <g key={t}>
          <line className="grid" x1={m.l} x2={W - m.r} y1={y(t / 100)} y2={y(t / 100)} />
          <text className="axis" x={m.l - 6} y={y(t / 100) + 4} textAnchor="end">{t}%</text>
        </g>
      ))}
      {weeks.map((w, i) => (
        <g key={w.date}>
          <text className="axis" x={x(i, weeks.length)} y={H - m.b + 20} textAnchor="middle">{wk(w.date)}</text>
          <text className="axis sub" x={x(i, weeks.length)} y={H - m.b + 33} textAnchor="middle">{w.flagged.n} flagged</text>
          <text className="axis sub" x={x(i, weeks.length)} y={H - m.b + 45} textAnchor="middle">{w.other.n} others</text>
        </g>
      ))}
      <line className="stated" x1={m.l} x2={W - m.r} y1={y(usual)} y2={y(usual)} />
      <text className="axis lab" x={W - m.r - 2} y={y(usual) - 5} textAnchor="end">usual {pc(usual)}</text>
      <Series weeks={weeks} pick={(w) => w.other} cls="rest" dx={OFF} />
      <Series weeks={weeks} pick={(w) => w.flagged} cls="flag" dx={-OFF} />
    </svg>
  )
}
