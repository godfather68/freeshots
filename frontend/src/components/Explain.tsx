import { buildWaterfall, matchesNote, signedShots, volatilityNote } from '../lib/explain'
import type { Item } from '../lib/types'
import { ScrollRegion } from './ui'

/** "Why this number": baseline to expected total as a diverging bar list, plus a "how solid is this" block. Hidden without data. */
export function Explain({ item }: { item: Item }) {
  const ex = item.fixture?.explain
  const w = buildWaterfall(ex)
  if (!ex || !w) return null
  const sig = ex.signals ?? null
  const solid: string[] = []
  if (sig) {
    const a = matchesNote(item.home, sig.home_matches)
    const b = matchesNote(item.away, sig.away_matches)
    if (a) solid.push(a)
    if (b) solid.push(b)
    if (typeof sig.odds_used === 'boolean') solid.push(sig.odds_used ? 'Bookmaker odds were used by the model.' : 'No bookmaker odds were used: the separate no-odds model scored this match.')
    const va = volatilityNote(item.home, sig.home_volatility)
    const vb = volatilityNote(item.away, sig.away_volatility)
    if (va) solid.push(va)
    if (vb) solid.push(vb)
    if (typeof sig.model_gap === 'number' && Number.isFinite(sig.model_gap)) {
      solid.push(`Cross-check gap: ${Math.abs(sig.model_gap).toFixed(1)} shots between two ways of estimating this match. Smaller is steadier.`)
    }
  }
  return (
    <section className="why" aria-labelledby="why-h">
      <h3 id="why-h">Why this number</h3>
      <ol className="wf" aria-label="From the starting point to the expected total">
        {w.baseline && (
          <li className="wf-base">
            <span className="wf-main"><b>Start: {w.baseline.value.toFixed(1)} shots</b><small>{w.baseline.label}</small></span>
          </li>
        )}
        {w.rows.map((r) => {
          const up = r.effect >= 0
          const pctW = Math.min(100, (Math.abs(r.effect) / w.maxAbs) * 100)
          return (
            <li key={r.key} className={`wf-row ${up ? 'up' : 'down'}`}>
              <span className="wf-main">
                <b>{r.label}</b>
                {r.detail && <small>{r.detail}</small>}
              </span>
              <span className="wf-bar" aria-hidden="true">
                <i style={{ width: `${pctW / 2}%`, [up ? 'left' : 'right']: '50%' }} />
              </span>
              <span className="wf-val">
                <span aria-hidden="true">{up ? '▲' : '▼'}</span> {signedShots(r.effect)}<span className="sr"> shots, {up ? 'more shots' : 'fewer shots'}</span>
              </span>
            </li>
          )
        })}
        {w.total !== null && (
          <li className="wf-total"><span className="wf-main"><b>= about {Math.round(w.total)} shots expected</b>{w.baseline ? <small>{w.total.toFixed(1)} before rounding</small> : null}</span></li>
        )}
      </ol>
      <details className="alt">
        <summary>Show as a table</summary>
        <ScrollRegion label="Why this number, table">
          <table>
            <caption className="sr">Each factor, its effect on expected shots, and the running total</caption>
            <thead><tr><th scope="col">Step</th><th scope="col">Effect (shots)</th><th scope="col">Running total</th></tr></thead>
            <tbody>
              {w.baseline && <tr><th scope="row">{w.baseline.label}</th><td>start</td><td>{w.baseline.value.toFixed(1)}</td></tr>}
              {w.rows.map((r) => <tr key={r.key}><th scope="row">{r.label}</th><td>{signedShots(r.effect)}</td><td>{r.running.toFixed(1)}</td></tr>)}
            </tbody>
          </table>
        </ScrollRegion>
      </details>
      {solid.length > 0 && (
        <div className="solid">
          <h4>How solid is this?</h4>
          <ul>{solid.map((s) => <li key={s}>{s}</li>)}</ul>
        </div>
      )}
    </section>
  )
}
