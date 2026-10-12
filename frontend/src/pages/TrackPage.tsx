import { useApp } from '../lib/app'
import { entryFor, pc } from '../lib/track'
import { RECORDS } from '../lib/url'
import type { RecordKind } from '../lib/url'
import { Help, Stepper, Tabs, ScrollRegion } from '../components/ui'
import type { ReliabilityBand } from '../lib/types'

function Reliability({ bands }: { bands: ReliabilityBand[] }) {
  const W = 320, H = 300, m = { l: 40, r: 12, t: 12, b: 40 }
  const x = (p: number) => m.l + p * (W - m.l - m.r)
  const y = (p: number) => m.t + (1 - p) * (H - m.t - m.b)
  const maxN = Math.max(...bands.map((b) => b.n), 1)
  const summary = bands.map((b) => `${pc(b.band[0])} to ${pc(b.band[1])}: said ${pc(b.stated, 1)}, happened ${pc(b.realised, 1)} (${b.n} matches)`).join('. ')
  return (
    <svg className="relchart" viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby="rel-t rel-d">
      <title id="rel-t">Reliability: what the model said against what happened</title>
      <desc id="rel-d">{summary}. Points on the diagonal mean perfectly calibrated.</desc>
      <rect className="pitch" x={m.l} y={m.t} width={W - m.l - m.r} height={H - m.t - m.b} />
      {[0, 0.2, 0.4, 0.6, 0.8, 1].map((p) => (
        <g key={p}>
          <line className="grid" x1={x(0)} x2={x(1)} y1={y(p)} y2={y(p)} />
          <line className="grid" x1={x(p)} x2={x(p)} y1={y(0)} y2={y(1)} />
          <text className="axis" x={m.l - 6} y={y(p) + 4} textAnchor="end">{Math.round(p * 100)}</text>
          <text className="axis" x={x(p)} y={H - m.b + 16} textAnchor="middle">{Math.round(p * 100)}</text>
        </g>
      ))}
      <line className="diag" x1={x(0)} y1={y(0)} x2={x(1)} y2={y(1)} />
      {bands.map((b) => (
        <circle key={b.band[0]} className="dot a" cx={x(b.stated)} cy={y(b.realised)} r={4 + 8 * Math.sqrt(b.n / maxN)} />
      ))}
      <text className="axis" x={(m.l + W - m.r) / 2} y={H - 6} textAnchor="middle">model said (%)</text>
      <text className="axis" transform={`translate(12 ${(m.t + H - m.b) / 2}) rotate(-90)`} textAnchor="middle">happened (%)</text>
    </svg>
  )
}

export function TrackPage() {
  const { ds, state, set, leagues } = useApp()
  const t = ds.track
  if (!t) {
    return (
      <div className="page narrow">
        <div className="card empty"><h1>Track record</h1><p>The track record file is not published yet. It appears after the next scheduled update.</p></div>
      </div>
    )
  }
  const kind: RecordKind = state.rec
  const rec = t.records[kind]
  const e = entryFor(rec, state.n)
  const [lo, hi] = ds.meta.thresholds.total
  const live = kind === 'live'
  const settled = rec.settled ?? 0

  return (
    <div className="page">
      <h1>Track record</h1>
      <p className="lede">Does &ldquo;70%&rdquo; really mean about 7 in 10? Here is every check we have, for the shot threshold you pick. <Help term="calibrated" /></p>
      <div className="card trackctl">
        <Stepper id="trk-n" label="Shots" value={state.n} min={lo} max={hi} onChange={(v) => set({ n: v }, { replace: true })} />
      </div>
      <Tabs label="Which record" tabs={RECORDS.map((r) => ({ id: r, label: r === 'backfill' ? 'Backfill (past season)' : 'Live picks' }))} value={kind} onChange={(r) => set({ rec: r }, { replace: true })}>
        <p className="recnote"><b>{rec.label}.</b> {live ? 'Saved before kick-off and settled afterwards. This is the proof that counts.' : 'The model run on matches it had not seen. A fair test, but not the same as picks saved in advance.'}</p>
        {live && (
          <p className="livebox"><b>{settled}</b> settled, <b>{rec.pending ?? 0}</b> waiting for kick-off.</p>
        )}
        {!e ? (
          <div className="card empty">
            {live && settled === 0 ? (
              <>
                <h2>0 settled so far</h2>
                <p>{rec.pending ?? 0} picks are saved and waiting for their matches. A handful of results would prove nothing either way, so we show numbers here only as they accumulate. Until then, judge the model on the backfill.</p>
                <p><a href={`#/track?${new URLSearchParams({ n: String(state.n) })}`} onClick={(ev) => { ev.preventDefault(); set({ rec: 'backfill' }, { replace: true }) }}>See the backfill</a></p>
              </>
            ) : (
              <><h2>No numbers for this record yet</h2><p>Nothing published for {state.n}+ shots.</p></>
            )}
          </div>
        ) : (
          <>
            {!e.exact && <p className="warn" role="status">No numbers for {state.n}+ in this record; showing the nearest published threshold, {e.n}+.</p>}
            <div className="stats">
              <div><span>Hit rate at 70%+ calls</span><b>{pc(e.entry.hit_rate, 1)}</b><small>{e.entry.hits.toLocaleString('en-GB')} of {e.entry.n.toLocaleString('en-GB')} matches</small></div>
              <div><span>Said on average</span><b>{pc(e.entry.mean_stated, 1)}</b><small>Hit rate at or above this is the goal</small></div>
              <div><span>95% range</span><b>{pc(e.entry.wilson95[0], 1)}&ndash;{pc(e.entry.wilson95[1], 1)}</b><small>Wilson interval on the hit rate</small></div>
              <div><span>Brier score</span><b>{e.entry.brier.toFixed(3)}</b><small>vs {e.entry.brier_baseline.toFixed(3)} for always guessing the base rate (lower is better)</small></div>
            </div>
            <div className="trackgrid">
              <section aria-labelledby="rel-h">
                <h2 id="rel-h">Reliability at {e.n}+ shots</h2>
                <Reliability bands={e.entry.reliability} />
                <p className="hint">Dots on the diagonal mean the model&rsquo;s percentages were right. Bigger dot, more matches.</p>
              </section>
              <section aria-labelledby="relt-h">
                <h2 id="relt-h">Same numbers as a table</h2>
                <ScrollRegion label="Reliability table">
                  <table>
                    <thead><tr><th scope="col">Model said</th><th scope="col">Matches</th><th scope="col">Said (avg)</th><th scope="col">Happened</th></tr></thead>
                    <tbody>
                      {e.entry.reliability.map((b) => (
                        <tr key={b.band[0]}><th scope="row">{pc(b.band[0])}&ndash;{pc(b.band[1])}</th><td>{b.n}</td><td>{pc(b.stated, 1)}</td><td>{pc(b.realised, 1)}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </ScrollRegion>
              </section>
            </div>
            {e.entry.by_league.length > 0 && (
              <section aria-labelledby="bl-h">
                <h2 id="bl-h">By league</h2>
                <ScrollRegion label="Results by league">
                  <table>
                    <thead><tr><th scope="col">League</th><th scope="col">Matches</th><th scope="col">Hit rate</th><th scope="col">Said</th></tr></thead>
                    <tbody>
                      {e.entry.by_league.map((l) => (
                        <tr key={l.league}><th scope="row">{leagues.get(l.league)?.name ?? l.league}</th><td>{l.n}</td><td>{pc(l.hit_rate, 1)}</td><td>{pc(l.stated, 1)}</td></tr>
                      ))}
                    </tbody>
                  </table>
                </ScrollRegion>
              </section>
            )}
          </>
        )}
      </Tabs>
      <p className="hint">Small samples prove nothing: judge over hundreds of matches, not a weekend. Lineups, injuries and red cards are not in the model.</p>
    </div>
  )
}
