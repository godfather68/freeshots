import { useApp } from '../lib/app'
import { TERMS } from '../lib/glossary'
import { GuideSteps } from '../components/Rail'

export function HowPage() {
  const { state } = useApp()
  return (
    <article className="page prose">
      <h1>How it works</h1>
      <p className="lede">The short version, then the detail.</p>
      <section className="card guide" aria-labelledby="g-short"><h2 id="g-short">Three things to know</h2><GuideSteps n={state.n} /></section>

      <h2>What the percentage means</h2>
      <p>It is the model&rsquo;s estimate of the chance that a match has <em>at least N total shots</em>: both teams together, on and off target, blocked shots included. At 24+ and 78%, roughly 78 of 100 comparable matches should reach 24 shots.</p>
      <p>&ldquo;80% sure: at least 27&rdquo; is the same idea read the other way round: the highest whole number the model is 80% confident the match will reach.</p>

      <h2>Where they come from</h2>
      <p>A statistical model (negative binomial on each team&rsquo;s recent shot rates, home and away, rest days and league level), then recalibrated league by league on recent seasons so the percentages mean what they say. No judgement or guessing goes into a number. This site runs no model: it only shows the board that was last published.</p>

      <h2>Team shots <span className="badge xp">experimental</span></h2>
      <p>Each row also shows the expected shots for each team (&ldquo;17 &ndash; 13&rdquo;, home first). These team numbers are experimental: the team bounds pass the overall calibration checks on 2025/26 but not yet the per-competition check, so we make no per-league confidence claim for them and they deserve more caution than the match totals.</p>

      <h2>European cups and other experimental matches <span className="tag xp">Experimental</span></h2>
      <p>Featured competitions (Champions League, Europa League, Conference League) are pinned to the top of the list. Their numbers are built from each club&rsquo;s domestic form and have not yet been validated on that competition, so they carry an <span className="tag xp">Experimental</span> tag, and they are left out of Top picks unless a day has nothing else.</p>
      <p>A <span className="tag ltd">Limited data</span> tag means one or both clubs have no domestic data in our sources (for example a side from a league we do not cover). The estimate then rests on less, so treat it as a rough guide.</p>

      <h2>Odds and no odds</h2>
      <p>Bookmaker odds sharpen the picture, but most fixtures this far ahead have none published. Those are scored by a separate model trained without odds, and badged <span className="badge mute">no odds</span>. Using the odds model with odds missing would overstate its confidence, so that never happens.</p>

      <h2>Calibration</h2>
      <p>A model is calibrated when its &ldquo;70%&rdquo; calls come true about 70% of the time. The <a href="#/track">track record</a> checks this on matches the model had not seen. Bounds are whole numbers, so reality tends to land slightly on the safe side of the stated figure.</p>

      <h2>Be careful with small samples</h2>
      <ul>
        <li><span className="badge warn">cold start</span> means a team has fewer than 5 earlier matches in our data (promoted sides, new competitions). Its numbers are less reliable.</li>
        <li>A handful of matches proves nothing in either direction. Judge the model over hundreds of matches, not a weekend.</li>
        <li>Lineups, injuries and red cards are not in the model. These are pre-match probabilities, not certainties.</li>
      </ul>

      <h2>The public record</h2>
      <p>Every update is saved to this site&rsquo;s public history with a server-side timestamp, so anyone can check that a prediction was published before kick-off.</p>

      <h2 id="glossary">Glossary</h2>
      <p>Every technical term on the site, in plain words. The same text pops up from the <span className="qmark" aria-hidden="true">?</span><span className="sr">question-mark buttons</span> next to each term.</p>
      <dl className="gloss">
        {Object.entries(TERMS).map(([k, [name, text]]) => (
          <div key={k}><dt id={`term-${k}`}>{name}</dt><dd>{text}</dd></div>
        ))}
      </dl>
    </article>
  )
}
