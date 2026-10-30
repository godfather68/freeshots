import { useApp } from '../lib/app'
import { entryFor, pc } from '../lib/track'
import { RECORDS } from '../lib/url'
import type { RecordKind } from '../lib/url'
import { Help, Stepper, Tabs, ScrollRegion } from '../components/ui'
import type { Track, TrackRecord } from '../lib/types'
import { weeklyPoints } from '../lib/recap'
import { cumulative, milestonesFor } from '../lib/history'
import { CumulativeCallsChart, CumulativeRateChart, HitRateChart, MilestoneList, MissChart, timeScale } from '../components/TrendChart'
import { LeagueBars, ReliabilityChart, SMALL_N } from '../components/TrackCharts'
import { ChartCard } from '../components/charts'
import { dayLong } from '../lib/format'

const EMPTY_WEEKLY = {
  live: 'Weekly points appear once matches with a saved prediction have been settled. football-data publishes results about twice a week, so the first weeks fill in gradually.',
  experimental: 'European cup results are entered by hand after each matchday, so weekly points appear a little after the matches. None have been entered yet.',
}

/** Growth of the record: cumulative hit rate (with its 95% band) and settled calls, annotated with model milestones. */
function Growth({ rec, kind, n, track }: { rec: TrackRecord; kind: 'live' | 'experimental'; n: number; track: Track }) {
  const { ds } = useApp()
  const { points, grain, usedN } = cumulative(rec, n)
  const xp = kind === 'experimental'
  const ms = milestonesFor(ds.meta.model_history, kind)
  const shownN = usedN ?? n
  const stated = entryFor(rec, shownN)?.entry.mean_stated ?? null
  const back = entryFor(track.records.backfill ?? undefined, shownN)
  const backfill = !xp && back?.exact ? back.entry.hit_rate : null
  const withCalls = points.filter((p) => p.cumPicks > 0)
  return (
    <section className="growth" aria-labelledby={`gr-${kind}-h`}>
      <h2 id={`gr-${kind}-h`}>Growth of the record{xp && <> <span className="tag xp">Experimental</span></>}</h2>
      <p className="hint">Every 70%+ call {xp ? 'on the European cups' : 'of the official record'} since it began, added up {grain === 'day' ? 'day by day' : 'week by week (the daily points no longer reach back to the start)'}. The numbered lines mark when a model changed.</p>
      {withCalls.length === 0 ? (
        <div className="card empty"><h3>No settled calls to chart yet</h3><p>{EMPTY_WEEKLY[kind]}</p></div>
      ) : (
        <>
          {usedN !== null && usedN !== n && <p className="warn" role="status">No growth numbers for {n}+; showing the nearest published threshold, {usedN}+.</p>}
          <ChartCard
            title={<>Hit rate so far, at {shownN}+ shots</>}
            howto={<>the green line is the share of all 70%+ calls so far that reached {shownN}+ shots; the shaded band is where the true rate plausibly lies (95%). Staying on or above the dashed line means the percentages held up.</>}
            legend={[
              { label: 'Hit rate so far', kind: 'line', tone: 's1' },
              { label: '95% range', kind: 'band', tone: 's1' },
              ...(stated !== null ? [{ label: `What we said (${pc(stated)})`, kind: 'dash' as const, tone: 'gold' as const }] : []),
              ...(backfill !== null ? [{ label: `Backfill season (${pc(backfill)})`, kind: 'dash' as const, tone: 'mut' as const }] : []),
              ...(ms.length ? [{ label: 'Model change', kind: 'milestone' as const, tone: 'gold' as const }] : []),
              { label: `Hollow: under ${SMALL_N} calls so far`, kind: 'hollow' as const, tone: 's1' as const },
            ]}
            note={backfill !== null ? 'The backfill is one past season the model had not seen, scored in one go: a reference level, not part of this record.' : undefined}
          >
            <CumulativeRateChart points={withCalls} stated={stated} backfill={backfill} milestones={ms} n={shownN} grain={grain} />
          </ChartCard>
          <ChartCard
            title="Evidence so far: settled 70%+ calls"
            howto={<>how many calls the hit rate above rests on. The more calls, the narrower its band and the more the rate means.</>}
            legend={[{ label: 'Settled calls, cumulative', kind: 'area', tone: 's2' }, ...(ms.length ? [{ label: 'Model change', kind: 'milestone' as const, tone: 'gold' as const }] : [])]}
          >
            <CumulativeCallsChart points={points} milestones={ms} n={shownN} />
          </ChartCard>
          <MilestoneList ms={ms} inRange={timeScale(withCalls, 600).inRange} />
        </>
      )}
    </section>
  )
}

/** Week by week: did 70%+ calls land as often as we said, and how far off were we? With a table, and honest about small samples. */
function Evolution({ rec, kind, n }: { rec: TrackRecord; kind: 'live' | 'experimental'; n: number }) {
  const w = weeklyPoints(rec.weekly, n)
  const stated = entryFor(rec, n)?.entry.mean_stated ?? null
  const xp = kind === 'experimental'
  const id = `ev-${kind}`
  return (
    <section className="evolution" aria-labelledby={`${id}-h`}>
      <h2 id={`${id}-h`}>Week by week{xp && <> <span className="tag xp">Experimental</span></>}</h2>
      {w.points.length === 0 ? (
        <div className="card empty"><h3>No weeks to chart yet</h3><p>{EMPTY_WEEKLY[kind]}</p></div>
      ) : (
        <>
          {w.usedN !== null && !w.exact && <p className="warn" role="status">No weekly numbers for {n}+; showing the nearest published threshold, {w.usedN}+.</p>}
          <div className="trackgrid">
            <ChartCard
              title={<>Hit rate of 70%+ calls at {w.usedN ?? n}+ shots</>}
              howto="each dot is one week: the share of 70%+ calls that reached the line. The bar through it is its 95% range; above the dashed line is good."
              legend={[
                { label: 'Weekly hit rate', kind: 'dot', tone: 's1' },
                { label: '95% range', kind: 'line', tone: 's1' },
                { label: `What we said${stated === null ? ' (at least 70%)' : ` (${pc(stated)})`}`, kind: 'dash', tone: 'gold' },
                { label: 'Under 10 calls', kind: 'hollow', tone: 's1' },
              ]}
            >
              <HitRateChart points={w.points} stated={stated} n={w.usedN ?? n} id={`${id}-hit`} />
            </ChartCard>
            <ChartCard
              title="Average miss per week, in shots"
              howto="how far the actual total landed from our expected total, either way. Lower is better."
              legend={[{ label: 'Average miss', kind: 'bar', tone: 's1' }, { label: 'Under 10 matches', kind: 'barhollow', tone: 's1' }]}
            >
              <MissChart points={w.points} id={`${id}-miss`} />
            </ChartCard>
          </div>
          <p className="hint smallnote">Small samples: a hollow dot or bar means fewer than 10 calls (or matches) that week, and its range is wide. Two or three weeks do not make a trend; read the overall hit rate above for the real picture.</p>
          <ScrollRegion label={`Weekly numbers, ${xp ? 'European cups' : 'live record'}`}>
            <table>
              <caption className="sr">Weekly numbers behind the charts</caption>
              <thead><tr><th scope="col">Week of</th><th scope="col">Matches</th><th scope="col">70%+ calls</th><th scope="col">Hit rate (95% range)</th><th scope="col">Avg miss</th><th scope="col">80% floor held</th></tr></thead>
              <tbody>
                {[...w.points].reverse().map((p) => (
                  <tr key={p.date}>
                    <th scope="row">{dayLong(p.date)}</th>
                    <td>{p.matches}</td>
                    <td>{p.picks === 0 ? 'none' : `${p.hits} of ${p.picks}`}{p.small && p.picks > 0 ? ' (small)' : ''}</td>
                    <td>{p.rate === null ? 'n/a' : `${pc(p.rate)} (${pc(p.lo)}–${pc(p.hi)})`}</td>
                    <td>{p.mae === null ? 'n/a' : p.mae.toFixed(1)}</td>
                    <td>{p.floor80 === null ? 'n/a' : pc(p.floor80)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
        </>
      )}
    </section>
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
  const hasXp = !!t.records.experimental
  const kind: RecordKind = state.rec === 'experimental' && !hasXp ? 'backfill' : state.rec
  const rec = (t.records[kind] ?? t.records.live) as TrackRecord
  const e = entryFor(rec, state.n)
  const [lo, hi] = ds.meta.thresholds.total
  const live = kind !== 'backfill'
  const xp = kind === 'experimental'
  const settled = rec.settled ?? 0
  const leagueRows = e ? e.entry.by_league.map((l) => ({ id: l.league, name: leagues.get(l.league)?.name ?? l.league, n: l.n, hit_rate: l.hit_rate, stated: l.stated })) : []

  return (
    <div className="page">
      <header className="pagehead">
        <p className="kicker">Proof</p>
        <h1>Track record</h1>
        <p className="lede">Does &ldquo;70%&rdquo; really mean about 7 in 10? Here is every check we have, for the shot threshold you pick. <Help term="calibrated" /></p>
      </header>
      <div className="card trackctl">
        <Stepper id="trk-n" label="Shots" value={state.n} min={lo} max={hi} onChange={(v) => set({ n: v }, { replace: true })} />
      </div>
      <Tabs label="Which record" tabs={RECORDS.filter((r) => r !== 'experimental' || hasXp).map((r) => ({ id: r, label: r === 'backfill' ? 'Backfill (past season)' : r === 'live' ? 'Live picks' : 'European cups' }))} value={kind} onChange={(r) => set({ rec: r }, { replace: true })}>
        <p className="recnote"><b>{rec.label}.</b> {xp ? 'Experimental: built from domestic form, results entered by hand. Kept apart from, and never counted in, the official live record.' : live ? 'Saved before kick-off and settled afterwards. This is the proof that counts.' : 'The model run on matches it had not seen. A fair test, but not the same as picks saved in advance.'}</p>
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
            {live && <Growth rec={rec} kind={kind as 'live' | 'experimental'} n={state.n} track={t} />}
            {!live && (
              <p className="hint">The backfill is a single past season scored in one go, so it has no timeline. The growth of the record over time, with model changes marked, is under <a href={`#/track?${new URLSearchParams({ rec: 'live', n: String(state.n) })}`} onClick={(ev) => { ev.preventDefault(); set({ rec: 'live' }, { replace: true }) }}>Live picks</a>.</p>
            )}
            <div className="trackgrid two">
              <ChartCard
                id="rel-h"
                headingLevel={2}
                title={<>Reliability at {e.n}+ shots</>}
                howto={<>each bubble groups matches by what we said (across) and shows how often they reached {e.n}+ (up). On the diagonal = the percentage meant what it said.</>}
                legend={[
                  { label: 'Probability band (bigger = more matches)', kind: 'dot', tone: 's1' },
                  { label: '95% range', kind: 'line', tone: 's1' },
                  { label: 'Perfectly calibrated', kind: 'diag', tone: 'mut' },
                  { label: `Under ${SMALL_N} matches`, kind: 'hollow', tone: 's1' },
                ]}
                tableLabel="Show the reliability numbers as a table"
                table={
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
                }
              >
                <ReliabilityChart bands={e.entry.reliability} n={e.n} />
              </ChartCard>
              {leagueRows.length > 0 && (
                <ChartCard
                  id="bl-h"
                  headingLevel={2}
                  title="By league"
                  howto={<>bars are the share of 70%+ calls that reached {e.n}+, best first; the thin mark is what we said. Leagues with few calls (n) can swing a lot.</>}
                  legend={[
                    { label: 'Hit rate, at or above what we said (▲)', kind: 'bar', tone: 's1' },
                    { label: 'Below what we said (▼)', kind: 'bar', tone: 's3' },
                    { label: 'What we said', kind: 'milestone', tone: 'mut' },
                    { label: `Faded: under ${SMALL_N} calls`, kind: 'barhollow', tone: 's1' },
                  ]}
                  tableLabel="Show the league numbers as a table"
                  table={
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
                  }
                >
                  <LeagueBars rows={leagueRows} n={e.n} />
                </ChartCard>
              )}
            </div>
          </>
        )}
        {live && <Evolution rec={rec} kind={kind as 'live' | 'experimental'} n={state.n} />}
      </Tabs>
      <p className="hint">Small samples prove nothing: judge over hundreds of matches, not a weekend. Lineups, injuries and red cards are not in the model.</p>
    </div>
  )
}
