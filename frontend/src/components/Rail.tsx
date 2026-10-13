import { useApp } from '../lib/app'
import { pct, probAt } from '../lib/curve'
import { dayLong } from '../lib/format'
import { topPicksInfo } from '../lib/items'
import { entryFor, pc } from '../lib/track'
import { guideStore } from '../lib/storage'
import { Badge, ItemTags, ProbPill, useStore } from './ui'

export function GuideSteps({ n }: { n: number }) {
  return (
    <ol className="steps">
      <li>
        <h3>Read the headline</h3>
        <p><b className="eg">70% chance of {n}+ shots</b> means that in about 7 of 10 similar matches, both teams together take at least {n} shots (on target, off target and blocked). It is a chance, not a promise.</p>
      </li>
      <li>
        <h3>Bar and &ldquo;80% sure&rdquo;</h3>
        <p>The <b>bar</b> shows that chance; the tick marks 70% and the fill turns green past it. <b>80% sure: 27+</b> is a safe floor: the model is 80% confident the match reaches at least 27 shots.</p>
      </li>
      <li>
        <h3>Change the question</h3>
        <p>Move the <b>shots</b> control to change the number you care about, then narrow by minimum chance, day or league. Every view has its own web address, so you can bookmark or share it.</p>
      </li>
    </ol>
  )
}

export function Guide() {
  const g = useStore(guideStore)
  const { state } = useApp()
  if (g.dismissed) return null
  return (
    <section className="card guide" id="guide" tabIndex={-1} aria-labelledby="guide-h">
      <div className="cardhead">
        <h2 id="guide-h">New here? Three things to know</h2>
        <button type="button" className="quiet" onClick={() => guideStore.set({ dismissed: true })}>Hide guide</button>
      </div>
      <GuideSteps n={state.n} />
      <p className="hint">Tap a <span className="qmark" aria-hidden="true">?</span><span className="sr">question-mark button</span> next to any term for a plain-English explanation. <a href="#/how">Full explanation</a></p>
    </section>
  )
}

export function TopPicks() {
  const { items, date, state, totalStart, href } = useApp()
  const { picks, onlyExperimental } = topPicksInfo(items, date, state.n, totalStart, 5)
  return (
    <section className="card picks-card" aria-labelledby="picks-h">
      <div className="cardhead">
        <h2 id="picks-h"><svg className="ico" viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path d="M3 1.5v13M3 2.5h9l-2 3 2 3H3" /></svg>Top picks, {dayLong(date)}</h2>
      </div>
      <p className="hint">Highest chance of {state.n}+ shots.</p>
      {onlyExperimental && <p className="notice xp" role="note">Only experimental predictions today, so they are shown here. Trust them less.</p>}
      {picks.length === 0 ? (
        <p className="hint">No upcoming fixtures on this day.</p>
      ) : (
        <ol className="picks">
          {picks.map((it) => (
            <li key={it.id}>
              <a href={href({ m: it.id, date })} aria-label={`${it.home} v ${it.away}, ${pct(probAt(it.curve, totalStart, state.n))}${it.experimental ? ', experimental' : ''}`}>
                <span className="pt"><Badge name={it.home} size={18} /><Badge name={it.away} size={18} /></span>
                <span className="pn">{it.home} <span className="v">v</span> {it.away}{it.experimental && onlyExperimental ? <ItemTags item={it} /> : null}</span>
                <ProbPill p={probAt(it.curve, totalStart, state.n)} />
              </a>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

export function TrackSummary() {
  const { ds, state, href } = useApp()
  const t = ds.track
  const live = t?.records.live
  const e = entryFor(t?.records.backfill, state.n)
  return (
    <section className="card proof-card" aria-labelledby="track-h">
      <h2 id="track-h">Track record</h2>
      {!t ? (
        <p className="hint">Track record not published yet.</p>
      ) : (
        <>
          {e ? (
            <p>
              <b className="big">{pc(e.entry.hit_rate)}</b> of {e.entry.n.toLocaleString('en-GB')} matches reached {e.n}+ shots when the model called them 70%+ (it said {pc(e.entry.mean_stated)} on average).
              <small className="hint"> {t.records.backfill.label}{e.exact ? '' : `. Nearest published threshold is ${e.n}+.`}</small>
            </p>
          ) : (
            <p className="hint">No backfill numbers for this threshold.</p>
          )}
          <p className="livebox">
            <b>Live picks:</b> {live?.settled ?? 0} settled, {live?.pending ?? 0} waiting for kick-off.
            {(live?.settled ?? 0) === 0 && <small className="hint"> Nothing to judge yet; results appear here once the first saved picks finish.</small>}
          </p>
        </>
      )}
      <p><a href={href({}, '/track')}>Full track record</a></p>
    </section>
  )
}
