import { useMemo } from 'react'
import { useApp } from '../lib/app'
import { pct } from '../lib/curve'
import { ROUTES, byDirection, rate, surgeCandidates, usualRate } from '../lib/surge'
import type { Candidate } from '../lib/surge'
import type { SurgeRoute, SurgeSignal } from '../lib/types'
import { SURGE_MAX_MIN } from '../lib/url'
import { KickoffText } from '../components/Panel'
import { SurgeChips, useSurgeCatalog } from '../components/Surge'
import { Badge, Help, LeagueChip, ProbPill } from '../components/ui'

const plural = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`
const thousands = (n: number) => n.toLocaleString('en-GB')
/** Same bar as shots.evaluation.surge.MIN_SEASONS_HELD. */
const MIN_SEASONS_HELD = 5
const ROUTE_SHORT: Record<SurgeRoute, string> = { noodds: 'Without odds', odds: 'With odds' }

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
      </p>
      <dl className="rstats">
        <div><dt>We expect</dt><dd><b>{item.expected}</b> shots</dd></div>
        <div><dt>Chance of {n}+</dt><dd><ProbPill p={c.p} label={`${pct(c.p)} chance of ${n}+ shots`} /></dd></div>
      </dl>
      <SurgeChips boosts={c.boosts} dampers={c.dampers} label={`Surge signals for ${item.home} v ${item.away}`} />
    </li>
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
  const shown = all.filter((c) => (!league || c.item.league === league) && c.boosts.length >= state.ms)
  const filtered = league !== '' || state.ms > 1
  const usual = Math.round(usualRate(surge) * 100)

  return (
    <div className="page surge">
      <h1>Surge Watch</h1>
      <p className="lede">
        A <b>surge</b> is a match that ends {surge.margin}+ shots above our expected total. It happens in about {usual}% of matches; these signals raise the odds. <Help term="surge" />
      </p>
      <p className="scaveat" role="note" aria-label="Caveat">
        <b>A lean, not a promise.</b> The signals come from {thousands(surge.matches)} past matches ({surge.period}), checked out of sample against what our own curves said beforehand. The probabilities on this site are unchanged: a signal never moves our numbers.
      </p>

      <section aria-labelledby="sg-boost">
        <h2 id="sg-boost">Signals that raise the odds</h2>
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
        <p className="hint">Upcoming league matches with at least one signal that raises the odds, most signals first, then our chance of {n}+ shots. Open a match for its full curve.</p>
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
                    {k}+ {k === 1 ? 'signal' : 'signals'} <span className="count">{all.filter((c) => (!league || c.item.league === league) && c.boosts.length >= k).length}</span>
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
                {filtered && <button type="button" className="chip" onClick={() => set({ ms: 1, lg: '' }, { replace: true })}>Clear filters</button>}
              </div>
              <p className="hint" role="status" aria-live="polite">{plural(shown.length, 'match', 'matches')} shown{filtered ? ` of ${all.length}` : ''}.</p>
            </div>
            {shown.length === 0 ? (
              <div className="card empty"><h3>No candidate fits these filters</h3><p>Try fewer signals or another league.</p></div>
            ) : (
              <ul className="rlist slist">{shown.map((c) => <CandidateRow key={c.item.id} c={c} n={n} />)}</ul>
            )}
          </>
        )}
      </section>
      <p className="disclaimer">Pre-match statistical patterns, not betting advice. Report: over-shoot patterns, {surge.report_date}.</p>
    </div>
  )
}
