import { useCallback, useEffect, useRef, useState } from 'react'
import { AppProvider, go, useApp, useRoute } from './lib/app'
import { DataError, loadDataset } from './lib/data'
import { freshness, utcStamp } from './lib/format'
import type { Dataset } from './lib/types'
import { guideStore } from './lib/storage'
import { BoardPage } from './pages/BoardPage'
import { HowPage } from './pages/HowPage'
import { MatchPage } from './pages/MatchPage'
import { TrackPage } from './pages/TrackPage'

function PitchMark() {
  return (
    <svg className="pitch-mark" viewBox="0 0 40 26" aria-hidden="true" focusable="false">
      <rect x="1" y="1" width="38" height="24" rx="2" /><line x1="20" y1="1" x2="20" y2="25" />
      <circle cx="20" cy="13" r="5" /><rect x="1" y="7" width="6" height="12" /><rect x="33" y="7" width="6" height="12" />
    </svg>
  )
}

function Freshness({ now }: { now?: number }) {
  const { ds } = useApp()
  const f = freshness(ds.meta, now)
  return (
    <div className={`statusbar ${f.level}`}>
      <p className="fresh wrap">
        <span className="dot" aria-hidden="true" />
        <span>Predictions built {utcStamp(ds.meta.built_at)}</span>
        <span>Results up to {ds.meta.results_up_to ?? 'n/a'}</span>
        {f.level !== 'ok' && (
          <span className="why"><span className="sr">Warning: </span>{f.reasons.join(' ')}</span>
        )}
      </p>
    </div>
  )
}

function Shell({ now }: { now?: number }) {
  const { route, state, ds } = useApp()
  const main = useRef<HTMLElement>(null)
  const prevPath = useRef<string | null>(null)
  const path = route.path
  const here = path === '/' ? 'board' : path.startsWith('/track') ? 'track' : path.startsWith('/how') ? 'how' : path.startsWith('/match') ? 'board' : ''

  useEffect(() => {
    const pageTitle = { board: 'Matches', track: 'Track record', how: 'How it works', '': 'Page not found' }[here]
    document.title = `${pageTitle} · Shots board`
    const was = prevPath.current
    prevPath.current = path
    if (was !== null && was !== path && (path !== '/' || !was.startsWith('/'))) {
      main.current?.focus({ preventScroll: true })
      window.scrollTo(0, 0)
    }
  }, [path, here])

  const openGuide = () => {
    guideStore.set({ dismissed: false })
    if (path !== '/') go('#/')
    setTimeout(() => document.getElementById('guide')?.focus(), 50)
  }
  const m = path.match(/^\/match\/([\w-]{1,64})$/)
  const q = (p: string) => `#${p}${state.n !== ds.meta.thresholds.default_total ? `?n=${state.n}` : ''}`

  return (
    <>
      <a className="skip" href="#main" onClick={(e) => { e.preventDefault(); main.current?.focus() }}>Skip to content</a>
      <header className="top">
        <div className="wrap topbar">
          <a className="brand" href="#/"><PitchMark /><span>Shots board</span></a>
          <nav aria-label="Main">
            <ul>
              <li><a href="#/" aria-current={here === 'board' ? 'page' : undefined}>Matches</a></li>
              <li><a href={q('/track')} aria-current={here === 'track' ? 'page' : undefined}>Track record</a></li>
              <li><a href="#/how" aria-current={here === 'how' ? 'page' : undefined}>How it works</a></li>
            </ul>
          </nav>
          <button type="button" className="quiet" onClick={openGuide}>Quick guide</button>
        </div>
        <Freshness now={now} />
      </header>
      <main id="main" ref={main} tabIndex={-1}>
        <div className="wrap">
          {path === '/' && <BoardPage />}
          {m && <MatchPage id={m[1]} />}
          {path === '/track' && <TrackPage />}
          {path === '/how' && <HowPage />}
          {!(path === '/' || m || path === '/track' || path === '/how') && (
            <div className="page narrow"><div className="card empty"><h1>Page not found</h1><p><a href="#/">Back to the matches</a></p></div></div>
          )}
        </div>
      </main>
      <footer className="foot">
        <div className="wrap">
          <p>Pre-match statistical estimates of total shots (on target, off target and blocked). Not betting advice, and not a promise about any single match.</p>
          <p>Shows only what was published: no model runs in your browser. Team crests are not used; badges are initials.</p>
        </div>
      </footer>
    </>
  )
}

function LoadError({ error, retry }: { error: Error; retry: () => void }) {
  const file = error instanceof DataError ? error.file : null
  return (
    <main id="main" tabIndex={-1} className="wrap">
      <div className="card empty" role="alert">
        <h1>The board is not available yet</h1>
        <p>{error.message}</p>
        <p className="hint">
          {file ? `Missing or unreadable: data/${file}. ` : ''}This site reads files published by the scheduled update. If you run it yourself, build them with the export command and reload.
        </p>
        <button type="button" onClick={retry}>Try again</button>
      </div>
    </main>
  )
}

export default function App({ now, today, load = loadDataset }: { now?: number; today?: string; load?: () => Promise<Dataset> }) {
  const [state, setState] = useState<{ ds: Dataset | null; error: Error | null }>({ ds: null, error: null })
  const [attempt, setAttempt] = useState(0)
  useRoute() // keep the shell subscribed so the title updates even before data arrives
  useEffect(() => {
    let live = true
    load().then(
      (ds) => live && setState({ ds, error: null }),
      (error: Error) => live && setState({ ds: null, error }),
    )
    return () => { live = false }
  }, [load, attempt])
  const retry = useCallback(() => { setState({ ds: null, error: null }); setAttempt((a) => a + 1) }, [])

  if (state.error) return <LoadError error={state.error} retry={retry} />
  if (!state.ds) {
    return (
      <main id="main" className="wrap" aria-busy="true">
        <p className="loading" role="status">Loading the board&hellip;</p>
      </main>
    )
  }
  return (
    <AppProvider ds={state.ds} today={today}>
      <Shell now={now} />
    </AppProvider>
  )
}
