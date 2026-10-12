import { memo } from 'react'
import { isHit, pct, probAt } from '../lib/curve'
import { dayShort, dayNum, monthShort } from '../lib/format'
import type { Item } from '../lib/types'
import { Badge, ProbPill } from './ui'

interface Props {
  item: Item
  n: number
  totalStart: number
  href: string
  selected: boolean
  starred: boolean
  homeFav: boolean
  awayFav: boolean
  showDate: boolean
  onStar: (id: string, label: string) => void
}

function MatchRowImpl({ item, n, totalStart, href, selected, starred, homeFav, awayFav, showDate, onStar }: Props) {
  const p = probAt(item.curve, totalStart, n)
  const label = `${item.home} v ${item.away}`
  const hit = item.actual ? isHit(item.actual.total, n) : null
  const time = item.finished ? 'FT' : (item.time ?? '')
  const aria =
    `${label}, ${item.finished ? 'finished' : `kick-off ${item.time}`}, ${pct(p)} chance of ${n}+ shots` +
    (item.actual ? `, actual ${item.actual.total} shots, ${hit ? 'hit' : 'miss'}` : '')
  return (
    <li className={`mrow${selected ? ' sel' : ''}${item.finished ? ' done' : ''}`}>
      <a className="mlink" href={href} data-id={item.id} aria-current={selected ? 'true' : undefined} aria-label={aria}>
        <span className="mtime">
          {showDate && <span className="mdate">{dayShort(item.date)} {dayNum(item.date)} {monthShort(item.date)}</span>}
          <span>{time}</span>
        </span>
        <span className="mteams">
          <span className="mteam"><Badge name={item.home} />{homeFav && <span className="fv" aria-hidden="true">&#9733;</span>}<span className="tn">{item.home}</span></span>
          <span className="mteam"><Badge name={item.away} />{awayFav && <span className="fv" aria-hidden="true">&#9733;</span>}<span className="tn">{item.away}</span></span>
        </span>
        <span className="mexp" aria-hidden="true">
          {item.teamExpected ? `${Math.round(item.teamExpected[0])} – ${Math.round(item.teamExpected[1])}` : ''}
        </span>
        <span className="mprob" aria-hidden="true">
          <ProbPill p={p} />
          {item.actual && (
            <span className={`res ${hit ? 'hit' : 'miss'}`}>
              {item.actual.total} shots {hit ? '✓ hit' : '✗ miss'}
            </span>
          )}
          {!item.actual && !item.odds && <span className="nodds">no odds</span>}
        </span>
      </a>
      <button
        type="button"
        className="star"
        aria-pressed={starred}
        aria-label={`Follow ${label}`}
        onClick={() => onStar(item.id, label)}
      >
        <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
          <path d="M10 1.8l2.4 5.2 5.6.6-4.2 3.8 1.2 5.6L10 14.2 4.9 17l1.2-5.6L2 7.6l5.6-.6z" />
        </svg>
      </button>
    </li>
  )
}
export const MatchRow = memo(MatchRowImpl)
