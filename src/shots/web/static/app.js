// Progressive enhancement only: every control and every page works as plain HTML/GET without this file.
(function () {
  "use strict";
  var doc = document, root = doc.documentElement;
  var form = doc.getElementById("filters");
  var narrow = window.matchMedia("(max-width: 900px)");
  var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  // ------------------------------------------------------------------ slider value labels
  function showValue(r) {
    var out = doc.querySelector('output[for="' + r.id + '"]');
    if (!out) return;
    out.setAttribute("aria-live", "off"); // dragging a slider must not chatter; results are announced once
    out.textContent = (out.dataset.fmt || "{v}").replace("{v}", r.value);
  }
  doc.querySelectorAll("input[type=range]").forEach(showValue);
  doc.addEventListener("input", function (e) {
    if (e.target.type === "range") showValue(e.target);
  });

  // ------------------------------------------------------------------ first-visit guide
  var guide = doc.getElementById("guide");
  function remember(v) {
    try { if (v) localStorage.setItem("shots.guide", v); else localStorage.removeItem("shots.guide"); } catch (e) { /* ignore */ }
  }
  if (guide) {
    var gclose = guide.querySelector(".gclose");
    if (gclose) {
      gclose.hidden = false;
      gclose.addEventListener("click", function () {
        remember("hidden");
        root.classList.add("guide-off");
        var back = doc.querySelector("[data-guide-open]");
        if (back) back.focus();
      });
    }
  }
  function openGuide() {
    remember("");
    root.classList.remove("guide-off");
    if (!guide) return false;
    var h = guide.querySelector("h2");
    h.setAttribute("tabindex", "-1");
    guide.scrollIntoView({ behavior: reduced.matches ? "auto" : "smooth", block: "start" });
    h.focus({ preventScroll: true });
    return true;
  }
  doc.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("[data-guide-open]");
    if (a && guide && location.pathname === "/") { e.preventDefault(); openGuide(); }
  });
  window.addEventListener("hashchange", function () { if (location.hash === "#guide") openGuide(); });

  // ------------------------------------------------------------------ glossary popovers (<details class="term">)
  function closeTerms(except) {
    doc.querySelectorAll("details.term[open]").forEach(function (d) { if (d !== except) d.open = false; });
  }
  doc.addEventListener("toggle", function (e) {
    var d = e.target;
    if (!d.classList || !d.classList.contains("term") || !d.open) return;
    closeTerms(d);
    var pop = d.querySelector(".pop");
    if (!pop.querySelector(".popclose")) {
      var b = doc.createElement("button");
      b.type = "button"; b.className = "popclose"; b.textContent = "Close";
      b.addEventListener("click", function () { d.open = false; d.querySelector("summary").focus(); });
      pop.appendChild(b);
    }
    pop.style.left = "";
    if (getComputedStyle(pop).position === "absolute") { // keep it inside the viewport
      var vw = root.clientWidth, r = pop.getBoundingClientRect(), dx = 0;
      if (r.right > vw - 8) dx = vw - 8 - r.right;
      if (r.left + dx < 8) dx = 8 - r.left;
      if (dx) pop.style.left = dx + "px";
    }
  }, true);
  doc.addEventListener("click", function (e) {
    if (!e.target.closest || !e.target.closest("details.term")) closeTerms(null);
  });
  doc.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var open = doc.querySelector("details.term[open]");
    if (open) { open.open = false; open.querySelector("summary").focus(); e.preventDefault(); e.stopPropagation(); }
  }, true);

  // ------------------------------------------------------------------ scrollable tables are keyboard reachable
  function markScrollers() {
    doc.querySelectorAll(".tw").forEach(function (w) {
      var over = w.scrollWidth > w.clientWidth + 1;
      if (over && !w.hasAttribute("tabindex")) {
        var cap = w.querySelector("caption");
        w.setAttribute("tabindex", "0"); w.setAttribute("role", "region");
        w.setAttribute("aria-label", (cap ? cap.textContent : "Table") + " (scrollable)");
      } else if (!over && w.dataset.auto) {
        w.removeAttribute("tabindex"); w.removeAttribute("role"); w.removeAttribute("aria-label");
      }
      if (over) w.dataset.auto = "1";
    });
  }
  markScrollers();
  window.addEventListener("resize", markScrollers);

  if (!form) return;

  // ------------------------------------------------------------------ filter form: short URLs
  function collect() {
    var sp = new URLSearchParams();
    var sideOff = !form.querySelector('input[name="side"]:checked') || form.querySelector('input[name="side"]:checked').value === "";
    Array.prototype.forEach.call(form.elements, function (el) {
      if (!el.name || el.disabled) return;
      if ((el.type === "checkbox" || el.type === "radio") && !el.checked) return;
      var v = el.value;
      if (el.type === "range" && el.dataset.default !== undefined && v === el.dataset.default) return;
      if (el.type === "radio" && (el.name === "when" || el.name === "side") && v === "") return;
      if (el.type === "search" && !v.trim()) return;
      if (el.type === "number" && v === "") return;
      if (el.tagName === "SELECT" && v === "kickoff") return;
      if (sideOff && (el.name === "tn" || el.name === "tp") && form.querySelector('input[name="side"]')) return;
      sp.append(el.name, v);
    });
    return sp.toString();
  }
  function urlNow() { var q = collect(); return form.getAttribute("action") + (q ? "?" + q : ""); }
  form.addEventListener("submit", function (e) { e.preventDefault(); location.href = urlNow(); });

  // ------------------------------------------------------------------ auto-apply without losing focus
  // Fetch the filtered page, swap the results in place, keep focus on the control, announce the count.
  var seq = 0, timer, status = doc.getElementById("result-status");
  function swapIn(html, url) {
    var next = new DOMParser().parseFromString(html, "text/html");
    var cur = doc.getElementById("board-live"), neu = next.getElementById("board-live");
    if (!cur || !neu) throw new Error("unexpected page");
    cur.replaceWith(neu);
    var t = doc.getElementById("filters-title"), nt = next.getElementById("filters-title");
    if (t && nt) t.innerHTML = nt.innerHTML;
    history.replaceState(null, "", url);
    if (status) status.textContent = neu.dataset.status || "";
    syncDrawerUI();
    markScrollers();
  }
  function go() {
    var url = urlNow(), mine = ++seq;
    fetch(url, { headers: { Accept: "text/html" }, credentials: "same-origin" })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (html) { if (mine === seq) swapIn(html, url); })
      .catch(function () { location.href = url; });
  }
  form.addEventListener("change", function (e) {
    if (e.target.type === "search" || e.target.type === "number") return;
    go();
  });
  form.addEventListener("input", function (e) {
    if (e.target.type === "search" || e.target.type === "number") { clearTimeout(timer); timer = setTimeout(go, 600); }
  });
  var apply = form.querySelector('.actions button[type="submit"]');
  if (apply) apply.hidden = true; // auto-apply replaces it; it stays for no-JS use

  // ------------------------------------------------------------------ mobile filter drawer
  var backdrop = doc.createElement("div");
  backdrop.className = "backdrop"; backdrop.hidden = true;
  doc.body.appendChild(backdrop);
  var inerted = [], lastToggle = null;
  function isOpen() { return form.classList.contains("open"); }
  function setInert(on) {
    if (!on) { inerted.forEach(function (n) { n.inert = false; }); inerted = []; return; }
    var node = form;
    while (node && node !== doc.body) {
      Array.prototype.forEach.call(node.parentNode.children, function (s) {
        if (s !== node && s !== backdrop && s.id !== "result-status" && s.tagName !== "SCRIPT") { s.inert = true; inerted.push(s); }
      });
      node = node.parentNode;
    }
  }
  function openDrawer() {
    if (!narrow.matches || isOpen()) return;
    lastToggle = doc.activeElement;
    form.classList.add("open"); backdrop.hidden = false; doc.body.classList.add("noscroll");
    setInert(true);
    var t = doc.querySelector(".ftoggle"); if (t) t.setAttribute("aria-expanded", "true");
    form.querySelector(".fclose").focus();
  }
  function closeDrawer(restore) {
    if (!isOpen()) return;
    form.classList.remove("open"); backdrop.hidden = true; doc.body.classList.remove("noscroll");
    setInert(false);
    var t = doc.querySelector(".ftoggle"); if (t) t.setAttribute("aria-expanded", "false");
    if (restore !== false) { (t && t.offsetParent ? t : lastToggle || doc.body).focus(); }
  }
  function syncDrawerUI() {
    var on = narrow.matches;
    var t = doc.querySelector(".ftoggle"); if (t) { t.hidden = !on; t.setAttribute("aria-expanded", isOpen() ? "true" : "false"); }
    var c = form.querySelector(".fclose"); if (c) c.hidden = !on;
    var d = form.querySelector(".fdone");
    if (d) {
      d.hidden = !on;
      var live = doc.getElementById("board-live");
      d.textContent = live && live.dataset.shown ? "Show " + live.dataset.shown + " matches" : "Show matches";
    }
  }
  syncDrawerUI();
  (narrow.addEventListener ? narrow.addEventListener.bind(narrow, "change") : narrow.addListener.bind(narrow))(function () {
    if (!narrow.matches) closeDrawer(false);
    syncDrawerUI();
  });
  doc.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest(".ftoggle");
    if (t) { openDrawer(); return; }
    if (e.target === backdrop || (e.target.closest && e.target.closest(".fclose, .fdone"))) closeDrawer();
  });
  doc.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && isOpen()) { closeDrawer(); e.preventDefault(); }
  });
})();
