import { useEffect, useRef } from 'react'
import { useApp, useMedia } from '../lib/app'
import { ListColumn } from '../components/ListColumn'
import { Panel } from '../components/Panel'
import { Guide, TopPicks, TrackSummary } from '../components/Rail'

export function BoardPage() {
  const { state, itemById, set } = useApp()
  const sheet = useMedia('(max-width: 699px)')
  const item = state.m ? (itemById.get(state.m) ?? null) : null
  const headRef = useRef<HTMLHeadingElement>(null)
  const prev = useRef<{ m: string | null; first: boolean }>({ m: null, first: true })

  // Focus follows the panel: into its heading when a match opens, back to the row it came from when it closes.
  // Not on first render, so a shared link does not steal focus.
  useEffect(() => {
    const was = prev.current
    prev.current = { m: item?.id ?? null, first: false }
    if (was.first || was.m === (item?.id ?? null)) return
    if (item) headRef.current?.focus({ preventScroll: !sheet })
    else if (was.m) {
      const row = document.querySelector<HTMLElement>(`a.mlink[data-id="${CSS.escape(was.m)}"]`)
      ;(row ?? document.getElementById('list-h'))?.focus()
    }
  }, [item, sheet])

  const close = () => set({ m: null }, { replace: false })
  useEffect(() => {
    if (!item) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.defaultPrevented) return
      const t = e.target as HTMLElement
      if (t.closest?.('details[open]')) return
      set({ m: null })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [item, set])

  const open = !!item
  return (
    <div className={`board${open ? ' has-panel' : ''}`}>
      <h1 className="sr">Upcoming football matches: chance of reaching a total-shots line</h1>
      <div className="g-list" inert={open && sheet}><ListColumn /></div>
      <aside className={`g-panel${open ? ' open' : ''}`} aria-label="Match detail" data-open={open}>
        {item ? (
          <Panel ref={headRef} item={item} onClose={close} />
        ) : (
          <div className="card emptypanel">
            <h2>Pick a match</h2>
            {state.m && <p role="alert">That match is not in the current board. It may have finished or been rescheduled.</p>}
            <p>Select a match on the left to see the chance of reaching your shot threshold, how sure the model is, and each team&rsquo;s share.</p>
          </div>
        )}
      </aside>
      <div className="g-guide" inert={open && sheet}><Guide /></div>
      <div className="g-rail" inert={open && sheet}>
        <TopPicks />
        <TrackSummary />
      </div>
    </div>
  )
}
