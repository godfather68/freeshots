import { useMemo, useRef } from 'react'
import { useApp } from '../lib/app'
import { MARKET_LABEL, checksCsv, fmtPct, settleCheck } from '../lib/edge'
import { checksStore } from '../lib/storage'
import { dayLong } from '../lib/format'
import { ScrollRegion, useStore } from '../components/ui'

const OUTCOME_LABEL = { hit: '✓ Hit', miss: '✗ Miss', pending: 'Waiting for the result' } as const

export function ChecksPage() {
  const { itemById, href } = useApp()
  const checks = useStore(checksStore)
  const area = useRef<HTMLTextAreaElement>(null)
  const rows = useMemo(
    () => [...checks].reverse().map((c) => ({ c, outcome: settleCheck(c, itemById.get(c.fixtureId)?.actual ?? null) })),
    [checks, itemById],
  )
  const csv = useMemo(() => checksCsv(rows), [rows])
  const settled = rows.filter((r) => r.outcome !== 'pending')
  const hits = settled.filter((r) => r.outcome === 'hit').length
  return (
    <div className="page narrow">
      <h1>My checks</h1>
      <p className="hint">Odds checks you saved. They live only in this browser (local storage): nothing is sent anywhere, and clearing site data removes them.</p>
      {rows.length === 0 ? (
        <div className="card empty">
          <p>No saved checks yet. Open a match, choose the <b>Odds check</b> tab, enter a price and save it.</p>
          <p><a href={href({}, '/')}>Back to the matches</a></p>
        </div>
      ) : (
        <>
          <p aria-live="polite">
            {rows.length} saved. {settled.length === 0 ? 'None has a result yet.' : `${hits} of ${settled.length} settled ${settled.length === 1 ? 'check' : 'checks'} came in.`}
          </p>
          <ScrollRegion label="Saved checks">
            <table className="checks">
              <caption className="sr">Saved odds checks</caption>
              <thead>
                <tr><th scope="col">Match</th><th scope="col">Check</th><th scope="col">Odds</th><th scope="col">Model</th><th scope="col">Result</th><th scope="col"><span className="sr">Remove</span></th></tr>
              </thead>
              <tbody>
                {rows.map(({ c, outcome }) => (
                  <tr key={c.id}>
                    <th scope="row">
                      <a href={href({ m: c.fixtureId }, '/')}>{c.home} v {c.away}</a>
                      <small>{c.league}, {dayLong(c.date)}</small>
                    </th>
                    <td>{c.side === 'over' ? 'Over' : 'Under'} {c.line.toFixed(1)}<small>{MARKET_LABEL[c.market]}</small></td>
                    <td>{c.oddsText}</td>
                    <td>{fmtPct(c.model)}</td>
                    <td><span className={`outcome-tag ${outcome}`}>{OUTCOME_LABEL[outcome]}</span></td>
                    <td><button type="button" className="quiet" aria-label={`Remove check for ${c.home} v ${c.away}`} onClick={() => checksStore.set(checks.filter((x) => x.id !== c.id))}>Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </ScrollRegion>
          <h2>Copy as CSV</h2>
          <label htmlFor="csv" className="lbl">Spreadsheet text, one row per check</label>
          <textarea id="csv" ref={area} readOnly rows={Math.min(10, rows.length + 2)} value={csv} className="csvbox" onFocus={(e) => e.currentTarget.select()} />
          <p><button type="button" className="quiet" onClick={() => area.current?.select()}>Select all</button></p>
          <p><button type="button" className="quiet" onClick={() => { if (window.confirm('Remove every saved check from this browser?')) checksStore.set([]) }}>Remove all checks</button></p>
        </>
      )}
      <p className="disclaimer">Estimates only, not advice. Outcomes are the published final shot counts against your line.</p>
    </div>
  )
}
