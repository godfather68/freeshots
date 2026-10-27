// Loading the published JSON. Only data/*.json relative to the page is ever fetched.
import type { Board, Dataset, Meta, Results, Surge, Track } from './types'

export class DataError extends Error {
  file: string
  constructor(file: string, message: string) {
    super(message)
    this.file = file
  }
}

export const dataUrl = (file: string, base: string = import.meta.env.BASE_URL): string => `${base}data/${file}`

async function getJson<T>(file: string, fetcher: typeof fetch): Promise<T> {
  let res: Response
  try {
    res = await fetcher(dataUrl(file), { cache: 'no-cache' })
  } catch {
    throw new DataError(file, `Could not reach data/${file}. Check your connection and try again.`)
  }
  if (!res.ok) throw new DataError(file, `data/${file} is not published yet (HTTP ${res.status}).`)
  try {
    const json = (await res.json()) as T & { schema?: number }
    if (json.schema !== 1) throw new Error('schema')
    return json
  } catch {
    throw new DataError(file, `data/${file} is not valid shots data (a dev server may be returning a web page instead).`)
  }
}

/** surge.json is optional: anything that is not a catalogue (no signal list) is treated as missing. */
function validSurge(s: Surge): Surge | null {
  return Array.isArray(s.signals) && s.routes && typeof s.margin === 'number' ? s : null
}

/** meta + board are required; results, track and surge degrade to null (the UI then hides those parts). */
export async function loadDataset(fetcher: typeof fetch = fetch): Promise<Dataset> {
  const [meta, board] = await Promise.all([getJson<Meta>('meta.json', fetcher), getJson<Board>('board.json', fetcher)])
  const [results, track, surge] = await Promise.all([
    getJson<Results>('results.json', fetcher).catch(() => null),
    getJson<Track>('track.json', fetcher).catch(() => null),
    getJson<Surge>('surge.json', fetcher).then(validSurge).catch(() => null),
  ])
  return { meta, board, results, track, surge }
}
