// Surge Watch, 2026-10-28: how surge candidates turned out (results.json `surge`), table context and season phase
// (board.json `context`), the phase filter and the candidate order.
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import App from './App'
import { board, dataset, meta, results, surge } from './test-fixtures'
import {
  isStrong, phaseOutcomes, stackRows,
  catalogOf, contextOf, ordinal, phaseText, positionsShort, settledAssessed, sideLine, signalOutcomes, sortCandidates,
  surgeCandidates, tallyRate, totals, weekOf, weeklyOutcomes,
} from './lib/surge'
import { buildItems } from './lib/items'
import { parseState, stateToParams } from './lib/url'
import type { Dataset, Fixture, Result, StackCell, Stacking, TableContext, TableSide } from './lib/types'

const START = meta.thresholds.total[0]
const side = (pos: number | null, o: Partial<TableSide> = {}): TableSide =>
  ({ pos, of: 20, pts: 21, gp: 9, left: 29, to_top: 2, to_safety: 12, ...o })
const CTX: Record<string, TableContext> = {
  fx001: { phase: 'run_in', home: side(3, { left: 5, gp: 33 }), away: side(17, { pts: 30, to_top: 40, to_safety: -1, left: 6, gp: 32 }) },
  fx005: { phase: 'middle', home: side(1, { to_top: 0 }), away: side(12, { to_safety: 2 }) },
}
const withContext = (ctx: Record<string, TableContext | unknown> = CTX): Dataset => ({
  ...dataset,
  board: { ...board, fixtures: board.fixtures.map((f) => (ctx[f.id] ? { ...f, context: ctx[f.id] as Fixture['context'] } : f)) },
})

const ordinary = results.results.filter((r) => !r.experimental)
/** Settled results with labels: `flagged` with a boost, `plain` assessed without; every k-th surges. */
function labelled(flagged: number, plain: number, surgeEvery = 3): Result[] {
  const day = (i: number) => new Date(Date.UTC(2026, 8, 14 + 2 * i)).toISOString().slice(0, 10)
  const pick = Array.from({ length: flagged + plain }, (_, i) => ({ ...ordinary[i % ordinary.length], id: `sg${String(i).padStart(3, "0")}`, date: day(i) }))
  return pick.map((r, i) => {
    const surged = i % surgeEvery === 0
    const total = r.expected + (surged ? 6 : 1)
    return { ...r, actual: { total, home: total - 10, away: 10 },
      surge: i < flagged ? { signals: i % 2 ? ['h2h_above'] : ['h2h_above', 'home_mismatch'], against: [] } : { signals: [], against: i % 2 ? ['winter'] : [] } }
  })
}
const withResults = (rs: Result[], base: Dataset = dataset): Dataset => {
  const ids = new Set(rs.map((r) => r.id))
  return { ...base, results: { schema: 1, results: [...rs, ...results.results.filter((r) => !ids.has(r.id))] } }
}

async function mount(hash: string, ds: Dataset = dataset) {
  window.location.hash = hash
  render(<App zone="Africa/Brazzaville" today="2026-10-12" now={Date.parse('2026-10-12T10:00:00Z')} load={() => Promise.resolve(ds)} />)
  await screen.findByRole('heading', { level: 1 }, { timeout: 3000 })
}
const rows = () => [...document.querySelectorAll('li.srow')] as HTMLElement[]
const outcomes = () => screen.getByRole('region', { name: 'How surge candidates turned out' })

describe('table context helpers', () => {
  it('words positions, gaps and phases', () => {
    expect([1, 2, 3, 4, 11, 12, 13, 21, 22, 103].map(ordinal)).toEqual(['1st', '2nd', '3rd', '4th', '11th', '12th', '13th', '21st', '22nd', '103rd'])
    expect(sideLine(side(3))).toBe('3rd of 20 · 21 pts · 2 behind the leader')
    expect(sideLine(side(1, { to_top: 0 }))).toBe('1st of 20 · 21 pts · top of the table')
    expect(sideLine(side(2, { to_top: 0 }))).toBe('2nd of 20 · 21 pts · level on points with the leader')
    expect(sideLine(side(18, { pts: 1, to_top: 20, to_safety: 0 }))).toBe('18th of 20 · 1 pt · 20 behind the leader · in the drop zone')
    expect(sideLine(side(15, { to_safety: 1 }))).toContain('1 pt above the drop zone')
    expect(sideLine(side(null, { gp: 0, pts: 0 }))).toBe('No league game yet · 20 teams')
    expect(phaseText(CTX.fx001)).toBe('Run-in: 5 games left')
    expect(phaseText(CTX.fx005)).toBe('Mid-season')
    expect(phaseText({ ...CTX.fx005, phase: null })).toBeNull()
    expect(positionsShort(CTX.fx001)).toBe('3rd v 17th of 20')
  })

  it('ignores a context that is not in the published shape', () => {
    expect(contextOf({ context: CTX.fx001 })).toBe(CTX.fx001)
    expect(contextOf({})).toBeNull()
    expect(contextOf({ context: null })).toBeNull()
    expect(contextOf({ context: { ...CTX.fx001, phase: 'playoffs' } as unknown as TableContext })).toBeNull()
    expect(contextOf({ context: { ...CTX.fx001, home: { ...side(3), pts: '21' } } as unknown as TableContext })).toBeNull()
  })

  it('reads and writes the phase filter and the order in the URL, ignoring bad values', () => {
    const s = parseState(new URLSearchParams('ph=run_in&ss=time'), meta)
    expect([s.ph, s.ss]).toEqual(['run_in', 'time'])
    expect(stateToParams(s, meta)).toMatchObject({ ph: 'run_in', ss: 'time' })
    const d = parseState(new URLSearchParams('ph=winter&ss=random'), meta)
    expect([d.ph, d.ss]).toEqual(['', 'signals'])
    expect(stateToParams(d, meta)).not.toHaveProperty('ph')
    expect(stateToParams(d, meta)).not.toHaveProperty('ss')
  })

  it('sorts candidates by signals, kick-off or chance', () => {
    const cs = surgeCandidates(buildItems(dataset), catalogOf(surge), 24, START)
    expect(sortCandidates(cs, 'signals').map((c) => c.item.id)).toEqual(cs.map((c) => c.item.id))
    const t = sortCandidates(cs, 'time').map((c) => `${c.item.date}|${c.item.id}`)
    expect([...t].sort()).toEqual(t)
    const p = sortCandidates(cs, 'prob').map((c) => c.p ?? -1)
    expect([...p].sort((a, b) => b - a)).toEqual(p)
  })
})

describe('outcome helpers', () => {
  it('counts surges as actual >= expected + margin, assessed ordinary results only, by week and by signal', () => {
    const rs = labelled(4, 2)
    const exp = { ...results.results.find((r) => r.experimental)!, surge: { signals: ['h2h_above'], against: [] } }
    const s = settledAssessed([...rs, exp, ordinary[0]], 5)
    expect(s).toHaveLength(6)
    const edge = settledAssessed([{ ...rs[1], actual: { total: rs[1].expected + 5, home: 1, away: rs[1].expected + 4 } }], 5)
    expect(edge[0].surged).toBe(true)
    const t = totals(s)
    expect(t.flagged).toEqual({ n: 4, surged: 2 })
    expect(t.other).toEqual({ n: 2, surged: 0 })
    expect(tallyRate(t.flagged)).toBe(0.5)
    expect(tallyRate({ n: 0, surged: 0 })).toBeNull()
    const weeks = weeklyOutcomes(s)
    expect(weeks.map((w) => w.date)).toEqual([...new Set(s.map((x) => weekOf(x.r.date)))].sort())
    expect(weeks.reduce((a, w) => a + w.flagged.n + w.other.n, 0)).toBe(6)
    const per = Object.fromEntries(signalOutcomes(s, surge).map((x) => [x.signal.id, x.tally]))
    expect(per.h2h_above.n).toBe(4)
    expect(per.home_mismatch.n).toBe(2)
    expect(per.winter.n).toBe(1)
    expect(per.dead_rubber.n).toBe(0)
  })

  it('takes the Monday of a venue date', () => {
    expect(weekOf('2026-10-12')).toBe('2026-10-12')
    expect(weekOf('2026-10-18')).toBe('2026-10-12')
    expect(weekOf('2026-11-01')).toBe('2026-10-26')
  })
})

describe('How surge candidates turned out', () => {
  it('has an empty state while nothing flagged is settled', async () => {
    await mount('#/surge')
    expect(within(outcomes()).getByRole('heading', { name: 'No surge candidate has been settled yet' })).toBeInTheDocument()
    expect(within(outcomes()).queryByRole('img')).toBeNull()
  })

  it('shows totals, a small-sample note instead of a chart, per-signal counts and the latest candidates', async () => {
    await mount('#/surge', withResults(labelled(4, 3)))
    const sec = outcomes()
    expect(sec).toHaveTextContent('With 1+ signal2 of 4 (50%) surged')
    expect(sec).toHaveTextContent('Looked at, no signal1 of 3 (33%) surged')
    expect(within(sec).getByRole('heading', { name: 'Too few settled candidates to chart yet' })).toBeInTheDocument()
    expect(within(sec).queryByRole('img')).toBeNull()
    const bySignal = within(sec).getByRole('region', { name: 'Settled matches by signal' })
    const h2h = within(bySignal).getByRole('row', { name: /Head-to-head runs high/ })
    expect(within(h2h).getAllByRole('cell').map((c) => c.textContent)).toEqual(['4', '2 (small sample)'])
    expect(within(bySignal).getByRole('row', { name: /Nothing to play for/ })).toHaveTextContent('none yet')
    const latest = [...sec.querySelectorAll('li.ssettled')] as HTMLElement[]
    expect(latest).toHaveLength(4)
    const words = latest.map((li) => li.querySelector('.sres')!.textContent!.trim())
    expect(words.filter((w) => w.endsWith('Surged'))).toHaveLength(2)
    expect(words.filter((w) => w.endsWith('No surge'))).toHaveLength(2)
    expect(latest[0]).toHaveTextContent(/We expected\d+ shots/)
    expect(within(latest[0]).getByRole('link')).toHaveAttribute('href', expect.stringContaining('m='))
  })

  it('charts the weekly surge rate with a table alternative once there are enough', async () => {
    await mount('#/surge', withResults(labelled(12, 6)))
    const sec = outcomes()
    const chart = within(sec).getByRole('img', { name: /Weekly share of settled matches that ended 5\+ shots above/ })
    expect(chart.querySelector('desc')!.textContent).toMatch(/Week of .*: with a signal \d+ of \d+ \(\d+%\) surged; without/)
    expect(chart.querySelectorAll('circle.tdot').length).toBeGreaterThan(0)
    expect(chart.querySelectorAll('rect.tsq').length).toBeGreaterThan(0)
    expect(chart.querySelectorAll('circle.tdot.hollow').length).toBe(chart.querySelectorAll('circle.tdot').length) // all weeks are small here
    const table = within(sec).getByRole('region', { name: 'Weekly surge numbers' })
    const weeks = weeklyOutcomes(settledAssessed(withResults(labelled(12, 6)).results!.results, 5))
    expect(within(table).getAllByRole('row')).toHaveLength(weeks.length + 1)
    expect(sec).toHaveTextContent(/Hollow: fewer than 10 matches that week/)
    expect(within(sec).getAllByText(/\(small\)/).length).toBeGreaterThan(0)
    expect([...sec.querySelectorAll('li.ssettled')]).toHaveLength(10) // the latest ten only
  })
})

describe('table context on the Surge page and in the match panel', () => {
  it('shows a phase chip and both positions on candidate rows', async () => {
    await mount('#/surge', withContext())
    const first = rows()[0]
    expect(first.querySelector('.sctx')).toHaveTextContent('Run-in: 5 games leftLeague positions: 3rd v 17th of 20')
    expect(rows()[1].querySelector('.sctx')).toHaveTextContent('Mid-season')
  })

  it('filters by season phase and sorts by kick-off, in the URL', async () => {
    await mount('#/surge', withContext())
    const phase = screen.getByRole('combobox', { name: 'Season phase' })
    expect(within(phase).getByRole('option', { name: 'Run-in (1)' })).toBeInTheDocument()
    await userEvent.selectOptions(phase, 'run_in')
    await waitFor(() => expect(rows()).toHaveLength(1))
    expect(window.location.hash).toContain('ph=run_in')
    expect(screen.getByRole('status')).toHaveTextContent('1 match shown of 2')
    await userEvent.selectOptions(phase, 'late')
    expect(await screen.findByText('No candidate fits these filters')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(rows()).toHaveLength(2)
    const want = sortCandidates(surgeCandidates(buildItems(dataset), catalogOf(surge), 24, START), 'time').map((c) => `Open ${c.item.home} v ${c.item.away}`)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Order' }), 'time')
    await waitFor(() => expect(rows().map((r) => within(r).getAllByRole('link')[0].getAttribute('aria-label'))).toEqual(want))
    expect(window.location.hash).toContain('ss=time')
    // the "2+ signals" filter is still there
    await userEvent.click(screen.getByRole('button', { name: /^2\+ signals/ }))
    expect(rows()).toHaveLength(1)
  })

  it('shows the table before the match in the panel, with a glossary entry', async () => {
    await mount('#/match/fx001', withContext())
    const sec = screen.getByRole('region', { name: /League table before the match/ })
    const fx = board.fixtures.find((f) => f.id === 'fx001')!
    expect(within(sec).getByText(fx.home).nextSibling).toHaveTextContent('3rd of 20 · 21 pts · 2 behind the leader')
    expect(sec).toHaveTextContent('17th of 20 · 30 pts · 40 behind the leader · in the drop zone')
    expect(sec).toHaveTextContent('Run-in: 5 games left')
    expect(within(sec).getByText(/What does “Table position and season phase” mean\?/)).toBeInTheDocument()
  })

  it('shows the phase of a play-off format and says it has no games-left count', async () => {
    await mount('#/match/fx005', withContext({ fx005: { ...CTX.fx005, phase: 'late', home: side(1, { left: null, to_top: 0 }), away: side(4, { left: null }) } }))
    const sec = screen.getByRole('region', { name: /League table before the match/ })
    expect(sec).toHaveTextContent('Late season')
    expect(sec).toHaveTextContent('Play-off format: no fixed season end, so no games-left count.')
  })

  it('has no context row when the fixture has none or it is malformed', async () => {
    await mount('#/match/fx002', withContext({ fx002: { phase: 'middle', home: { pos: 'x' } } }))
    expect(screen.queryByRole('region', { name: /League table before the match/ })).toBeNull()
  })
})

// ---------------------------------------------------------------------------- strong candidates, stacking, phase outcomes

const cell = (n: number, over: number, stated: number, held = 7): StackCell => ({ n, over_rate: over, stated_rate: stated, excess_ci: [0.01, 0.05], seasons_held: held, seasons: 7 })
const STACKING: Stacking = {
  min_boosts: 2, note: 'Matches by number of boost signals.', groups: ['0', '1', '2', '3+', '2+'], phases: ['early', 'middle', 'late', 'run_in'],
  routes: {
    odds: { signals: ['h2h_above'], share: { '0': 0.67, '1': 0.27, '2': 0.06, '3+': 0.01, '2+': 0.07 },
      by_count: { '0': cell(33081, 0.2067, 0.2139, 1), '1': cell(13294, 0.2236, 0.2095), '2': cell(2769, 0.2481, 0.211), '3+': { n: 0 }, '2+': cell(3268, 0.2512, 0.2115) },
      season_phase: {}, strong_applies: true },
    noodds: { signals: ['h2h_above'], share: { '0': 0.6, '1': 0.31, '2': 0.07, '3+': 0.02, '2+': 0.09 },
      by_count: { '0': cell(30000, 0.2, 0.21, 2), '1': cell(15000, 0.22, 0.21), '2': cell(3500, 0.24, 0.213), '3+': cell(800, 0.25, 0.214), '2+': cell(4384, 0.2409, 0.213) },
      season_phase: {}, strong_applies: false },
  },
}
const withStacking = (base: Dataset = dataset): Dataset => ({
  ...base,
  surge: { ...surge, stacking: STACKING },
  board: { ...base.board, fixtures: base.board.fixtures.map((f) => (f.id === 'fx001' ? { ...f, surge: { ...f.surge!, strong: true } } : f)) },
})

describe('strong surge candidates and stacking', () => {
  it('reads the strong flag only when the export set it', () => {
    expect(isStrong({ signals: ['a', 'b'], against: [], strong: true })).toBe(true)
    expect(isStrong({ signals: ['a', 'b'], against: [] })).toBe(false)
    expect(isStrong(null)).toBe(false)
    const cs = surgeCandidates(buildItems(withStacking()), catalogOf(surge), 24, START)
    expect(cs.map((c) => [c.item.id, c.strong])).toEqual([['fx001', true], ['fx005', false]])
    expect(stackRows(STACKING).map((r) => r.group)).toEqual(['0', '1', '2', '3+', '2+'])
  })

  it('marks strong candidates on the Surge page and explains the rule with the stacking numbers', async () => {
    await mount('#/surge', withStacking())
    expect(within(rows()[0]).getByText('Strong candidate')).toBeInTheDocument()
    expect(within(rows()[1]).queryByText('Strong candidate')).toBeNull()
    const sec = screen.getByRole('region', { name: 'Several signals at once' })
    expect(sec).toHaveTextContent('With odds: 2+ signals surged 25% vs 21% our curve, held 7 of 7 seasons; confirmed on later seasons')
    expect(sec).toHaveTextContent('Without odds: 2+ signals surged 24% vs 21% our curve, held 7 of 7 seasons, but not confirmed on later seasons, so no match is marked strong here.')
    const table = within(sec).getByRole('region', { name: 'Surges by number of signals' })
    const r2 = within(table).getByRole('row', { name: /^2\+ signals/ })
    expect(within(r2).getAllByRole('cell').map((c) => c.textContent)).toEqual(['24% vs 21% (4,384, held 7/7)', '25% vs 21% (3,268, held 7/7)']) // without odds first, as on the cards
    expect(within(table).getByRole('row', { name: /^3\+ signals/ })).toHaveTextContent('none')
    expect(within(table).getByRole('row', { name: /^No signal/ })).toBeInTheDocument()
  })

  it('has no stacking section when surge.json has none', async () => {
    await mount('#/surge')
    expect(screen.queryByRole('region', { name: 'Several signals at once' })).toBeNull()
  })

  it('shows the strong badge in the match panel', async () => {
    await mount('#/match/fx001', withStacking())
    const sec = screen.getByRole('region', { name: /Surge signals/ })
    expect(within(sec).getByText('Strong candidate')).toBeInTheDocument()
    expect(sec).toHaveTextContent('Two or more signals at once')
  })

  it('has no strong badge in the panel of an ordinary candidate', async () => {
    await mount('#/match/fx005', withStacking())
    expect(within(screen.getByRole('region', { name: /Surge signals/ })).queryByText('Strong candidate')).toBeNull()
  })
})

describe('surge rate by season phase', () => {
  const phased = (k: number) => labelled(k, k).map((r, i) => ({ ...r, context: { ...CTX.fx005, phase: (['early', 'middle', 'late', 'run_in'] as const)[i % 4] } }))

  it('counts settled matches by the recorded phase', () => {
    const rows_ = phaseOutcomes(settledAssessed(phased(6), 5))
    expect(rows_.map((r) => r.phase)).toEqual(['early', 'middle', 'late', 'run_in'])
    expect(rows_.reduce((a, r) => a + r.flagged.n + r.other.n, 0)).toBe(12)
    expect(phaseOutcomes(settledAssessed(labelled(3, 3), 5)).every((r) => r.flagged.n + r.other.n === 0)).toBe(true)
  })

  it('shows a short note while thin and a table once there are enough', async () => {
    await mount('#/surge', withResults(labelled(4, 3)))
    expect(outcomes()).toHaveTextContent('Too few settled matches with a recorded season phase yet (0 so far')
    expect(within(outcomes()).queryByRole('region', { name: 'Settled matches by season phase' })).toBeNull()
  })

  it('tabulates flagged and unflagged surge rates per phase', async () => {
    await mount('#/surge', withResults(phased(8)))
    const table = within(outcomes()).getByRole('region', { name: 'Settled matches by season phase' })
    expect(within(table).getAllByRole('row')).toHaveLength(5)
    expect(within(table).getByRole('row', { name: /^Run-in/ })).toHaveTextContent(/of \d+/)
  })
})
