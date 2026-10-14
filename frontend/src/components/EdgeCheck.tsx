import { useId, useState } from 'react'
import { useApp } from '../lib/app'
import { pct } from '../lib/curve'
import { UNDER_CAVEAT } from '../lib/config'
import {
  MARKETS, MARKET_LABEL, MAX_CHECKS, ODDS_FORMATS, evaluate, fmtEv, fmtGap, fmtPct, lineRange, parseOdds,
} from '../lib/edge'
import type { Check, Market, OddsFormat, Side } from '../lib/edge'
import { checksStore, oddsFormatStore } from '../lib/storage'
import type { Item } from '../lib/types'
import { isLimited, useStore } from './ui'

const PLACEHOLDER: Record<OddsFormat, string> = { decimal: 'e.g. 1.85', fractional: 'e.g. 5/6', american: 'e.g. -120' }
const FORMAT_LABEL: Record<OddsFormat, string> = { decimal: 'Decimal', fractional: 'Fractional', american: 'American' }

function Bars({ model, implied, modelLabel }: { model: number; implied: number; modelLabel: string }) {
  return (
    <div className="edgebars" aria-hidden="true">
      <span className="eb"><i className="m" style={{ width: `${Math.round(model * 100)}%` }} /><em>Model {pct(model)}</em></span>
      <span className="eb"><i className="p" style={{ width: `${Math.round(implied * 100)}%` }} /><em>{modelLabel} {pct(implied)}</em></span>
    </div>
  )
}

export function EdgeCheck({ item }: { item: Item }) {
  const { state, set, ds, totalStart, teamStart } = useApp()
  const uid = useId()
  const fmt = useStore(oddsFormatStore)
  const [saved, setSaved] = useState<string | null>(null)
  const f = item.fixture
  const hasTeam = !!f?.team
  const market: Market = state.mk !== 'total' && !hasTeam ? 'total' : state.mk
  const side: Side = state.sd

  const curve = market === 'total' ? item.curve : market === 'home' ? f?.team?.home.curve : f?.team?.away.curve
  const start = market === 'total' ? totalStart : teamStart
  const defLine = (market === 'total' ? ds.meta.thresholds.default_total : ds.meta.thresholds.default_team) + 0.5
  const line = state.ln ?? defLine
  const range = lineRange(curve?.length ?? 0, start)
  const nMin = Math.ceil(range.lo - 0.5), nMax = Math.floor(range.hi - 0.5)
  const n = Math.floor(line)
  const inRange = line >= range.lo && line <= range.hi

  const parsed = parseOdds(state.od, fmt)
  const showErr = state.od.trim() !== '' && !parsed.ok
  const res = parsed.ok ? evaluate({ curve, start, line, side, decimal: parsed.decimal }) : null
  const sideWord = side === 'over' ? 'Over' : 'Under'
  const lineText = line.toFixed(1)
  const xp = item.experimental || market !== 'total'

  const setMarket = (m: Market) => { setSaved(null); set({ mk: m, ln: null }, { replace: true }) }
  const save = () => {
    if (!res || !res.ok || !parsed.ok) return
    const c: Check = {
      id: `${item.id}-${Date.now().toString(36)}`, savedAt: new Date().toISOString(), fixtureId: item.id,
      home: item.home, away: item.away, league: ds.meta.leagues.find((l) => l.id === item.league)?.name ?? item.league,
      date: item.date, market, side, line, oddsText: state.od.trim(), decimal: parsed.decimal, model: res.model,
    }
    checksStore.set([...checksStore.get(), c].slice(-MAX_CHECKS))
    setSaved(`Saved: ${sideWord} ${lineText} ${MARKET_LABEL[market].toLowerCase()} at ${state.od.trim()}.`)
  }

  return (
    <div className="tabbody edge">
      <p className="hint edge-intro">
        Compare the model&rsquo;s chance with a bookmaker&rsquo;s price for a shots line. Lines end in .5, so there is no refund case:
        Over 24.5 means 25 or more shots.
      </p>

      <div className="notices">
        {xp && <p className="notice xp" role="note"><b>Experimental</b>{market !== 'total' ? 'Team shots are experimental and not yet validated per competition. ' : ''}{item.experimental ? 'This fixture is built from domestic form only. ' : ''}Trust it less.</p>}
        {isLimited(item) && <p className="notice ltd" role="note"><b>Limited data</b>One or both clubs have little data behind them. Treat this as a rough guide.</p>}
        {!item.odds && <p className="notice" role="note"><b>No odds used</b>The model did not see bookmaker odds for this match (the no-odds model scored it), so it knows less about how the market sees the game.</p>}
        {side === 'under' && UNDER_CAVEAT && <p className="notice" role="note"><b>Under side</b>{UNDER_CAVEAT}</p>}
      </div>

      <div className="edgeform">
        <div role="group" aria-label="Market" className="chipset">
          {MARKETS.map((m) => (
            <button key={m} type="button" className="chip" aria-pressed={market === m} disabled={m !== 'total' && !hasTeam} onClick={() => setMarket(m)}>
              {m === 'total' ? 'Total shots' : m === 'home' ? `${item.home} shots` : `${item.away} shots`}{m !== 'total' ? ' (experimental)' : ''}
            </button>
          ))}
        </div>
        {!hasTeam && <p className="hint">Team markets need per-team curves, which this match does not have.</p>}

        <div role="group" aria-label="Side" className="chipset">
          {(['over', 'under'] as const).map((s) => (
            <button key={s} type="button" className="chip" aria-pressed={side === s} onClick={() => { setSaved(null); set({ sd: s }, { replace: true }) }}>{s === 'over' ? 'Over' : 'Under'}</button>
          ))}
        </div>

        <div className="stepper">
          <label htmlFor={`${uid}-ln`} className="lbl">Line</label>
          <div className="steprow">
            <button type="button" className="sq" aria-label="Lower line" disabled={n <= nMin} onClick={() => set({ ln: n - 0.5 }, { replace: true })}>&minus;</button>
            <input id={`${uid}-ln`} type="range" min={nMin} max={nMax} step={1} value={Math.min(nMax, Math.max(nMin, n))} aria-valuetext={`${lineText}`} onChange={(e) => set({ ln: Number(e.target.value) + 0.5 }, { replace: true })} />
            <button type="button" className="sq" aria-label="Higher line" disabled={n >= nMax} onClick={() => set({ ln: n + 1.5 }, { replace: true })}>+</button>
            <output htmlFor={`${uid}-ln`}>{lineText}</output>
          </div>
        </div>

        <div className="oddsfield">
          <label htmlFor={`${uid}-od`} className="lbl">Bookmaker odds for {sideWord} {lineText}</label>
          <div role="group" aria-label="Odds format" className="chipset small">
            {ODDS_FORMATS.map((o) => (
              <button key={o} type="button" className="chip" aria-pressed={fmt === o} onClick={() => oddsFormatStore.set(o)}>{FORMAT_LABEL[o]}</button>
            ))}
          </div>
          <input
            id={`${uid}-od`} type="text" inputMode="text" autoComplete="off" spellCheck={false} maxLength={10}
            value={state.od} placeholder={PLACEHOLDER[fmt]}
            aria-invalid={showErr} aria-describedby={showErr ? `${uid}-err` : undefined}
            onChange={(e) => { setSaved(null); set({ od: e.target.value.replace(/[^\d.+\-/]/g, '') }, { replace: true }) }}
          />
          {showErr && !parsed.ok && <p id={`${uid}-err`} className="field-err" role="alert">{parsed.error}</p>}
        </div>
      </div>

      <section className="edgeres" aria-labelledby={`${uid}-rh`} aria-live="polite">
        <h3 id={`${uid}-rh`}>Result</h3>
        {!inRange || (res && !res.ok) ? (
          <p className="notice" role="note">
            The published curve covers lines from {range.lo.toFixed(1)} to {range.hi.toFixed(1)} for this market. {lineText} is outside it, and we do not extrapolate.
          </p>
        ) : !res || !res.ok ? (
          <p className="hint">Enter the bookmaker&rsquo;s odds to see how they compare.</p>
        ) : (
          <>
            <p className="edgeread"><b>{res.reading}</b></p>
            <Bars model={res.model} implied={res.implied} modelLabel="Price implies" />
            <dl className="edgedl">
              <div><dt>Model chance of {sideWord} {lineText}</dt><dd>{fmtPct(res.model)}</dd></div>
              <div><dt>Price implies</dt><dd>{fmtPct(res.implied)}<small>1 divided by the odds. Includes the bookmaker&rsquo;s margin, so the true market chance is a bit lower.</small></dd></div>
              <div><dt>Gap</dt><dd>{fmtGap(res.gapPts)} points<small>Model minus price.</small></dd></div>
              <div><dt>Fair odds</dt><dd>{res.fair === null ? 'n/a' : res.fair.toFixed(2)}<small>1 divided by the model chance, in decimal.</small></dd></div>
              <div><dt>Expected value per 1 staked</dt><dd>{fmtEv(res.ev)}<small>Average result per unit staked if the model&rsquo;s chance were exactly right. It will not be.</small></dd></div>
            </dl>
            <div className="edgeactions">
              <button type="button" className="quiet" onClick={save} disabled={item.finished}>Save to My checks</button>
              <a href="#/checks">Open My checks</a>
            </div>
            {item.finished && <p className="hint">This match has finished, so it cannot be saved.</p>}
            {saved && <p className="hint" role="status">{saved} Kept only in this browser.</p>}
          </>
        )}
      </section>

      <p className="disclaimer">Estimates only, not advice. Bookmakers price in line-ups and news that the model does not see.</p>
    </div>
  )
}
