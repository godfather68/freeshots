# Shots board (static frontend)

Client-side React + TypeScript app (Vite). It reads the JSON contract in `docs/static-api.md` from
`data/*.json` (`meta`, `board`, `results`, `track`) next to the page and does nothing else over the
network. No model runs in the browser; probabilities are the published calibrated curve values.

## Commands (run in `frontend/`)

| Command | What it does |
| --- | --- |
| `npm install` | install dependencies |
| `npm run dev` | dev server on http://127.0.0.1:5173 (needs `public/data/*.json`, otherwise shows the friendly "not published yet" state) |
| `npm run build` | type-check and build to `dist/` (relative base, works at any URL path) |
| `npm run preview` | serve `dist/` on http://127.0.0.1:4173 |
| `npm run lint` | ESLint (with jsx-a11y) and `tsc` |
| `npm run test` | Vitest: data layer (curve lookup, filters, sorting, hit/miss, URL state, loading) and component tests |
| `npm run test:e2e` | builds, then Playwright + axe-core (WCAG 2.2 AA) audits on the built site |

Playwright needs a Chromium build: `npx playwright install chromium`, or point `CHROMIUM_PATH` at an
existing Chromium/headless-shell executable. The e2e tests answer `data/*.json` from
`src/test-fixtures/` (so they do not depend on the real data) and use the vendored axe-core at
`tests/a11y/vendor/axe.min.js`. Screenshots land in `test-results/` (git-ignored).

## Publishing

`public/data/` is where the exporter (`python -m shots.export --out frontend/public/data`) writes the
JSON; Vite copies it into `dist/data/`. For GitHub Pages deploy `dist/`; nothing else is needed.
Set `VITE_BASE=/repo/` only if you want absolute asset URLs; the default `./` already works on
project sites.

## Routing

Hash routing (`#/`, `#/match/<id>`, `#/track`, `#/how`) instead of a `404.html` redirect trick:
GitHub Pages serves `index.html` only at the site root, a 404 fallback would return a 404 status
(bad for caching and monitors) and needs the repo name hard-coded, whereas hash routes need no server
support, work from `file://`, on any subpath or custom domain, and survive reloads. View state lives in
the hash query: `date`, `n` (shot threshold), `tn` (team threshold), `p` (minimum %), `lg`, `sort`,
`chip`, `tab`, `m` (selected match), `pt` (panel tab), `rec` (track record view). Bad values are
clamped or ignored.

## Layout

- `src/lib/` pure logic: `curve` (lookup, labels), `items` (merge, filter, sort, group), `url`, `data`,
  `format`, `track`, `storage` (localStorage, try/catch), `app.tsx` (context + router).
- `src/components/` list, row, panel, chart, rail, shared UI. `src/pages/` board, match, track, how.
- `src/test-fixtures/` deterministic data that follows the contract (tests only, never shipped).
