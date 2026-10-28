import { useMemo } from 'react'
import { useApp } from '../lib/app'
import { catalogOf, contextOf, isPlayoffFormat, isStrong, labelsOf, phaseText, sideLine } from '../lib/surge'
import type { Catalog } from '../lib/surge'
import type { Item, SurgeSignal } from '../lib/types'
import { Help } from './ui'

export function useSurgeCatalog(): Catalog | null {
  const { ds } = useApp()
  return useMemo(() => catalogOf(ds.surge), [ds.surge])
}

/** Signal chips: direction in a mark and in words (screen readers), never by colour alone. */
export function SurgeChips({ boosts, dampers, label }: { boosts: SurgeSignal[]; dampers: SurgeSignal[]; label: string }) {
  if (boosts.length + dampers.length === 0) return null
  return (
    <ul className="schips" aria-label={label}>
      {boosts.map((s) => (
        <li key={s.id} className="schip boost" title={s.explanation}>
          <span aria-hidden="true">▲</span> {s.label}<span className="sr"> (raises the chance of a surge)</span>
        </li>
      ))}
      {dampers.map((s) => (
        <li key={s.id} className="schip damper" title={s.explanation}>
          <span aria-hidden="true">▼</span> {s.label}<span className="sr"> (lowers the chance of a surge)</span>
        </li>
      ))}
    </ul>
  )
}

/** "Strong candidate" badge: several signals on a route where stacking held up out of sample. Words, not colour. */
export function StrongBadge() {
  return (
    <span className="tag strong" title="Two or more signals, on a route where that combination held up out of sample">
      <span aria-hidden="true">▲▲</span> Strong candidate
    </span>
  )
}

/** Small "Surge signals" row in the match panel. Hidden when the fixture was not assessed or surge.json is missing. */
export function SurgeRow({ item }: { item: Item }) {
  const cat = useSurgeCatalog()
  const got = labelsOf(item.fixture?.surge, cat)
  if (!got || item.finished) return null
  const none = got.boosts.length + got.dampers.length === 0
  return (
    <section className="surgerow" aria-labelledby="surge-h">
      <h3 id="surge-h">Surge signals <Help term="surge" /></h3>
      {isStrong(item.fixture?.surge) && (
        <p className="sstrong"><StrongBadge /> <span className="hint">Two or more signals at once: past matches like this surged clearly more often than our curve said. <a href="#/surge">See the numbers</a></span></p>
      )}
      {none ? (
        <p className="hint">None for this match: nothing in it has gone with more (or fewer) surges than our curve said.</p>
      ) : (
        <SurgeChips boosts={got.boosts} dampers={got.dampers} label={`Surge signals for ${item.home} v ${item.away}`} />
      )}
      <p className="hint">A lean from past matches, not a promise: the chances above are unchanged. <a href="#/surge">What the signals mean</a></p>
    </section>
  )
}

/** League table before the match and the season phase (fixture `context`). Hidden when not published or finished. */
export function TableContextRow({ item }: { item: Item }) {
  const c = contextOf(item.fixture)
  if (!c || item.finished) return null
  const phase = phaseText(c)
  return (
    <section className="ctxrow" aria-labelledby="ctx-h">
      <h3 id="ctx-h">League table before the match <Help term="phase" /></h3>
      <dl className="ctxlist">
        <div><dt>{item.home}</dt><dd>{sideLine(c.home)}</dd></div>
        <div><dt>{item.away}</dt><dd>{sideLine(c.away)}</dd></div>
      </dl>
      <p className="hint">
        {phase && <><span className="tag phase">{phase}</span> </>}
        {isPlayoffFormat(c) && <>Play-off format: no fixed season end, so no games-left count. </>}
        Our own table from the results so far (points, then goal difference); context only, the chances above are unchanged.
      </p>
    </section>
  )
}
