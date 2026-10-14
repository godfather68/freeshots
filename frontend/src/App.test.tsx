import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import App from './App'
import { board, dataset, meta, results } from './test-fixtures'
import { UNDER_CAVEAT } from './lib/config'

const START = meta.thresholds.total[0]
const todays = board.fixtures.filter((f) => f.date === '2026-10-12')
const first = todays[0]

async function mount(hash = '#/', load = () => Promise.resolve(dataset)) {
  window.location.hash = hash
  render(<App zone="Africa/Brazzaville" today="2026-10-12" now={Date.parse('2026-10-12T10:00:00Z')} load={load} />)
  await screen.findByRole('heading', { name: /Upcoming football matches/ }, { timeout: 3000 }).catch(() => undefined)
}
const rowFor = (home: string) => screen.getAllByRole('link').find((a) => a.getAttribute('aria-label')?.startsWith(`${home} v `))!
const hash = () => window.location.hash

describe('match list', () => {
  it('renders today with league groups, counts and rows', async () => {
    await mount()
    expect(screen.getByRole('heading', { name: /Premier League/ })).toBeInTheDocument()
    const rows = document.querySelectorAll('a.mlink')
    expect(rows).toHaveLength(todays.length)
    expect(document.querySelector('p[role=status]')).toHaveTextContent(`${todays.length} matches in`)
  })

  it('shows the model probability at N, exactly as published', async () => {
    await mount()
    const expected = Math.round(first.curve[24 - START] * 100)
    const label = rowFor(first.home).getAttribute('aria-label')!
    expect(label).toContain(`${expected}% chance of 24+ shots`)
    expect(label).toContain(`kick-off ${first.kickoff_local}`)
  })

  it('moving the threshold re-reads the curve and writes N to the URL', async () => {
    await mount()
    await userEvent.click(screen.getByRole('button', { name: 'Increase Shots' }))
    expect(hash()).toContain('n=25')
    const expected = Math.round(first.curve[25 - START] * 100)
    await waitFor(() => expect(rowFor(first.home).getAttribute('aria-label')).toContain(`${expected}% chance of 25+ shots`))
  })

  it('70%+ chip keeps only matches that clear the bar and announces the count', async () => {
    await mount()
    await userEvent.click(screen.getByRole('button', { name: /^70%\+/ }))
    const want = todays.filter((f) => f.curve[24 - START] >= 0.7).length
    expect(document.querySelectorAll('a.mlink')).toHaveLength(want)
    expect(hash()).toContain('chip=p70')
  })

  it('empty state offers to clear filters', async () => {
    await mount('#/?p=95&n=45')
    expect(await screen.findByText('Nothing clears these filters')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    await waitFor(() => expect(document.querySelectorAll('a.mlink').length).toBeGreaterThan(0))
    expect(hash()).not.toContain('p=95')
  })

  it('a day with no matches says so and points to the next day', async () => {
    await mount('#/?date=2026-10-14')
    expect(await screen.findByText(/No matches on Wed 14 Oct/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Go to Sat 17 Oct/ })).toBeInTheDocument()
  })

  it('bad query params never crash: values are clamped', async () => {
    await mount('#/?n=9999&p=-3&sort=zzz&chip=%00&tab=nope&m=ghost')
    expect(screen.getByLabelText(/Shots/, { selector: 'input' })).toHaveValue('45')
    expect(screen.getByRole('heading', { name: 'Pick a match' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/not in the current board|may have finished/i)
  })

  it('stars persist in localStorage and fill the Favourites tab', async () => {
    await mount()
    await userEvent.click(within(rowFor(first.home).closest('li')!).getByRole('button', { name: /Follow/ }))
    expect(JSON.parse(localStorage.getItem('shots.fav')!).matches).toEqual([first.id])
    await userEvent.click(screen.getByRole('tab', { name: 'Favourites' }))
    expect(document.querySelectorAll('a.mlink')).toHaveLength(1)
  })

  it('shows finished matches with actual shots and a hit/miss mark for the current N', async () => {
    const r = results.results[0]
    await mount(`#/?date=${r.date}&chip=done`)
    const row = document.querySelector(`a.mlink[data-id="${r.id}"]`)!
    const hit = r.actual.total >= 24
    expect(row.textContent).toContain(`${r.actual.total} shots`)
    expect(row.textContent).toContain(hit ? 'hit' : 'miss')
    expect(row.getAttribute('aria-label')).toContain(hit ? ', hit' : ', miss')
  })
})

describe('detail panel', () => {
  it('opens beside the list from the URL with the headline, floors and a text alternative', async () => {
    await mount(`#/?m=${first.id}`)
    const panel = screen.getByRole('complementary', { name: 'Match detail' })
    expect(within(panel).getByRole('heading', { level: 2, name: new RegExp(first.home) })).toBeInTheDocument()
    const p = Math.round(first.curve[24 - START] * 100)
    expect(within(panel).getByTestId('headline')).toHaveTextContent(`${p}%chance of 24+ shots in total`)
    expect(within(panel).getByText(`at least ${first.lb['80']}`)).toBeInTheDocument()
    expect(within(panel).getByRole('img', { name: /Chance of at least N total shots/ })).toBeInTheDocument()
    await userEvent.click(within(panel).getByText('Show the chart as a table'))
    expect(within(panel).getByRole('table')).toBeInTheDocument()
  })

  it('clicking a row selects it and the list stays', async () => {
    await mount()
    await userEvent.click(rowFor(first.home))
    await waitFor(() => expect(hash()).toContain(`m=${first.id}`))
    expect(await screen.findByRole('heading', { level: 2, name: new RegExp(first.away) })).toBeInTheDocument()
    expect(document.querySelectorAll('a.mlink').length).toBe(todays.length)
    expect(rowFor(first.home)).toHaveAttribute('aria-current', 'true')
  })

  it('Teams tab labels team numbers experimental, About tab states odds and model', async () => {
    const withTeam = board.fixtures.find((f) => f.team && !f.odds)!
    await mount(`#/?date=${withTeam.date}&m=${withTeam.id}&pt=teams`)
    expect(screen.getAllByText('experimental').length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('tab', { name: 'About this match' }))
    expect(screen.getByText(/No odds yet/)).toBeInTheDocument()
    expect(screen.getByText(withTeam.model)).toBeInTheDocument()
  })

  it('flags a cold start', async () => {
    const cold = board.fixtures.find((f) => f.cold_start)!
    await mount(`#/?date=${cold.date}&m=${cold.id}`)
    expect(screen.getAllByText('cold start').length).toBeGreaterThan(0)
  })

  it('a match without team calibration says so', async () => {
    const none = board.fixtures.find((f) => f.team === null)!
    await mount(`#/?date=${none.date}&m=${none.id}&pt=teams`)
    expect(screen.getByText(/without team calibration/)).toBeInTheDocument()
  })

  it('finished match shows the outcome against the chosen N', async () => {
    const r = results.results[0]
    await mount(`#/?date=${r.date}&m=${r.id}&n=20`)
    expect(screen.getByRole('heading', { name: 'Final shots' })).toBeInTheDocument()
    expect(screen.getByText(r.actual.total >= 20 ? /Hit/ : /Miss/)).toBeInTheDocument()
  })

  it('unknown match id on the full page is a friendly not-found, not a crash', async () => {
    await mount('#/match/does-not-exist')
    expect(await screen.findByRole('heading', { name: 'Match not found' })).toBeInTheDocument()
  })
})

describe('pages and states', () => {
  it('track record page: backfill numbers, then an honest empty live state', async () => {
    await mount('#/track')
    expect(await screen.findByRole('heading', { level: 1, name: 'Track record' })).toBeInTheDocument()
    expect(screen.getByText('Hit rate at 70%+ calls')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: /Reliability/ })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('tab', { name: 'Live picks' }))
    expect(screen.getByText('0 settled so far')).toBeInTheDocument()
  })

  it('how it works page lists the glossary', async () => {
    await mount('#/how')
    expect(await screen.findByRole('heading', { level: 1, name: 'How it works' })).toBeInTheDocument()
    expect(screen.getByText('Cold start', { selector: 'dt' })).toBeInTheDocument()
  })

  it('missing data files give a friendly error with a retry', async () => {
    let calls = 0
    const load = () => (++calls === 1 ? Promise.reject(new Error('data/board.json is not published yet (HTTP 404).')) : Promise.resolve(dataset))
    window.location.hash = '#/'
    render(<App zone="Africa/Brazzaville" today="2026-10-12" load={load} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('not published yet')
    await act(async () => { await userEvent.click(screen.getByRole('button', { name: 'Try again' })) })
    expect(await screen.findByRole('heading', { name: /Upcoming football matches/ })).toBeInTheDocument()
  })

  it('warns when the data is stale or the last job failed', async () => {
    window.location.hash = '#/'
    const failed = { ...dataset, meta: { ...meta, last_job: { status: 'failed', finished_at: null } } }
    render(<App zone="Africa/Brazzaville" today="2026-10-12" now={Date.parse(meta.built_at) + 3600_000} load={() => Promise.resolve(failed)} />)
    expect(await screen.findByText(/last update did not finish/i)).toBeInTheDocument()
  })

  it('guide can be dismissed and the choice is remembered', async () => {
    await mount()
    await userEvent.click(screen.getByRole('button', { name: 'Hide guide' }))
    expect(screen.queryByRole('heading', { name: /New here/ })).toBeNull()
    expect(JSON.parse(localStorage.getItem('shots.guide')!).dismissed).toBe(true)
  })
})

describe('Champions League and experimental fixtures', () => {
  const cl = board.fixtures.filter((f) => f.league === 'uefa-champions-league')
  const day = '2026-10-13'

  it('pins the featured league first and tags experimental / limited-data rows', async () => {
    await mount(`#/?date=${day}`)
    const heads = [...document.querySelectorAll('.ghead')]
    expect(heads[0].textContent).toContain('Champions League')
    expect(heads[0].textContent).toContain('Featured')
    expect(document.querySelectorAll('.mlink .tag.xp')).toHaveLength(cl.length)
    expect(document.querySelectorAll('.mlink .tag.ltd')).toHaveLength(cl.filter((f) => f.data_quality !== 'full').length)
    const opts = [...document.querySelectorAll('optgroup')].map((g) => g.getAttribute('label'))
    expect(opts).toEqual(['Featured', 'All leagues'])
  })

  it('panel shows the experimental notice and the limited-data line', async () => {
    const partial = cl.find((f) => f.data_quality === 'partial')!
    await mount(`#/?date=${day}&m=${partial.id}`)
    expect(screen.getByText(/Built from each club.s domestic form\. Not yet validated on Champions League matches\./)).toBeInTheDocument()
    expect(document.querySelector('.notice.ltd')).toHaveTextContent('One or both clubs have no domestic data in our sources.')
  })

  it('a full-quality experimental fixture has the notice but no limited-data line', async () => {
    const full = cl.find((f) => f.data_quality === 'full')!
    await mount(`#/?date=${day}&m=${full.id}`)
    expect(screen.getByText(/Not yet validated on/)).toBeInTheDocument()
    expect(document.querySelector('.notice.ltd')).toBeNull()
  })

  it('Top picks leave experimental fixtures out when the day has others', async () => {
    await mount(`#/?date=${day}`)
    const picks = document.querySelector('.picks')!
    expect(picks.textContent).not.toMatch(/Bayern|Benfica|PSV|Red Star/)
    expect(screen.queryByText(/Only experimental predictions/)).toBeNull()
  })

  it('Top picks show experimental fixtures, labelled, when the day has only those', async () => {
    const onlyCl = { ...dataset, board: { ...board, fixtures: cl }, results: null }
    window.location.hash = `#/?date=${day}`
    render(<App zone="Africa/Brazzaville" today={day} now={Date.parse('2026-10-13T10:00:00Z')} load={() => Promise.resolve(onlyCl)} />)
    expect(await screen.findByText(/Only experimental predictions/)).toBeInTheDocument()
    expect(document.querySelectorAll('.picks li').length).toBeGreaterThan(0)
  })

  it('works when the additive fields are absent', async () => {
    const stripped = {
      ...dataset,
      meta: { ...meta, leagues: meta.leagues.map((l) => ({ ...l, featured: undefined })) },
      board: { ...board, fixtures: board.fixtures.map((f) => ({ ...f, experimental: undefined, data_quality: undefined })) },
    }
    window.location.hash = `#/?date=${day}`
    render(<App zone="Africa/Brazzaville" today={day} now={Date.parse('2026-10-13T10:00:00Z')} load={() => Promise.resolve(stripped)} />)
    expect(await screen.findByRole('heading', { name: /Upcoming football matches/ })).toBeInTheDocument()
    expect(document.querySelectorAll('.tag.xp, .tag.ltd')).toHaveLength(0)
  })

  it('hero strip counts matches and the 70% chip count still works', async () => {
    await mount(`#/?date=${day}`)
    const want = board.fixtures.filter((f) => f.date === day && f.curve[24 - START] >= 0.7).length
    expect(document.querySelector('.hero-stats')).toHaveTextContent(`Clear 70% at 24+${want}`)
    await userEvent.click(screen.getByRole('button', { name: /^70%\+/ }))
    expect(document.querySelectorAll('a.mlink')).toHaveLength(want)
  })
})

describe('time zones', () => {
  const f0 = todays[0]
  const late = { ...f0, date: '2026-10-13', kickoff_local: '23:30', kickoff_utc: '2026-10-13T22:30:00Z' }
  const crossing = { ...dataset, board: { ...dataset.board, fixtures: [late, ...dataset.board.fixtures.filter((f) => f.id !== f0.id)] } }
  const mountIn = async (zone: string, h: string) => {
    window.location.hash = h
    render(<App zone={zone} today="2026-10-13" now={Date.parse('2026-10-13T10:00:00Z')} load={() => Promise.resolve(crossing)} />)
    await screen.findByRole('group', { name: /Show kick-off times in/ })
  }

  it('names the active zone with its UTC offset and defaults to My time', async () => {
    await mountIn('Africa/Brazzaville', '#/?date=2026-10-13')
    expect(screen.getByText(/Times in/).textContent).toBe('Times in Africa/Brazzaville (UTC+1)')
    expect(screen.getByRole('button', { name: 'My time' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: 'Venue time' })).toHaveAttribute('aria-pressed', 'false')
  })

  it('a late match moves to the next day in a zone ahead of the venue', async () => {
    await mountIn('Asia/Baku', '#/?date=2026-10-14')
    const row = rowFor(f0.home)
    expect(row.getAttribute('aria-label')).toContain('kick-off 02:30')
    expect(document.querySelector('.strip a[aria-current="date"]')).toHaveTextContent('14')
  })

  it('stays on the same day in a zone behind the venue', async () => {
    await mountIn('America/New_York', '#/?date=2026-10-13')
    expect(rowFor(f0.home).getAttribute('aria-label')).toContain('kick-off 18:30')
  })

  it('the toggle switches to venue times, persists, and shows both clocks in the panel', async () => {
    await mountIn('Asia/Baku', '#/?date=2026-10-14')
    await userEvent.click(screen.getByRole('button', { name: 'Venue time' }))
    expect(JSON.parse(localStorage.getItem('shots.timemode')!)).toBe('venue')
    expect(screen.getByText(/Times at each venue/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'My time' }))
    expect(JSON.parse(localStorage.getItem('shots.timemode')!)).toBe('mine')
    await userEvent.click(rowFor(f0.home))
    await waitFor(() => expect(document.querySelector('.ko')).toHaveTextContent(/Wed 14 Oct, 02:30 your time · Tue 13 Oct 23:30 at the venue/))
  })

  it('shows the build time in the visitor zone', async () => {
    await mountIn('Asia/Baku', '#/')
    expect(document.querySelector('.fresh')).toHaveTextContent(/Predictions built \d+ \w{3} \d\d:\d\d UTC\+4/)
  })
})

describe('odds check', () => {
  const withTeams = board.fixtures.find((f) => f.id === 'fx001')!
  const noTeams = board.fixtures.find((f) => f.team === null)!
  const open = (f: { id: string; date: string }, extra = '') => mount(`#/?date=${f.date}&m=${f.id}&pt=edge${extra}`)
  const res = () => document.querySelector('.edgeres')!
  const over = (curve: number[], start: number, n: number) => curve[n - start]

  it('is a tab, shows the disclaimer, and asks for odds first', async () => {
    await open(withTeams)
    expect(screen.getByRole('tab', { name: 'Odds check' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText(/Estimates only, not advice/)).toBeInTheDocument()
    expect(res()).toHaveTextContent('Enter the bookmaker')
  })

  it('computes from the published curve: Over 24.5 at 2.00', async () => {
    await open(withTeams, '&ln=24.5&od=2')
    const p = over(withTeams.curve, 10, 25)
    expect(res()).toHaveTextContent(`${(p * 100).toFixed(1)}%`)
    expect(res()).toHaveTextContent(`Fair odds${(1 / p).toFixed(2)}`)
    const ev = p - (1 - p)
    expect(res()).toHaveTextContent(`${ev >= 0 ? '+' : '\u2212'}${Math.abs(ev).toFixed(2)}`)
  })

  it('under is the complement and shows the config caveat', async () => {
    await open(withTeams, '&ln=24.5&sd=under&od=1.9')
    const p = 1 - over(withTeams.curve, 10, 25)
    expect(res()).toHaveTextContent(`${(p * 100).toFixed(1)}%`)
    expect(screen.getByText(UNDER_CAVEAT as string)).toBeInTheDocument()
  })

  it('accepts fractional and American odds and explains bad input without crashing', async () => {
    await open(withTeams, '&od=5/6')
    expect(res()).toHaveTextContent('Price implies54.5%')
    const input = screen.getByLabelText(/Bookmaker odds for/)
    await userEvent.clear(input)
    await userEvent.type(input, '-120')
    expect(hash()).toContain('od=-120')
    await userEvent.clear(input)
    await userEvent.type(input, '0.5')
    expect(await screen.findByRole('alert')).toHaveTextContent('above 1.00')
    expect(input).toHaveAttribute('aria-invalid', 'true')
  })

  it('says so for a line outside the published range', async () => {
    await open(withTeams, '&ln=60.5&od=2')
    expect(res()).toHaveTextContent('outside it, and we do not extrapolate')
  })

  it('offers team markets only with team curves, labelled experimental', async () => {
    await open(withTeams)
    expect(screen.getAllByRole('button', { name: /shots \(experimental\)/ })[0]).toBeEnabled()
    expect(document.querySelector('.edge .notice.xp')).toBeNull()
    await userEvent.click(screen.getAllByRole('button', { name: /\(experimental\)/ })[0])
    expect(hash()).toContain('mk=home')
    expect(document.querySelector('.edge .notice.xp')).toHaveTextContent('Team shots are experimental')
  })

  it('disables team markets without team curves and flags no-odds fixtures', async () => {
    await open(noTeams)
    expect(screen.getAllByRole('button', { name: /\(experimental\)/ })[0]).toBeDisabled()
    if (!noTeams.odds) expect(screen.getByText(/did not see bookmaker odds/)).toBeInTheDocument()
  })

  it('saves a check, remembers the odds format, and lists it with CSV', async () => {
    await open(withTeams, '&ln=24.5&od=-120')
    await userEvent.click(screen.getByRole('button', { name: 'American' }))
    expect(JSON.parse(localStorage.getItem('shots.oddsformat')!)).toBe('american')
    await userEvent.click(screen.getByRole('button', { name: 'Save to My checks' }))
    expect(JSON.parse(localStorage.getItem('shots.checks')!)).toHaveLength(1)
    expect(await within(res() as HTMLElement).findByRole('status')).toHaveTextContent('Kept only in this browser')
    act(() => { window.location.hash = '#/checks' })
    expect(await screen.findByRole('heading', { name: 'My checks' })).toBeInTheDocument()
    expect(screen.getByText('Waiting for the result')).toBeInTheDocument()
    expect((screen.getByLabelText(/Spreadsheet text/) as HTMLTextAreaElement).value).toContain('over')
  }, 20000)

  it('settles a saved check once the match is in the results', async () => {
    const r = results.results[0]
    localStorage.setItem('shots.checks', JSON.stringify([{
      id: 'z', savedAt: '2026-10-10T08:00:00Z', fixtureId: r.id, home: r.home, away: r.away, league: 'X', date: r.date,
      market: 'total', side: 'over', line: r.actual.total - 0.5, oddsText: '2', decimal: 2, model: 0.6,
    }]))
    const { checksStore } = await import('./lib/storage')
    checksStore.reload()
    await mount('#/checks')
    expect(await screen.findByText('✓ Hit')).toBeInTheDocument()
  })

  it('shows an empty state on My checks', async () => {
    await mount('#/checks')
    expect(await screen.findByText(/No saved checks yet/)).toBeInTheDocument()
  })
})

describe('why this number', () => {
  const full = board.fixtures.find((f) => f.id === 'fx003')!
  const partial = board.fixtures.find((f) => f.id === 'fx004')!
  const plain = board.fixtures.find((f) => f.id === 'fx002')!

  it('renders baseline, signed factors, closing line and signals', async () => {
    await mount(`#/?date=${full.date}&m=${full.id}`)
    const why = document.querySelector('.why')!
    expect(why).toHaveTextContent('Start: 27.8 shots')
    expect(why).toHaveTextContent('Premier League average this season')
    expect(why).toHaveTextContent('+2.9')
    expect(why).toHaveTextContent('\u22121.2')
    expect(why.querySelectorAll('.wf-row.down')).toHaveLength(1)
    expect(why).toHaveTextContent('= about 33 shots expected')
    expect(why).toHaveTextContent('How solid is this?')
    expect(why).toHaveTextContent(/form built on 4 matches, very few/)
    expect(why).toHaveTextContent('No bookmaker odds were used')
    expect(why).toHaveTextContent('swing a lot')
    expect(within(why as HTMLElement).getByRole('table', { hidden: true })).toBeInTheDocument()
  })

  it('copes with a partial block (no baseline, no signals)', async () => {
    await mount(`#/?date=${partial.date}&m=${partial.id}`)
    const why = document.querySelector('.why')!
    expect(why).toHaveTextContent('Match tempo')
    expect(why).toHaveTextContent('\u22120.8')
    expect(why).not.toHaveTextContent('Start:')
    expect(why).not.toHaveTextContent('How solid')
  })

  it('is hidden when the fixture has no explain', async () => {
    await mount(`#/?date=${plain.date}&m=${plain.id}`)
    expect(document.querySelector('.why')).toBeNull()
    expect(screen.getByTestId('headline')).toBeInTheDocument()
  })
})
