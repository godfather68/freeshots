import { useApp } from '../lib/app'
import { dayLong } from '../lib/format'
import { byDay, recap } from '../lib/recap'
import type { DayGroup, Recap, Verdict } from '../lib/recap'
import { Badge, LeagueChip, ProbPill, Stepper } from '../components/ui'

const n1 = (x: number | null) => (x === null ? 'n/a' : x.toFixed(1))
const plural = (k: number, one: string, many: string) => `${k} ${k === 1 ? one : many}`

/** Hit or miss, in words and a mark, never colour alone. */
function Mark({ ok, yes, no }: { ok: boolean; yes: string; no: string }) {
  return <span className={`res ${ok ? 'hit' : 'miss'}`}><span aria-hidden="true">{ok ? '✓' : '✗'}</span> {ok ? yes : no}</span>
}

function Stats({ r, n }: { r: Recap; n: number }) {
  return (
    <dl className="rcstats">
      <div>
        <dt>70%+ calls that reached {n}+</dt>
        <dd>{r.calls === 0 ? <span className="none">no 70%+ calls</span> : <><b>{r.callHits}</b> of {r.calls}</>}</dd>
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
        {r.source_url && /^https?:\/\//.test(r.source_url) && (
          <a className="srclink" href={r.source_url} rel="noopener noreferrer" target="_blank">Result source<span className="sr"> for {r.home} v {r.away} (opens in a new tab)</span></a>
        )}
      </p>
      <dl className="rstats">
        <div><dt>We expected</dt><dd><b>{r.expected}</b></dd></div>
        <div><dt>&ldquo;80% sure&rdquo; floor</dt><dd><b>{r.lb['80']}</b> <Mark ok={v.floorHeld} yes="held" no="broken" /></dd></div>
        <div><dt>Chance of {n}+</dt><dd><ProbPill p={v.p} /></dd></div>
        <div><dt>At {n}+ shots</dt><dd><Mark ok={v.hit} yes="Hit" no="Miss" /></dd></div>
      </dl>
    </li>
  )
}

function Day({ g, n, open, kind }: { g: DayGroup; n: number; open: boolean; kind: string }) {
  const { leagues, href } = useApp()
  const r = g.recap
  return (
    <section className="card rday" aria-labelledby={`d-${kind}-${g.date}`}>
      <h3 id={`d-${kind}-${g.date}`}>{dayLong(g.date)} <span className="cnt">&middot; {plural(r.matches, 'match', 'matches')}</span></h3>
      <Stats r={r} n={n} />
      {r.bias !== null && Math.abs(r.bias) >= 0.05 && (
        <p className="hint">On average {Math.abs(r.bias).toFixed(1)} {Math.abs(r.bias) === 1 ? 'shot' : 'shots'} {r.bias > 0 ? 'more' : 'fewer'} than we expected.</p>
      )}
      <details className="rmatches" open={open}>
        <summary>{plural(r.matches, 'match', 'matches')}: predicted against actual</summary>
        <ul className="rlist">
          {g.verdicts.map((v) => {
            const l = leagues.get(v.r.league)
            return <Row key={v.r.id} v={v} n={n} href={href({ date: v.r.date, m: v.r.id }, '/')} league={l ?? { id: v.r.league, name: v.r.league, country: '' }} />
          })}
        </ul>
      </details>
    </section>
  )
}

function Group({ kind, title, intro, rows, n, empty }: { kind: 'official' | 'xp'; title: string; intro: string; rows: DayGroup[]; n: number; empty: string }) {
  const all = recap(rows.flatMap((g) => g.verdicts))
  const xp = kind === 'xp'
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
          {rows.map((g, i) => <Day key={g.date} g={g} n={n} open={i < 2} kind={kind} />)}
        </>
      )}
    </section>
  )
}

export function ResultsPage() {
  const { ds, state, set, totalStart } = useApp()
  const [lo, hi] = ds.meta.thresholds.total
  const all = ds.results?.results ?? []
  const official = byDay(all.filter((r) => r.experimental !== true), state.n, totalStart)
  const xp = byDay(all.filter((r) => r.experimental === true), state.n, totalStart)
  const hasXp = xp.length > 0 || ds.meta.leagues.some((l) => l.featured) || !!ds.track?.records.experimental
  return (
    <div className="page">
      <h1>Results</h1>
      <p className="lede">What we said before kick-off against what happened, match by match, for the last 30 days. Move the shots threshold to re-check every call. Days are match days at the venue.</p>
      <div className="card trackctl">
        <Stepper id="res-n" label="Shots" value={state.n} min={lo} max={hi} onChange={(v) => set({ n: v }, { replace: true })} />
      </div>
      <Group
        kind="official"
        title="League matches (official record)"
        intro="Predictions saved before kick-off, settled from the published results. These are the numbers the track record counts."
        rows={official}
        n={state.n}
        empty="Results are published about twice a week, then settled by the next update. The first finished matches with a saved prediction will show here as soon as they are in."
      />
      {hasXp && (
        <Group
          kind="xp"
          title="Champions League"
          intro="Experimental: built from each club's domestic form, never checked on this competition, and results are typed in by hand, so they lag. These numbers are kept apart and never count in the official record."
          rows={xp}
          n={state.n}
          empty="Champions League results are entered by hand after each matchday, so they appear a little after the matches. None have been entered yet."
        />
      )}
      <p className="hint">&ldquo;Floor&rdquo; is the highest shot count we were at least 80% sure the match would reach. &ldquo;Average miss&rdquo; is how far the actual total landed from our expected total, in shots, ignoring direction. Lineups, injuries and red cards are not in the model.</p>
    </div>
  )
}
