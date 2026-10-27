import { useMemo } from 'react'
import { useApp } from '../lib/app'
import { catalogOf, labelsOf } from '../lib/surge'
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

/** Small "Surge signals" row in the match panel. Hidden when the fixture was not assessed or surge.json is missing. */
export function SurgeRow({ item }: { item: Item }) {
  const cat = useSurgeCatalog()
  const got = labelsOf(item.fixture?.surge, cat)
  if (!got || item.finished) return null
  const none = got.boosts.length + got.dampers.length === 0
  return (
    <section className="surgerow" aria-labelledby="surge-h">
      <h3 id="surge-h">Surge signals <Help term="surge" /></h3>
      {none ? (
        <p className="hint">None for this match: nothing in it has gone with more (or fewer) surges than our curve said.</p>
      ) : (
        <SurgeChips boosts={got.boosts} dampers={got.dampers} label={`Surge signals for ${item.home} v ${item.away}`} />
      )}
      <p className="hint">A lean from past matches, not a promise: the chances above are unchanged. <a href="#/surge">What the signals mean</a></p>
    </section>
  )
}
