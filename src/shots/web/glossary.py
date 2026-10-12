"""Plain-language definitions of every technical term the UI uses. One source for the inline help
popovers and the glossary on the How it works page."""

from __future__ import annotations

# key -> (term as shown, short definition). Keep each definition to one or two sentences.
TERMS: dict[str, tuple[str, str]] = {
    "probability": (
        "Chance (probability)",
        "How likely the model thinks it is that the match reaches the shot threshold. 70% means about 7 in 10 "
        "similar matches get there. It is a chance, not a promise."),
    "threshold": (
        "Shot threshold",
        "The number of total shots you are asking about, both teams together (on target, off target and blocked). "
        "'24+' means 24 or more. Move the slider to ask about a different number."),
    "expected": (
        "Expected shots",
        "The model's middle estimate for the match total: half the time there are fewer, half the time more. "
        "It is not the same as the threshold, which is the line you are testing."),
    "sure80": (
        "80% sure",
        "A safe floor. '27+ at 80% sure' means the model is 80% confident the match has at least 27 shots. "
        "Higher confidence (90%) gives a lower floor; lower confidence (70%) a higher one."),
    "vs_league": (
        "Versus league average",
        "Expected shots minus what the average match in that league gets. +3.0 means about three more shots than "
        "a typical game in the competition."),
    "no_odds": (
        "No odds",
        "Bookmaker odds were not published yet, which is normal for matches more than a few days away. These "
        "matches are scored by a separate model trained without odds, so they are a little less sharp."),
    "odds": (
        "Odds published",
        "Bookmaker odds exist for this match and the model that uses them scored it. They help the model judge "
        "how open the game is likely to be."),
    "cold_start": (
        "Cold start",
        "At least one team has fewer than 5 earlier matches in our data (promoted sides, new competitions). "
        "The model has little to go on, so treat the numbers with extra caution."),
    "experimental": (
        "Experimental team numbers",
        "Shots for one team on its own. They pass the overall calibration checks but not yet the "
        "per-competition check, so trust them less than the match total."),
    "bar": (
        "The probability bar",
        "The bar fills in proportion to the chance. The vertical tick marks 70%: when the green fill passes it, "
        "the match clears the bar we highlight."),
    "calibrated": (
        "Calibrated",
        "A model is calibrated when its '70%' calls come true about 70% of the time. The track record page checks "
        "this on matches the model had not seen."),
}


def term(key: str) -> tuple[str, str]:
    return TERMS[key]
