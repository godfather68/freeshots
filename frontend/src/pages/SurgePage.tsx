import { useMemo } from 'react'
import { useApp } from '../lib/app'
import { pct } from '../lib/curve'
import { dayLong } from '../lib/format'
import {
  PHASE_LABEL, ROUTES, SURGE_SMALL, SURGE_SORT_LABEL, byDirection, contextOf, groupLabel, isPlayoffFormat, phaseOf,
  phaseOutcomes, phaseText, positionsShort, rate, settledAssessed, signalOutcomes, sortCandidates, stackRows, stackingOf,
  surgeCandidates, totals, usualRate, weeklyOutcomes,
} from '../lib/surge'
import type { Candidate, Settled } from '../lib/surge'
import type { StackCell, Stacking, Surge, SurgeRoute, SurgeSignal } from '../lib/types'
import { SURGE_MAX_MIN, SURGE_PHASES, SURGE_SORTS } from '../lib/url'
import type { SurgePhaseFilter, SurgeSort } from '../lib/url'
import { KickoffText } from '../components/Panel'
import { StrongBadge, SurgeChips, useSurgeCatalog } from '../components/Surge'
import { SurgeOutcomeChart, tallyText } from '../components/SurgeOutcomes'
import { Badge, Help, LeagueChip, ProbPill, ScrollRegion } from '../components/ui'
import { ChartCard, Legend } from '../components/charts'

/** Legend and reading line shared by every signal card. */
function SignalLegend() {
  return (
    <div className="sglegend">
      <p className="howto"><span className="howto-k">How to read this</span> each card compares how often past matches with the signal surged (solid bar) with what our curve said for those same matches (striped bar). A solid bar longer than the striped one is the signal's edge.</p>
      <Legend items={[
        { label: 'Happened (raises the odds ▲)', kind: 'bar', tone: 's1' },
        { label: 'Happened (lowers them ▼)', kind: 'bar', tone: 's3' },
        { label: 'What our curve said', kind: 'stripe', tone: 'mut' },
      ]} />
    </div>
  )
}

const plural = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`
const thousands = (n: number) => n.toLocaleString('en-GB')
/** Same bar as shots.evaluation.surge.MIN_SEASONS_HELD. */
const MIN_SEASONS_HELD = 5
const ROUTE_SHORT: Record<SurgeRoute, string> = { noodds: 'Without odds', odds: 'With odds' }
/** How many settled candidates the "latest" list shows. */
const LATEST = 10

/** Realised share against what our curve said, on one scale shared by every card. */
function SurgeBar({ over, stated, max }: { over: number; stated: number; max: number }) {
  const w = (x: number) => `${Math.min(100, (x / max) * 100)}%`
  return (
    <div className="sbar" role="img" aria-label={`Happened in ${rate(over, 1)} of these matches; our curve said ${rate(stated, 1)}`}>
      <span className="row"><span className="track"><i className="real" style={{ width: w(over) }} /></span><em>Happened {rate(over, 1)}</em></span>
      <span className="row"><span className="track"><i className="curve" style={{ width: w(stated) }} /></span><em>Our curve {rate(stated, 1)}</em></span>
    </div>
  )
}

/** Why a signal is not used on a route: the gap is not clear, it did not hold in enough seasons, or it is held back. */
function notUsedWhy(st: { excess_ci: [number, number] | (number | null)[]; seasons_held: number }, boost: boolean): string {
  const [lo, hi] = st.excess_ci
  const clear = boost ? lo !== null && lo > 0 : hi !== null && hi < 0
  if (!clear) return 'too close to call'
  if (st.seasons_held < MIN_SEASONS_HELD) return 'not steady enough across seasons'
  return 'held back for now: on this season’s data it would flag far more matches than in the study'
}

function SignalCard({ s, max }: { s: SurgeSignal; max: number }) {
  const boost = s.direction === 'boost'
  return (
    <article className={`card scard ${s.direction}`} aria-labelledby={`sg-${s.id}`}>
      <h3 id={`sg-${s.id}`} className="stitle"><span aria-hidden="true">{boost ? '▲' : '▼'}</span> {s.label}</h3>
      <p className="sexpl">{s.explanation}</p>
      {ROUTES.map((r) => {
        const st = s.routes[r]
        if (!st) return null
        return st.applies ? (
          <div key={r} className="sroute">
            <p className="shead">
              <span className="rname">{ROUTE_SHORT[r]}</span>
              <span><strong>{rate(st.over_rate)}</strong> vs {rate(st.base_rate)} usual</span>
            </p>
            <SurgeBar over={st.over_rate} stated={st.stated_rate} max={max} />
            <p className="hint">Held in {st.seasons_held} of {st.seasons} seasons &middot; {thousands(st.n)} matches</p>
          </div>
        ) : (
          <p key={r} className="hint snot">
            <span className="tag ltd">Not used</span> {ROUTE_SHORT[r]}: {rate(st.over_rate, 1)} against our curve&rsquo;s {rate(st.stated_rate, 1)}, {notUsedWhy(st, boost)}.
          </p>
        )
      })}
    </article>
  )
}

/** Compact table context on a candidate row: phase chip and the two league positions. */
function ContextLine({ c }: { c: Candidate }) {
  const ctx = contextOf(c.item.fixture)
  if (!ctx) return null
  return (
    <p className="sctx">
      {phaseText(ctx) && <span className="tag phase">{phaseText(ctx)}</span>}
      {isPlayoffFormat(ctx) && <span className="tag phase">Play-off format</span>}
      <span className="hint"><span className="sr">League positions: </span>{positionsShort(ctx)}</span>
    </p>
  )
}

function CandidateRow({ c, n }: { c: Candidate; n: number }) {
  const { leagues, href, zone, timeMode } = useApp()
  const { item } = c
  const lg = leagues.get(item.league) ?? { id: item.league, name: item.league, country: '' }
  return (
    <li className="srow">
      <div className="rhead">
        <a href={href({ date: item.date, m: item.id }, '/')} className="rteams" aria-label={`Open ${item.home} v ${item.away}`}>
          <span className="mteam"><Badge name={item.home} /><span className="tn">{item.home}</span></span>
          <span className="mteam"><Badge name={item.away} /><span className="tn">{item.away}</span></span>
        </a>
        <p className="scount"><b>{c.boosts.length}</b><span>{c.boosts.length === 1 ? 'signal' : 'signals'}</span></p>
      </div>
      <p className="rmeta">
        <LeagueChip league={lg} />
        <span className="sko"><KickoffText item={item} zone={zone} mode={timeMode} /></span>
        {c.strong && <StrongBadge />}
      </p>
      <dl className="rstats">
        <div><dt>We expect</dt><dd><b>{item.expected}</b> shots</dd></div>
        <div><dt>Chance of {n}+</dt><dd><ProbPill p={c.p} label={`${pct(c.p)} chance of ${n}+ shots`} /></dd></div>
      </dl>
      <SurgeChips boosts={c.boosts} dampers={c.dampers} label={`Surge signals for ${item.home} v ${item.away}`} />
      <ContextLine c={c} />
    </li>
  )
}

const cellText = (c: StackCell | null) =>
  !c || !c.n ? 'none' : c.over_rate === undefined || c.stated_rate === undefined ? `${thousands(c.n)} matches`
    : `${rate(c.over_rate)} vs ${rate(c.stated_rate)} (${thousands(c.n)}${c.seasons_held !== undefined ? `, held ${c.seasons_held}/${c.seasons}` : ''})`

/** Several signals at once: the strong-candidate rule and the "By number of signals" table (surge.json `stacking`). */
function Stacked({ st }: { st: Stacking }) {
  const two = (r: SurgeRoute) => st.routes[r].by_count['2+'] ?? null
  const line = (r: SurgeRoute) => {
    const c = two(r)
    const nums = c && c.over_rate !== undefined && c.stated_rate !== undefined
      ? `${st.min_boosts}+ signals surged ${rate(c.over_rate)} vs ${rate(c.stated_rate)} our curve, held ${c.seasons_held} of ${c.seasons} seasons`
      : `${st.min_boosts}+ signals: no numbers`
    return st.routes[r].strong_applies
      ? <><b>{ROUTE_SHORT[r]}:</b> {nums}; confirmed on later seasons, so these matches are marked <StrongBadge />.</>
      : <><b>{ROUTE_SHORT[r]}:</b> {nums}, but not confirmed on later seasons, so no match is marked strong here.</>
  }
  return (
    <section aria-labelledby="sg-stack" className="sstack">
      <h2 id="sg-stack">Several signals at once</h2>
      <p className="hint">Signals partly add up: matches with {st.min_boosts} or more surged more often than with one. A <b>strong candidate</b> has {st.min_boosts}+ signals on a route where that held up out of sample.</p>
      <ul className="hint">
        {ROUTES.map((r) => <li key={r}>{line(r)}</li>)}
      </ul>
      <p className="hint">Matches with odds are labelled only when our Surge model is not used (it already corrects for these patterns), so strong candidates are rare.</p>
      <ScrollRegion label="Surges by number of signals">
        <table>
          <caption>By number of signals: share that surged vs what our curve said (past matches, out of sample)</caption>
          <thead><tr><th scope="col">Signals</th>{ROUTES.map((r) => <th key={r} scope="col">{ROUTE_SHORT[r]}</th>)}</tr></thead>
          <tbody>
            {stackRows(st).map(({ group, cells }) => (
              <tr key={group}>
                <th scope="row">{groupLabel(group)}</th>
                {ROUTES.map((r) => <td key={r}>{cellText(cells[r])}</td>)}
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollRegion>
    </section>
  )
}

/** One settled surge candidate: expected, actual, surged or not (in words and a mark, never colour alone). */
function SettledRow({ s, margin, labels }: { s: Settled; margin: number; labels: Map<string, SurgeSignal> }) {
  const { leagues, href } = useApp()
  const { r } = s
  const lg = leagues.get(r.league) ?? { id: r.league, name: r.league, country: '' }
  const named = s.boosts.map((id) => labels.get(id)?.label).filter((x): x is string => !!x)
  return (
    <li className="rrow ssettled">
      <div className="rhead">
        <a href={href({ date: r.date, m: r.id }, '/')} className="rteams" aria-label={`Open ${r.home} v ${r.away}`}>
          <span className="mteam"><Badge name={r.home} /><span className="tn">{r.home}</span></span>
          <span className="mteam"><Badge name={r.away} /><span className="tn">{r.away}</span></span>
        </a>
        <p className={`res sres ${s.surged ? 'hit' : 'flat'}`}>
          <span aria-hidden="true">{s.surged ? '▲' : '–'}</span> {s.surged ? 'Surged' : 'No surge'}
        </p>
      </div>
      <p className="rmeta"><LeagueChip league={lg} /><span className="sko">{dayLong(r.date)}</span>{r.surge?.strong === true && <StrongBadge />}</p>
      <dl className="rstats">
        <div><dt>We expected</dt><dd><b>{r.expected}</b> shots</dd></div>
        <div><dt>Actual</dt><dd><b>{r.actual.total}</b> <small className="short">{s.gap >= 0 ? '+' : ''}{s.gap} (surge = +{margin})</small></dd></div>
      </dl>
      {named.length > 0 && <p className="hint">Signals: {named.join(', ')}</p>}
    </li>
  )
}

/** How surge candidates turned out (results.json `surge`): chart + table, per-signal counts, the latest ones. */
function Outcomes({ surge }: { surge: Surge }) {
  const { ds } = useApp()
  const settled = useMemo(() => settledAssessed(ds.results?.results ?? [], surge.margin), [ds.results, surge.margin])
  const tot = totals(settled)
  const weeks = weeklyOutcomes(settled)
  const perSignal = signalOutcomes(settled, surge)
  const latest = settled.filter((s) => s.boosts.length > 0).slice(0, LATEST)
  const labels = new Map(surge.signals.map((s) => [s.id, s]))
  const usual = usualRate(surge)
  const byPhase = phaseOutcomes(settled)
  const byPhaseN = byPhase.reduce((a, r) => a + r.flagged.n + r.other.n, 0)
  return (
    <section aria-labelledby="sg-out" className="sout">
      <h2 id="sg-out">How surge candidates turned out</h2>
      <p className="hint">
        Settled league matches of the last 30 days that Surge Watch looked at before kick-off. A match <b>surged</b> when it ended {surge.margin}+ shots above our expected total; usually about {rate(usual)} do.
      </p>
      {settled.length === 0 ? (
        <div className="card empty">
          <h3>No surge candidate has been settled yet</h3>
          <p>Signals were first recorded on 27 October 2026. Flagged matches appear here as their results come in (football-data publishes results about twice a week).</p>
        </div>
      ) : (
        <>
          <dl className="card rcstats souttot">
            <div><dt>With 1+ signal</dt><dd><b>{tallyText(tot.flagged)}</b> surged</dd></div>
            <div><dt>Looked at, no signal</dt><dd><b>{tallyText(tot.other)}</b> surged</dd></div>
          </dl>
          {tot.flagged.n < SURGE_SMALL ? (
            <div className="card empty">
              <h3>Too few settled candidates to chart yet</h3>
              <p>{plural(tot.flagged.n, 'settled match', 'settled matches')} with a signal so far; the weekly chart appears from {SURGE_SMALL}. A handful of matches says almost nothing: one in five matches surges anyway.</p>
            </div>
          ) : (
            <>
              <ChartCard
                title="Share that surged, week by week"
                howto={<>each week, how many settled matches ended {surge.margin}+ shots above our expected total: those flagged with a signal against those we looked at without one. Flagged above the others (and the dashed line) is what the signals promise.</>}
                legend={[
                  { label: 'With 1+ signal', kind: 'dot', tone: 's1' },
                  { label: 'Looked at, no signal', kind: 'square', tone: 's3' },
                  { label: `Usual share (${rate(usual)})`, kind: 'dash', tone: 'gold' },
                  { label: `Hollow: fewer than ${SURGE_SMALL} matches that week`, kind: 'hollow', tone: 's1' },
                ]}
              >
                <SurgeOutcomeChart weeks={weeks} usual={usual} margin={surge.margin} id="sg-out-chart" />
              </ChartCard>
              <p className="hint smallnote">Small samples: weeks hold a few dozen matches at most, so a week above or below the line means little. The research behind the signals used about 50,000 matches.</p>
              <ScrollRegion label="Weekly surge numbers">
                <table>
                  <caption className="sr">Weekly numbers behind the chart</caption>
                  <thead><tr><th scope="col">Week of</th><th scope="col">With 1+ signal: surged</th><th scope="col">No signal: surged</th></tr></thead>
                  <tbody>
                    {[...weeks].reverse().map((w) => (
                      <tr key={w.date}>
                        <th scope="row">{dayLong(w.date)}</th>
                        <td>{tallyText(w.flagged)}{w.flagged.n > 0 && w.flagged.n < SURGE_SMALL ? ' (small)' : ''}</td>
                        <td>{tallyText(w.other)}{w.other.n > 0 && w.other.n < SURGE_SMALL ? ' (small)' : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </ScrollRegion>
            </>
          )}
          <h3>By signal</h3>
          <ScrollRegion label="Settled matches by signal">
            <table>
              <caption className="sr">Settled matches carrying each signal and how many surged</caption>
              <thead><tr><th scope="col">Signal</th><th scope="col">Settled</th><th scope="col">Surged</th></tr></thead>
              <tbody>
                {perSignal.map(({ signal, tally }) => (
                  <tr key={signal.id}>
                    <th scope="row"><span aria-hidden="true">{signal.direction === 'boost' ? '▲' : '▼'}</span> {signal.label}<span className="sr"> ({signal.direction === 'boost' ? 'raises' : 'lowers'} the chance)</span></th>
                    <td>{tally.n}</td>
                    <td>{tally.n === 0 ? 'none yet' : `${tally.surged}${tally.n < SURGE_SMALL ? ' (small sample)' : ` (${rate(tally.surged / tally.n)})`}`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
          <h3>By season phase</h3>
          {byPhaseN < SURGE_SMALL ? (
            <p className="hint">Too few settled matches with a recorded season phase yet ({byPhaseN} so far; the table appears from {SURGE_SMALL}).</p>
          ) : (
            <ScrollRegion label="Settled matches by season phase">
              <table>
                <caption className="sr">Share that surged by season phase, with and without a signal</caption>
                <thead><tr><th scope="col">Season phase</th><th scope="col">With 1+ signal: surged</th><th scope="col">No signal: surged</th></tr></thead>
                <tbody>
                  {byPhase.map((row) => (
                    <tr key={row.phase}>
                      <th scope="row">{PHASE_LABEL[row.phase]}</th>
                      <td>{tallyText(row.flagged)}{row.flagged.n > 0 && row.flagged.n < SURGE_SMALL ? ' (small)' : ''}</td>
                      <td>{tallyText(row.other)}{row.other.n > 0 && row.other.n < SURGE_SMALL ? ' (small)' : ''}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </ScrollRegion>
          )}
          {latest.length > 0 && (
            <>
              <h3>Latest settled candidates</h3>
              <ul className="rlist slist">{latest.map((s) => <SettledRow key={s.r.id} s={s} margin={surge.margin} labels={labels} />)}</ul>
            </>
          )}
        </>
      )}
    </section>
  )
}

export function SurgePage() {
  const { ds, items, state, set, leagues, totalStart } = useApp()
  const cat = useSurgeCatalog()
  const n = ds.meta.thresholds.default_total
  const all = useMemo(() => surgeCandidates(items, cat, n, totalStart), [items, cat, n, totalStart])
  const surge = cat ? ds.surge ?? null : null

  if (!surge) {
    return (
      <div className="page narrow">
        <h1>Surge Watch</h1>
        <div className="card empty" role="status">
          <h2>Surge Watch is not published yet</h2>
          <p>The surge signals have not been published with this update. The rest of the site is unaffected: every chance shown is the model&rsquo;s own.</p>
          <p><a href="#/">Back to the matches</a></p>
        </div>
      </div>
    )
  }

  const { boosts, dampers } = byDirection(surge)
  const max = Math.max(0.3, ...surge.signals.flatMap((s) => ROUTES.map((r) => Math.max(s.routes[r]?.over_rate ?? 0, s.routes[r]?.stated_rate ?? 0)))) * 1.1
  const candLeagues = [...new Set(all.map((c) => c.item.league))]
    .map((id) => ({ id, name: leagues.get(id)?.name ?? id, country: leagues.get(id)?.country ?? '' }))
    .sort((a, b) => a.name.localeCompare(b.name))
  const league = candLeagues.some((l) => l.id === state.lg) ? state.lg : ''
  const phase: SurgePhaseFilter = state.ph
  const inScope = (c: Candidate) => (!league || c.item.league === league) && (!phase || phaseOf(c) === phase)
  const shown = sortCandidates(all.filter((c) => inScope(c) && c.boosts.length >= state.ms), state.ss)
  const filtered = league !== '' || state.ms > 1 || phase !== ''
  const usual = Math.round(usualRate(surge) * 100)
  const phaseCount = (p: SurgePhaseFilter) => all.filter((c) => (!league || c.item.league === league) && c.boosts.length >= state.ms && (!p || phaseOf(c) === p)).length

  return (
    <div className="page surge">
      <header className="pagehead">
      <p className="kicker">Over-shoot signals</p>
      <h1>Surge Watch</h1>
      <p className="lede">
        A <b>surge</b> is a match that ends {surge.margin}+ shots above our expected total. It happens in about {usual}% of matches; these signals raise the odds. <Help term="surge" />
      </p>
      </header>
      <p className="scaveat" role="note" aria-label="Caveat">
        <b>A lean, not a promise.</b> The signals come from {thousands(surge.matches)} past matches ({surge.period}), checked out of sample against what our own curves said beforehand. The probabilities on this site are unchanged: a signal never moves our numbers. Matches with bookmaker odds are already adjusted by our Surge model, so they are only labelled here when that model is not used.
      </p>

      <section aria-labelledby="sg-boost">
        <h2 id="sg-boost">Signals that raise the odds</h2>
        <SignalLegend />
        <div className="sgrid">{boosts.map((s) => <SignalCard key={s.id} s={s} max={max} />)}</div>
      </section>
      {dampers.length > 0 && (
        <section aria-labelledby="sg-damp">
          <h2 id="sg-damp">Signals that lower them</h2>
          <div className="sgrid">{dampers.map((s) => <SignalCard key={s.id} s={s} max={max} />)}</div>
        </section>
      )}
      <p className="hint smallnote">
        &ldquo;Usual&rdquo; is the share of all matches that surged; &ldquo;our curve&rdquo; is what our published curves gave for the same matches. A signal is used only where the gap to our curve held up across seasons; elsewhere it is marked &ldquo;not used&rdquo; (usually because our model already accounts for it). Matches are scored with odds when bookmakers have published them, otherwise by the no-odds model.
      </p>

      <section aria-labelledby="sg-cand" className="scand">
        <h2 id="sg-cand">Upcoming surge candidates</h2>
        <p className="hint">Upcoming league matches with at least one signal that raises the odds, most signals first, then our chance of {n}+ shots. Each shows the season phase and both teams&rsquo; league positions before the match <Help term="phase" />. Open a match for its full curve.</p>
        {all.length === 0 ? (
          <div className="card empty">
            <h3>No upcoming match has a surge signal right now</h3>
            <p>Signals show up as the season moves on (for example the last league games in spring) and as new results come in.</p>
          </div>
        ) : (
          <>
            <div className="card trackctl rfilter">
              <div className="chips" role="group" aria-label="Minimum number of signals">
                {Array.from({ length: SURGE_MAX_MIN }, (_, i) => i + 1).map((k) => (
                  <button key={k} type="button" className="chip" aria-pressed={state.ms === k} onClick={() => set({ ms: k }, { replace: true })}>
                    {k}+ {k === 1 ? 'signal' : 'signals'} <span className="count">{all.filter((c) => inScope(c) && c.boosts.length >= k).length}</span>
                  </button>
                ))}
              </div>
              <div className="controls" role="group" aria-label="Narrow the candidates">
                <label className="sel">League
                  <select value={league} onChange={(e) => set({ lg: e.target.value }, { replace: true })}>
                    <option value="">All leagues</option>
                    {candLeagues.map((l) => <option key={l.id} value={l.id}>{l.name}{l.country ? ` (${l.country})` : ''}</option>)}
                  </select>
                </label>
                <label className="sel">Season phase
                  <select value={phase} onChange={(e) => set({ ph: e.target.value as SurgePhaseFilter }, { replace: true })}>
                    {SURGE_PHASES.map((p) => <option key={p} value={p}>{p ? PHASE_LABEL[p] : 'All phases'} ({phaseCount(p)})</option>)}
                  </select>
                </label>
                <label className="sel">Order
                  <select value={state.ss} onChange={(e) => set({ ss: e.target.value as SurgeSort }, { replace: true })}>
                    {SURGE_SORTS.map((s) => <option key={s} value={s}>{s === 'prob' ? `${SURGE_SORT_LABEL[s]} of ${n}+` : SURGE_SORT_LABEL[s]}</option>)}
                  </select>
                </label>
                {filtered && <button type="button" className="chip" onClick={() => set({ ms: 1, lg: '', ph: '' }, { replace: true })}>Clear filters</button>}
              </div>
              <p className="hint" role="status" aria-live="polite">{plural(shown.length, 'match', 'matches')} shown{filtered ? ` of ${all.length}` : ''}.</p>
            </div>
            {shown.length === 0 ? (
              <div className="card empty"><h3>No candidate fits these filters</h3><p>Try fewer signals, another league or another season phase.</p></div>
            ) : (
              <ul className="rlist slist">{shown.map((c) => <CandidateRow key={c.item.id} c={c} n={n} />)}</ul>
            )}
          </>
        )}
      </section>

      {stackingOf(surge) && <Stacked st={stackingOf(surge)!} />}

      <Outcomes surge={surge} />

      <p className="disclaimer">Pre-match statistical patterns, not betting advice. Report: over-shoot patterns, {surge.report_date}.</p>
    </div>
  )
}

