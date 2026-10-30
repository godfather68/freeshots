import { forwardRef } from 'react'
import type { ReactNode } from 'react'
import { useApp } from '../lib/app'
import { isHit, pct, probAt } from '../lib/curve'
import { dayLong, dayShort, dayNum, monthShort } from '../lib/format'
import { stampIn, wallClock } from '../lib/time'
import { favouritesStore, toggleIn } from '../lib/storage'
import type { Item } from '../lib/types'
import { PANEL_TABS } from '../lib/url'
import type { PanelTab } from '../lib/url'
import { CurveChart } from './CurveChart'
import { Legend } from './charts'
import { EdgeCheck } from './EdgeCheck'
import { Explain } from './Explain'
import { SurgeRow, TableContextRow } from './Surge'
import { Badge, Help, ItemTags, LeagueChip, ProbPill, Stepper, Tabs, isLimited, useStore, ScrollRegion } from './ui'

const TAB_LABEL: Record<PanelTab, string> = { pred: 'Prediction', teams: 'Teams', edge: 'Odds check', about: 'About this match' }

/** "Tue 13 Oct, 20:00 your time · 21:00 at the venue": both clocks when they differ. */
export function KickoffText({ item, zone, mode }: { item: Item; zone: string; mode: 'mine' | 'venue' }) {
  if (!item.time) return <>{dayLong(item.date)}{item.finished ? ' (finished)' : ', kick-off time to be confirmed'}</>
  const day = (iso: string) => `${dayShort(iso)} ${dayNum(iso)} ${monthShort(iso)} `
  if (item.utc === null) return <>{dayLong(item.venueDate)}, <b>{item.time}</b> venue time</>
  if (mode === 'mine') {
    const differs = item.venueClock !== null && (item.venueClock !== item.time || item.venueDate !== item.date)
    return (
      <>
        {dayLong(item.date)}, <b>{item.time}</b> your time
        {differs && <> &middot; {item.venueDate !== item.date ? day(item.venueDate) : ''}{item.venueClock} at the venue</>}
      </>
    )
  }
  const mine = wallClock(item.utc, zone)
  const differs = mine.time !== item.time || mine.date !== item.date
  return (
    <>
      {dayLong(item.date)}, <b>{item.time}</b> at the venue
      {differs && <> &middot; {mine.date !== item.date ? day(mine.date) : ''}{mine.time} your time</>}
    </>
  )
}

function StarTeam({ team }: { team: string }) {
  const favs = useStore(favouritesStore)
  const on = favs.teams.includes(team)
  return (
    <button type="button" className="star sm" aria-pressed={on} aria-label={`Follow ${team}`} onClick={() => favouritesStore.set({ ...favs, teams: toggleIn(favs.teams, team) })}>
      <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M10 1.8l2.4 5.2 5.6.6-4.2 3.8 1.2 5.6L10 14.2 4.9 17l1.2-5.6L2 7.6l5.6-.6z" /></svg>
    </button>
  )
}

/** Plain-language notices for experimental and limited-data fixtures. */
function Notices({ item }: { item: Item }) {
  const { leagues } = useApp()
  const lg = leagues.get(item.league)
  const ltd = isLimited(item)
  if (!item.experimental && !ltd) return null
  return (
    <div className="notices">
      {item.experimental && (
        <p className="notice xp" role="note">
          <b>Experimental <Help term="fx_experimental" /></b>
          Built from each club&rsquo;s domestic form. Not yet validated on {lg?.name ?? 'these'} matches.
        </p>
      )}
      {ltd && (
        <p className="notice ltd" role="note">
          <b>Limited data <Help term="limited_data" /></b>
          One or both clubs have no domestic data in our sources.{item.dataQuality === 'none' ? ' Treat this one as a rough guide.' : ''}
        </p>
      )}
    </div>
  )
}

function Outcome({ item, n }: { item: Item; n: number }) {
  if (!item.actual) return null
  const hit = isHit(item.actual.total, n)
  return (
    <section className={`outcome ${hit ? 'hit' : 'miss'}`} aria-labelledby="out-h">
      <h3 id="out-h">Final shots</h3>
      <p className="big"><b>{item.actual.total}</b> total <span className="split">({item.actual.home} &ndash; {item.actual.away})</span></p>
      <p>
        <strong>{hit ? '✓ Hit' : '✗ Miss'}</strong> at {n}+ shots: the match {hit ? 'reached' : 'finished below'} the line.
        The pre-match floor at 80% sure was {item.lb['80']}+ and it {item.actual.total >= item.lb['80'] ? 'held' : 'did not hold'}.
      </p>
    </section>
  )
}

function Prediction({ item }: { item: Item }) {
  const { state, ds, totalStart } = useApp()
  const n = state.n
  const p = probAt(item.curve, totalStart, n)
  const f = item.fixture
  const [lo] = ds.meta.thresholds.total
  const summary = `Chance that both teams together take at least N shots, for N from ${lo} to ${lo + item.curve.length - 1}. At ${n} shots: ${pct(p)}. At least ${item.lb['90']} shots is 90% sure, ${item.lb['80']} is 80% sure, ${item.lb['70']} is 70% sure. Expected total ${item.expected}.`
  return (
    <div className="tabbody">
      <div className="headline" data-testid="headline">
        <span className="hl-pct"><ProbPill p={p} /></span>
        <p className="hl-text"><b>chance of {n}+ shots in total</b><br /><small>Both teams together, on and off target, blocked included. <Help term="probability" /></small></p>
      </div>

      <h3>How sure, for each floor <Help term="sure80" /></h3>
      <ul className="bounds">
        <li className="exp">
          <span className="bq">Expected</span>
          <b>{item.expected}</b>
          <span className="bn">shots in total</span>
        </li>
        {(['90', '80', '70'] as const).map((q) => (
          <li key={q} className={q === '80' ? 'main' : ''}>
            <span className="bq">{q}% sure</span>
            <b>at least {item.lb[q]}</b>
            <span className="bn">shots</span>
          </li>
        ))}
      </ul>

      <SurgeRow item={item} />
      <TableContextRow item={item} />

      <Explain item={item} />

      <h3>Chance by number of shots</h3>
      <p className="howto"><span className="howto-k">How to read this</span> pick a number of shots along the bottom; the curve gives the chance the match reaches at least that many. The dots mark the 90%, 80% and 70% floors.</p>
      <Legend items={[
        { label: 'Chance of N+ shots', kind: 'area', tone: 's1' },
        { label: `Your line (${n}+)`, kind: 'line', tone: 'mut' },
        { label: `Floors: 90% ${item.lb['90']}+, 80% ${item.lb['80']}+, 70% ${item.lb['70']}+`, kind: 'milestone', tone: 'gold' },
      ]} />
      <CurveChart
        title="Chance of at least N total shots"
        summary={summary}
        series={[{ key: 'total', label: 'Total', curve: item.curve, start: totalStart, cls: 'a' }]}
        marker={n}
        guides={[0.7, 0.8, 0.9]}
        floors={[{ q: 0.9, at: item.lb['90'] }, { q: 0.8, at: item.lb['80'] }, { q: 0.7, at: item.lb['70'] }]}
        ticks={[{ x: Math.min(Math.max(item.expected, lo), lo + item.curve.length - 1), label: `exp ${item.expected}` }]}
      />
      <details className="alt">
        <summary>Show the chart as a table</summary>
        <ScrollRegion label="Chance of at least N shots, table">
          <table>
            <caption className="sr">Chance that the match has at least N total shots</caption>
            <thead><tr><th scope="col">Shots (at least)</th><th scope="col">Chance</th></tr></thead>
            <tbody>
              {item.curve.map((c, i) => (
                <tr key={i} className={totalStart + i === n ? 'cur' : ''}><th scope="row">{totalStart + i}+</th><td>{pct(c)}</td></tr>
              ))}
            </tbody>
          </table>
        </ScrollRegion>
      </details>

      <h3>Against the league <Help term="vs_league" /></h3>
      {f && f.league_avg !== null && f.above_league !== null ? (
        <div className="vs">
          <p>
            Expected total <b>{item.expected}</b> against a league average of <b>{f.league_avg.toFixed(1)}</b>:{' '}
            <strong className={f.above_league >= 0 ? 'up' : 'down'}>
              {f.above_league >= 0 ? '▲ +' : '▼ −'}{Math.abs(f.above_league).toFixed(1)}
            </strong>{' '}
            shots {f.above_league >= 0 ? 'above' : 'below'} a typical match here. <Help term="expected" />
          </p>
          <VsBar expected={item.expected} avg={f.league_avg} />
        </div>
      ) : (
        <p className="hint">League average is only published for upcoming fixtures.</p>
      )}
    </div>
  )
}

function VsBar({ expected, avg }: { expected: number; avg: number }) {
  const max = Math.max(expected, avg, 1) * 1.15
  return (
    <div className="vsbar" role="img" aria-label={`Expected ${expected} shots, league average ${avg.toFixed(1)}`}>
      <span className="row"><i className="exp" style={{ width: `${(expected / max) * 100}%` }} /><em>Expected {expected}</em></span>
      <span className="row"><i className="avg" style={{ width: `${(avg / max) * 100}%` }} /><em>League average {avg.toFixed(1)}</em></span>
    </div>
  )
}

/** Who takes the shots: expected shots of each side as one split bar, numbers on both ends. */
function TeamSplit({ home, away, h, a }: { home: string; away: string; h: number; a: number }) {
  const share = h + a > 0 ? h / (h + a) : 0.5
  return (
    <div className="tsplit" role="img" aria-label={`Expected shots: ${home} ${h.toFixed(1)}, ${away} ${a.toFixed(1)} (${Math.round(share * 100)}% to ${Math.round((1 - share) * 100)}%)`}>
      <p className="ts-names" aria-hidden="true"><span><b>{h.toFixed(1)}</b> {home} <small>{Math.round(share * 100)}%</small></span><span><small>{Math.round((1 - share) * 100)}%</small> {away} <b>{a.toFixed(1)}</b></span></p>
      <div className="ts-bar" aria-hidden="true">
        <i className="ts-h" style={{ width: `${share * 100}%` }} />
        <i className="ts-a" style={{ width: `${(1 - share) * 100}%` }} />
      </div>
      <p className="hint ts-cap" aria-hidden="true">Expected shots of each side, and its share of the total.</p>
    </div>
  )
}

function Teams({ item }: { item: Item }) {
  const { state, ds, set, teamStart } = useApp()
  const f = item.fixture
  const [tlo, thi] = ds.meta.thresholds.team
  const tn = state.tn
  if (!f || !f.team) {
    return (
      <div className="tabbody">
        <p><span className="badge xp">experimental</span></p>
        <p>{item.finished && item.actual
          ? `Per-team curves are only kept for upcoming matches. Actual shots: ${item.home} ${item.actual.home}, ${item.away} ${item.actual.away}.`
          : 'This match was scored by a model without team calibration, so there are no team numbers.'}</p>
      </div>
    )
  }
  const t = f.team
  const ph = probAt(t.home.curve, teamStart, tn)
  const pa = probAt(t.away.curve, teamStart, tn)
  const summary = `Chance that each team alone takes at least N shots. At ${tn} shots: ${f.home} ${pct(ph)}, ${f.away} ${pct(pa)}. Expected: ${f.home} ${t.home.mean.toFixed(1)}, ${f.away} ${t.away.mean.toFixed(1)}.`
  return (
    <div className="tabbody">
      <p className="xpnote"><span className="badge xp">experimental</span> Shots for one team on its own. They pass the overall calibration checks but not yet the per-competition one, so trust them less than the match total. <Help term="experimental" /></p>
      <TeamSplit home={f.home} away={f.away} h={t.home.mean} a={t.away.mean} />
      <Stepper id="ctl-tn" label="Team shots" value={tn} min={tlo} max={thi} onChange={(v) => set({ tn: v }, { replace: true })} />
      <div className="teamcards">
        {([['home', f.home, t.home, ph], ['away', f.away, t.away, pa]] as const).map(([side, name, tc, p]) => (
          <section key={side} className={`tcard ${side}`} aria-label={name}>
            <h3><span className={`swatch ${side}`} aria-hidden="true" />{name}</h3>
            <p className="tp"><ProbPill p={p} /><span>chance of {tn}+ shots</span></p>
            <dl>
              <div><dt>Expected</dt><dd>{tc.mean.toFixed(1)}</dd></div>
              <div><dt>Median</dt><dd>{tc.median}</dd></div>
              <div><dt>80% sure</dt><dd>{tc.lb80}+</dd></div>
            </dl>
          </section>
        ))}
      </div>
      <p className="howto"><span className="howto-k">How to read this</span> each line gives the chance that one team alone takes at least that many shots; the dots show both at your team line ({tn}+).</p>
      <Legend label="Teams" items={[{ label: f.home, kind: 'line', tone: 's1' }, { label: f.away, kind: 'dash', tone: 's3' }]} />
      <CurveChart
        title="Chance of at least N shots for each team"
        xLabel="shots by one team (at least)"
        summary={summary}
        series={[
          { key: 'h', label: f.home, curve: t.home.curve, start: teamStart, cls: 'a' },
          { key: 'a', label: f.away, curve: t.away.curve, start: teamStart, cls: 'b' },
        ]}
        marker={tn}
        guides={[0.8]}
      />
    </div>
  )
}

function About({ item }: { item: Item }) {
  const { ds, zone, timeMode } = useApp()
  const f = item.fixture
  const r = item.result
  return (
    <div className="tabbody">
      <dl className="about">
        <div><dt>Model</dt><dd>{f?.model ?? ds.meta.models.join(', ')} <small>{f ? '' : '(the model set live at the time)'}</small></dd></div>
        <div>
          <dt>Odds <Help term={item.odds ? 'odds' : 'no_odds'} /></dt>
          <dd>{item.odds ? 'Odds published: scored by the model that uses them.' : 'No odds yet: scored by the separate no-odds model.'}</dd>
        </div>
        <div>
          <dt>Cold start <Help term="cold_start" /></dt>
          <dd>{f ? (item.coldStart ? <span className="badge warn">cold start: treat with caution</span> : 'No: both teams have enough earlier matches.') : 'Not recorded for finished matches.'}</dd>
        </div>
        <div>
          <dt>Prediction made</dt>
          <dd>{r ? stampIn(r.predicted_at, zone) + ', before kick-off' : `In the board built ${stampIn(ds.meta.built_at, zone)}`}</dd>
        </div>
        {f && <div><dt>Kick-off</dt><dd><KickoffText item={item} zone={zone} mode={timeMode} /></dd></div>}
        <div><dt>Calibrated <Help term="calibrated" /></dt><dd>Probabilities are the model&rsquo;s own calibrated values, shown exactly as published. See the <a href="#/track">track record</a>.</dd></div>
      </dl>
    </div>
  )
}

export interface PanelProps {
  item: Item
  full?: boolean
  onClose?: () => void
  back?: ReactNode
}

export const Panel = forwardRef<HTMLHeadingElement, PanelProps>(function Panel({ item, full, onClose, back }, ref) {
  const { state, set, leagues, href, zone, timeMode } = useApp()
  const lg = leagues.get(item.league)
  const f = item.fixture
  return (
    <article className={`panelbody${lg?.featured ? ' featured' : ''}`} aria-labelledby="panel-h">
      <header className="phead">
        <div className="crumb">
          {lg ? <LeagueChip league={lg} /> : <span>{item.league}</span>}
          {f?.round ? <span className="round"><span className="sr"> round </span>{f.round.replace('Regular Season - ', 'Round ')}</span> : null}
          <span className="phactions">
            {!full && <a className="btn quiet" href={href({}, `/match/${item.id}`)}>Full page view</a>}
            {onClose && <button type="button" className="quiet close" onClick={onClose} aria-label="Close match panel"><span aria-hidden="true">&times;</span> Close</button>}
            {back}
          </span>
        </div>
        <h2 id="panel-h" tabIndex={-1} ref={ref} className="matchtitle">
          <span className="side home"><Badge name={item.home} size={52} /><span className="tline"><span className="tname">{item.home}</span><StarTeam team={item.home} /></span></span>
          <span className="vs" aria-hidden="true">v</span><span className="sr"> versus </span>
          <span className="side away"><Badge name={item.away} size={52} /><span className="tline"><span className="tname">{item.away}</span><StarTeam team={item.away} /></span></span>
        </h2>
        <p className="ko">
          <span><KickoffText item={item} zone={zone} mode={timeMode} /></span>
          <span className="kotags">
            <span className={`badge${item.odds ? '' : ' mute'}`}>{item.odds ? 'odds' : 'no odds'}</span>
            {item.coldStart && <span className="badge warn">cold start</span>}
            <ItemTags item={item} />
          </span>
        </p>
      </header>
      <Notices item={item} />
      <Outcome item={item} n={state.n} />
      <Tabs label="Match detail" tabs={PANEL_TABS.map((t) => ({ id: t, label: TAB_LABEL[t] }))} value={state.pt} onChange={(t) => set({ pt: t }, { replace: true })} className="ptabs">
        {state.pt === 'pred' && <Prediction item={item} />}
        {state.pt === 'teams' && <Teams item={item} />}
        {state.pt === 'edge' && <EdgeCheck item={item} />}
        {state.pt === 'about' && <About item={item} />}
      </Tabs>
    </article>
  )
})
