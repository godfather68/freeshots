import { useApp } from '../lib/app'
import { pct, probAt } from '../lib/curve'
import { dayLong } from '../lib/format'
import { summariseDay } from '../lib/items'
import { Badge } from './ui'

/** Compact summary of the selected day: how many matches, how many clear 70%, and the best pick. */
export function Hero() {
  const { items, date, today, state, totalStart, href } = useApp()
  const s = summariseDay(items, date, state.n, totalStart)
  const best = s.best
  const bp = best ? probAt(best.curve, totalStart, state.n) : null
  return (
    <section className="hero" aria-labelledby="hero-h">
      <svg className="hero-pitch" viewBox="0 0 400 200" preserveAspectRatio="xMaxYMid slice" aria-hidden="true" focusable="false">
        <rect x="150" y="10" width="240" height="180" rx="4" />
        <line x1="270" y1="10" x2="270" y2="190" />
        <circle cx="270" cy="100" r="34" />
        <circle cx="270" cy="100" r="2.5" className="spot" />
        <rect x="150" y="55" width="40" height="90" />
        <rect x="350" y="55" width="40" height="90" />
        <path d="M190 80a22 22 0 0 1 0 40M350 80a22 22 0 0 0 0 40" />
      </svg>
      <div className="hero-main">
        <p className="eyebrow">{date === today ? 'Today' : 'Match day'}</p>
        <h2 id="hero-h">{dayLong(date)}</h2>
      </div>
      <dl className="hero-stats">
        <div>
          <dt>Matches</dt>
          <dd><b>{s.matches}</b></dd>
        </div>
        <div>
          <dt>Clear 70% at {state.n}+</dt>
          <dd><b>{s.clear70}</b></dd>
        </div>
        <div className="hero-best">
          <dt>{s.bestExperimental ? 'Best pick (experimental)' : 'Best pick'}</dt>
          <dd>
            {best ? (
              <a href={href({ m: best.id, date })} aria-label={`Best pick: ${best.home} v ${best.away}, ${pct(bp)}`}>
                <span className="pt"><Badge name={best.home} size={22} /><Badge name={best.away} size={22} /></span>
                <span className="bn">{best.home} <i>v</i> {best.away}</span>
                <b className="bp">{pct(bp)}</b>
              </a>
            ) : (
              <span className="none">No upcoming matches</span>
            )}
          </dd>
        </div>
      </dl>
    </section>
  )
}
