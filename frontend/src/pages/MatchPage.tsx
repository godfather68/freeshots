import { useApp } from '../lib/app'
import { Panel } from '../components/Panel'

export function MatchPage({ id }: { id: string }) {
  const { itemById, href } = useApp()
  const item = itemById.get(id)
  const back = href({ m: id }, '/')
  if (!item) {
    return (
      <div className="page narrow">
        <div className="card empty">
          <h1>Match not found</h1>
          <p>This match is not in the current board. It may have finished, been rescheduled, or the link may be old.</p>
          <p><a href={href({ m: null }, '/')}>Back to the board</a></p>
        </div>
      </div>
    )
  }
  return (
    <div className="page narrow">
      <h1 className="sr">Shots prediction: {item.home} v {item.away}</h1>
      <p><a href={back}>&lsaquo; Back to the board</a></p>
      <div className="card fullpanel"><Panel item={item} full /></div>
    </div>
  )
}
