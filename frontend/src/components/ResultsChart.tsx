// Results page: hit rate of 70%+ calls per day or per week (bars), against what we said on average (dots).
import { useState } from 'react'
import { dayLong, dayNum, monthShort } from '../lib/format'
import { SMALL_SAMPLE, wilson } from '../lib/recap'
import type { DayGroup, Verdict } from '../lib/recap'
import { pc } from '../lib/track'
import { ChartCard, Plot, useWidth } from './charts'

export interface Bucket { date: string; matches: number; calls: number; hits: number; said: number | null }

/** Monday of the ISO week of a date (YYYY-MM-DD). */
export function weekStart(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`)
  const dow = (d.getUTCDay() + 6) % 7
  return new Date(d.getTime() - dow * 86_400_000).toISOString().slice(0, 10)
}

/** Buckets (oldest first) of the verdicts by day or by week: matches, 70%+ calls, their hits, the average stated chance. */
export function buckets(groups: DayGroup[], grain: 'day' | 'week'): Bucket[] {
  const by = new Map<string, Verdict[]>()
  for (const g of groups) {
    const k = grain === 'day' ? g.date : weekStart(g.date)
    by.set(k, [...(by.get(k) ?? []), ...g.verdicts])
  }
  return [...by.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([date, vs]) => {
    const calls = vs.filter((v) => v.call)
    const said = calls.length ? calls.reduce((a, v) => a + (v.p ?? 0), 0) / calls.length : null
    return { date, matches: vs.length, calls: calls.length, hits: calls.filter((v) => v.hit).length, said }
  })
}

const short = (d: string) => `${dayNum(d)} ${monthShort(d)}`

function Bars({ data, n, grain }: { data: Bucket[]; n: number; grain: 'day' | 'week' }) {
  const [ref, W] = useWidth(640)
  const H = 230
  const m = { l: 44, r: 12, t: 22, b: 46 }
  const k = data.length
  const bandW = (W - m.l - m.r) / Math.max(k, 1)
  const x = (i: number) => m.l + (i + 0.5) * bandW
  const y = (v: number) => m.t + (1 - v) * (H - m.t - m.b)
  const bw = Math.max(4, Math.min(34, bandW * 0.62))
  const every = Math.max(1, Math.ceil(k / Math.max(1, Math.floor((W - m.l - m.r) / 52))))
  const lab = (b: Bucket) => (grain === 'day' ? dayLong(b.date) : `Week of ${dayLong(b.date)}`)
  const desc = data.map((b) => `${lab(b)}: ${b.calls === 0 ? 'no 70%+ calls' : `${b.hits} of ${b.calls} calls hit (${pc(b.hits / b.calls)}), we said ${pc(b.said ?? 0)}`}, ${b.matches} matches`).join('. ')
  return (
    <div ref={ref} className="chartbox">
      <Plot
        W={W} H={H} top={m.t} bottom={H - m.b}
        title={`Hit rate of 70%+ calls at ${n}+ shots, ${grain === 'day' ? 'day by day' : 'week by week'}`}
        desc={`${desc}.`}
        points={data.map((b, i) => {
          const [lo, hi] = wilson(b.hits, b.calls)
          return {
            x: x(i), y: b.calls ? y(b.hits / b.calls) : null,
            tip: <><b>{lab(b)}</b>{b.calls === 0 ? <span>No 70%+ calls ({b.matches} matches)</span> : <><span>{b.hits} of {b.calls} calls hit: <b>{pc(b.hits / b.calls)}</b></span><span>We said {pc(b.said ?? 0)} on average</span><span>95% range {pc(lo)}–{pc(hi)}</span></>}{b.calls > 0 && b.calls < SMALL_SAMPLE && <span className="tipwarn">Small sample</span>}</>,
          }
        })}
      >
        <rect className="pitch" x={m.l} y={m.t} width={W - m.l - m.r} height={H - m.t - m.b} rx={6} />
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line className="grid" x1={m.l} x2={W - m.r} y1={y(t)} y2={y(t)} />
            <text className="axis" x={m.l - 8} y={y(t) + 4} textAnchor="end">{t * 100}%</text>
          </g>
        ))}
        <line className="backref" x1={m.l} x2={W - m.r} y1={y(0.7)} y2={y(0.7)} />
        {data.map((b, i) => (
          <g key={b.date}>
            {b.calls > 0 && <rect className={`tbar${b.calls < SMALL_SAMPLE ? ' hollow' : ''}`} x={x(i) - bw / 2} y={y(b.hits / b.calls)} width={bw} height={Math.max(1, y(0) - y(b.hits / b.calls))} rx={4} />}
            {b.said !== null && <circle className="said" cx={x(i)} cy={y(b.said)} r={4} />}
            {i % every === 0 && (
              <>
                <text className="axis" x={x(i)} y={H - m.b + 16} textAnchor="middle">{short(b.date)}</text>
                <text className="axis sub" x={x(i)} y={H - m.b + 29} textAnchor="middle">{b.calls}/{b.matches}</text>
              </>
            )}
          </g>
        ))}
      </Plot>
    </div>
  )
}

/** The chart card with its day / week switch. Hidden when no day has a 70%+ call. */
export function ResultsChart({ groups, n }: { groups: DayGroup[]; n: number }) {
  const [grain, setGrain] = useState<'day' | 'week'>('day')
  const data = buckets(groups, grain)
  if (!data.some((b) => b.calls > 0)) return null
  return (
    <ChartCard
      className="rchart"
      title={<>Hit rate of 70%+ calls at {n}+ shots</>}
      howto={<>each bar is the share of our 70%+ calls that reached {n}+ shots that {grain}; the dot is what we said on average. A bar at or above its dot means the call held up. Under each bar: calls / matches.</>}
      legend={[
        { label: 'Hit rate of 70%+ calls', kind: 'bar', tone: 's1' },
        { label: 'What we said (average)', kind: 'dot', tone: 'gold' },
        { label: `Under ${SMALL_SAMPLE} calls`, kind: 'barhollow', tone: 's1' },
        { label: 'The 70% line', kind: 'dash', tone: 'mut' },
      ]}
      note="One day or week is an anecdote: a handful of calls can swing either way. The track record has the long view."
      table={
        <div className="tablewrap">
          <table>
            <caption className="sr">Hit rate of 70%+ calls per {grain}</caption>
            <thead><tr><th scope="col">{grain === 'day' ? 'Day' : 'Week of'}</th><th scope="col">Matches</th><th scope="col">70%+ calls that hit</th><th scope="col">We said (avg)</th></tr></thead>
            <tbody>
              {[...data].reverse().map((b) => (
                <tr key={b.date}><th scope="row">{dayLong(b.date)}</th><td>{b.matches}</td><td>{b.calls === 0 ? 'none' : `${b.hits} of ${b.calls} (${pc(b.hits / b.calls)})`}</td><td>{b.said === null ? 'n/a' : pc(b.said)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      }
    >
      <div className="seg" role="group" aria-label="Group the chart by">
        <button type="button" className="chip" aria-pressed={grain === 'day'} onClick={() => setGrain('day')}>Daily</button>
        <button type="button" className="chip" aria-pressed={grain === 'week'} onClick={() => setGrain('week')}>Weekly</button>
      </div>
      <Bars data={data} n={n} grain={grain} />
    </ChartCard>
  )
}
