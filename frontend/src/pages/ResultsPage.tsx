import { useApp } from '../lib/app'
import { dayLong } from '../lib/format'
import { NEAR_MISS, byDay, isNearMiss, isProvisional, keepVerdict, provisionalNote, recap, shortBy } from '../lib/recap'
import type { DayGroup, Recap, ResultFilter, Verdict } from '../lib/recap'
import { OUTCOMES } from '../lib/url'
import type { Outcome } from '../lib/url'
import { Badge, Help, LeagueChip, ProbPill, Stepper } from '../components/ui'

const n1 = (x: number | null) => (x === null ? 'n/a' : x.toFixed(1))
const plural = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`
const isFiltered = (f: ResultFilter) => f.outcome !== 'all' || f.calls || f.league !== ''
const OUTCOME_LABEL: Record<Outcome, (n: number) => string> = {
  all: () => 'All',
  hit: (n) => `Hits (${n}+)`,
  miss: (n) => `Misses (under ${n})`,
  near: () => `Near misses (1–${NEAR_MISS} short)`,
  floor: () => 'Floor broken',
}

/** Hit or miss, in words and a mark, never colour alone. */
function Mark({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return <span className={`res ${ok ? 'hit' : 'miss'}`}><span aria-hidden="true">{ok ? '✓' : '✗'}</span> {ok ? yes : no}</span>
}

function Stats({ r, n }: { r: Recap; n: number }) {
  return (
    <dl className="rcstats">
      <div>
        <dt>70%+ calls that reached {n}+</dt>
        <dd>{r.calls === 0 ? <span className="none">no 70%+ calls</span> : <><b>{r.callHits}</b> of {r.calls}{r.callNear > 0 && <small className="near"> &middot; {r.callNear} missed by 1&ndash;{NEAR_MISS}</small>}</>}</dd>
      </div>
      <div><dt>All matches reaching {n}+</dt><dd><b>{r.reached}</b> of {r.matches}</dd></div>
      <div><dt>Average miss</dt><dd><b>{n1(r.mae)}</b> shots</dd></div>
      <div><dt>&ldquo;80% sure&rdquo; floor held</dt><dd><b>{r.floorHeld}</b> of {r.matches}</dd></div>
    </dl>
  )
}

type ChipLeague = Parameters<typeof LeagueChip>[0]['league']

function Row({ v, n, href, league }: { v: Verdict; n: number; href: string; league: ChipLeague }) {
  const { r } = v
  const xp = r.experimental === true
  return (
    <li className="rrow">
      <div className="rhead">
        <a href={href} className="rteams" aria-label={`Open ${r.home} v ${r.away}`}>
          <span className="mteam"><Badge name={r.home} /><span className="tn">{r.home}</span></span>
          <span className="mteam"><Badge name={r.away} /><span className="tn">{r.away}</span></span>
        </a>
        <p className="ractual">
          <b>{r.actual.total}</b><span>shots</span>
          <small>{r.actual.home} &ndash; {r.actual.away}</small>
        </p>
      </div>
      <p className="rmeta">
        <LeagueChip league={league} />
        {!xp && !r.odds && <span className="nodds">no odds</span>}
        {xp && <span className="tag xp">Experimental</span>}
        {r.result_source === 'manual' && <span className="tag ltd">Entered by hand</span>}
        {isProvisional(r) && <span className="tag ltd prov" title={provisionalNote(r)}>Provisional<span className="sr">: {provisionalNote(r)}</span></span>}
        {r.source_url && /^https?:\/\//.test(r.source_url) && (
          <a className="srclink" href={r.source_url} rel="noopener noreferrer" target="_blank">Result source<span className="sr"> for {r.home} v {r.away} (opens in a new tab)</span></a>
        )}
      </p>
      <dl className="rstats">
        <div><dt>We expected</dt><dd><b>{r.expected}</b></dd></div>
        <div><dt>&ldquo;80% sure&rdquo; floor</dt><dd><b>{r.lb['80']}</b> <Mark ok={v.floorHeld} yes="held" no="broken" /></dd></div>
        <div><dt>Chance of {n}+</dt><dd><ProbPill p={v.p} /></dd></div>
        <div><dt>At {n}+ shots</dt><dd><Mark ok={v.hit} yes="Hit" no="Miss" />{!v.hit && <small className={`short${isNearMiss(v) ? ' near' : ''}`}> {shortBy(v)} short</small>}</dd></div>
      </dl>
    </li>
  )
}

function Day({ g, n, open, kind, f }: { g: DayGroup; n: number; open: boolean; kind: string; f: ResultFilter }) {
  const { leagues, href } = useApp()
  const r = g.recap
  const prov = g.verdicts.filter((v) => isProvisional(v.r)).length
  const filtered = isFiltered(f)
  const shown = filtered ? g.verdicts.filter((v) => keepVerdict(v, f)) : g.verdicts
  if (shown.length === 0) return null
  return (
    <section className="card rday" aria-labelledby={`d-${kind}-${g.date}`}>
      <h3 id={`d-${kind}-${g.date}`}>{dayLong(g.date)} <span className="cnt">&middot; {plural(r.matches, 'match', 'matches')}</span></h3>
      <Stats r={r} n={n} />
      {prov > 0 && (
        <p className="hint provnote"><span className="tag ltd prov">Provisional</span> {prov === r.matches ? (r.matches === 1 ? 'This result was' : 'All these results were') : `${prov} of ${r.matches} results were`} entered by hand and will be replaced by the official data when it arrives. <Help term="provisional" /></p>
      )}
      {r.bias !== null && Math.abs(r.bias) >= 0.05 && (
        <p className="hint">On average {Math.abs(r.bias).toFixed(1)} {Math.abs(r.bias) === 1 ? 'shot' : 'shots'} {r.bias > 0 ? 'more' : 'fewer'} than we expected.</p>
      )}
      <details className="rmatches" open={open || filtered}>
        <summary>{filtered ? `${shown.length} of ${plural(r.matches, 'match', 'matches')} shown` : plural(r.matches, 'match', 'matches')}: predicted against actual</summary>
        <ul className="rlist">
          {shown.map((v) => {
            const l = leagues.get(v.r.league)
            return <Row key={v.r.id} v={v} n={n} href={href({ date: v.r.date, m: v.r.id }, '/')} league={l ?? { id: v.r.league, name: v.r.league, country: '' }} />
          })}
        </ul>
      </details>
    </section>
  )
}

/** Per-league accuracy over the days shown: most 70%+ calls first. A league name filters the list. */
function ByLeague({ verdicts, n, kind }: { verdicts: Verdict[]; n: number; kind: string }) {
  const { leagues, set, state } = useApp()
  const per = new Map<string, Verdict[]>()
  for (const v of verdicts) per.set(v.r.league, [...(per.get(v.r.league) ?? []), v])
  if (per.size < 2) return null
  const rows = [...per.entries()]
    .map(([id, vs]) => ({ id, name: leagues.get(id)?.name ?? id, country: leagues.get(id)?.country ?? '', r: recap(vs) }))
    .sort((a, b) => b.r.calls - a.r.calls || b.r.matches - a.r.matches || a.name.localeCompare(b.name))
  const pct = (k: number, of: number) => (of === 0 ? '' : ` (${Math.round((100 * k) / of)}%)`)
  return (
    <details className="card rleagues">
      <summary>By league: {plural(rows.length, 'league', 'leagues')}</summary>
      <div className="tablewrap">
        <table>
          <caption className="sr">Accuracy by league at {n}+ shots</caption>
          <thead>
            <tr><th scope="col">League</th><th scope="col">Matches</th><th scope="col">70%+ calls that hit</th><th scope="col">Floor held</th><th scope="col">Avg miss</th></tr>
          </thead>
          <tbody>
            {rows.map(({ id, name, country, r }) => (
              <tr key={id}>
                <th scope="row">
                  <button type="button" className="linkish" aria-pressed={state.lg === id} onClick={() => set({ lg: state.lg === id ? '' : id }, { replace: true })}>
                    {name}{country && <span className="cnt"> {country}</span>}<span className="sr"> ({kind === 'xp' ? 'European cups' : 'league matches'}): show only this league</span>
                  </button>
                </th>
                <td>{r.matches}</td>
                <td>{r.calls === 0 ? <span className="none">none</span> : <><b>{r.callHits}</b> of {r.calls}{pct(r.callHits, r.calls)}</>}</td>
                <td>{r.floorHeld} of {r.matches}</td>
                <td>{n1(r.mae)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">Few matches per league: treat these as a first look, not a verdict. The track record has the long view.</p>
    </details>
  )
}

function Group({ kind, title, intro, rows, n, empty, f }: { kind: 'official' | 'xp'; title: string; intro: string; rows: DayGroup[]; n: number; empty: string; f: ResultFilter }) {
  const all = recap(rows.flatMap((g) => g.verdicts))
  const xp = kind === 'xp'
  const shown = rows.flatMap((g) => g.verdicts).filter((v) => keepVerdict(v, f)).length
  return (
    <section className={`rgroup${xp ? ' xp' : ''}`} aria-labelledby={`g-${kind}`}>
      <h2 id={`g-${kind}`}>{title}{xp && <> <span className="tag xp">Experimental</span></>}</h2>
      <p className="hint">{intro}</p>
      {rows.length === 0 ? (
        <div className="card empty"><h3>Nothing to compare yet</h3><p>{empty}</p></div>
      ) : (
        <>
          <div className="card rtotal">
            <h3>All {plural(rows.length, 'day', 'days')} below</h3>
            <Stats r={all} n={n} />
            <p className="hint">{plural(all.matches, 'match', 'matches')}. With few matches these are anecdotes, not accuracy; the <a href={`#/track?rec=${xp ? 'experimental' : 'live'}&n=${n}`}>track record</a> has the long view.</p>
          </div>
          <ByLeague verdicts={rows.flatMap((g) => g.verdicts)} n={n} kind={kind} />
          {isFiltered(f) && shown === 0 && <div className="card empty"><h3>No match fits these filters</h3><p>Try another outcome, league or shots threshold.</p></div>}
          {rows.map((g, i) => <Day key={g.date} g={g} n={n} open={i < 2} kind={kind} f={f} />)}
        </>
      )}
    </section>
  )
}

export function ResultsPage() {
  const { ds, state, set, totalStart, leagues } = useApp()
  const [lo, hi] = ds.meta.thresholds.total
  const all = ds.results?.results ?? []
  const official = byDay(all.filter((r) => r.experimental !== true), state.n, totalStart)
  const xp = byDay(all.filter((r) => r.experimental === true), state.n, totalStart)
  const hasXp = xp.length > 0 || ds.meta.leagues.some((l) => l.featured) || !!ds.track?.records.experimental
  const resultLeagues = [...new Set(all.map((r) => r.league))]
    .map((id) => ({ id, name: leagues.get(id)?.name ?? id, country: leagues.get(id)?.country ?? '' }))
    .sort((a, b) => a.name.localeCompare(b.name))
  // A league chosen on another page may have no results: ignore it here rather than show an empty page.
  const league = resultLeagues.some((l) => l.id === state.lg) ? state.lg : ''
  const f: ResultFilter = { outcome: state.rf, calls: state.rc, league }
  const verdicts = [...official, ...xp].flatMap((g) => g.verdicts)
  const counts = Object.fromEntries(OUTCOMES.map((o) => [o, verdicts.filter((v) => keepVerdict(v, { ...f, outcome: o })).length])) as Record<Outcome, number>
  const filtered = isFiltered(f)
  return (
    <div className="page">
      <h1>Results</h1>
      <p className="lede">What we said before kick-off against what happened, match by match, for the last 30 days. Move the shots threshold to re-check every call. Days are match days at the venue.</p>
      <div className="card trackctl rfilter">
        <Stepper id="res-n" label="Shots" value={state.n} min={lo} max={hi} onChange={(v) => set({ n: v }, { replace: true })} />
        {all.length > 0 && (
          <>
            <div className="chips" role="group" aria-label="Show">
              {OUTCOMES.map((o) => (
                <button key={o} type="button" className="chip" aria-pressed={state.rf === o} onClick={() => set({ rf: o }, { replace: true })}>
                  {OUTCOME_LABEL[o](state.n)} <span className="count">{counts[o]}</span>
                </button>
              ))}
            </div>
            <div className="controls" role="group" aria-label="Narrow the results">
              <label className="sel chk">
                <input type="checkbox" checked={state.rc} onChange={(e) => set({ rc: e.target.checked }, { replace: true })} /> 70%+ calls only
              </label>
              <label className="sel">League
                <select value={league} onChange={(e) => set({ lg: e.target.value }, { replace: true })}>
                  <option value="">All leagues</option>
                  {resultLeagues.map((l) => <option key={l.id} value={l.id}>{l.name}{l.country ? ` (${l.country})` : ''}</option>)}
                </select>
              </label>
              {filtered && <button type="button" className="chip" onClick={() => set({ rf: 'all', rc: false, lg: '' }, { replace: true })}>Clear filters</button>}
            </div>
            <p className="hint" role="status" aria-live="polite">{filtered ? `Showing ${plural(counts[state.rf], 'match', 'matches')}. Day and total figures still count every match.` : ''}</p>
          </>
        )}
      </div>
      <Group
        kind="official"
        title="League matches (official record)"
        intro="Predictions saved before kick-off, settled from the published results. These are the numbers the track record counts."
        rows={official}
        n={state.n}
        f={f}
        empty="Results are published about twice a week, then settled by the next update. The first finished matches with a saved prediction will show here as soon as they are in."
      />
      {hasXp && (
        <Group
          kind="xp"
          title="European cups"
          intro="Experimental: built from each club's domestic form, never checked on this competition, and results are typed in by hand, so they lag. These numbers are kept apart and never count in the official record."
          rows={xp}
          n={state.n}
          f={f}
          empty="European cup results are entered by hand after each matchday, so they appear a little after the matches. None have been entered yet."
        />
      )}
      <p className="hint">&ldquo;Floor&rdquo; is the highest shot count we were at least 80% sure the match would reach. &ldquo;Average miss&rdquo; is how far the actual total landed from our expected total, in shots, ignoring direction. Lineups, injuries and red cards are not in the model.</p>
    </div>
  )
}
