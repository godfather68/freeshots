// Runs in <head>, before first paint: marks JS as available and applies the remembered "guide dismissed"
// choice so the guide does not flash. Storage can be blocked (private mode), hence try/catch.
(function () {
  var d = document.documentElement;
  d.classList.add("js");
  try {
    if (location.hash === "#guide") localStorage.removeItem("shots.guide");
    if (localStorage.getItem("shots.guide") === "hidden") d.classList.add("guide-off");
  } catch (e) { /* storage unavailable: the guide simply stays visible */ }
})();
