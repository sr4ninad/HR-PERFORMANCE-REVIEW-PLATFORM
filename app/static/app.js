/* Small progressive enhancements. Everything works without this file:
   the theme falls back to the OS preference, toasts simply stay on screen,
   and rings render at their final value. */
(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* ---- Theme toggle ---------------------------------------------------- */

  function currentTheme() {
    var chosen = document.documentElement.getAttribute("data-theme");
    if (chosen) return chosen;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  function labelToggles() {
    var next = currentTheme() === "light" ? "dark" : "light";
    document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-label", "Switch to " + next + " theme");
      button.setAttribute("title", "Switch to " + next + " theme");
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-theme-toggle]");
    if (!button) return;
    var next = currentTheme() === "light" ? "dark" : "light";
    var apply = function () {
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) { /* private mode: theme just won't persist */ }
      labelToggles();
    };
    if (document.startViewTransition && !reduceMotion.matches) {
      document.startViewTransition(apply);
    } else {
      apply();
    }
  });

  /* ---- Toasts ------------------------------------------------------------ */

  function dismiss(toast) {
    if (!toast || toast.classList.contains("is-leaving")) return;
    toast.classList.add("is-leaving");
    setTimeout(function () { toast.remove(); }, reduceMotion.matches ? 0 : 220);
  }

  function initToasts(root) {
    root.querySelectorAll(".toast:not([data-ready])").forEach(function (toast) {
      toast.setAttribute("data-ready", "");
      var timer = setTimeout(function () { dismiss(toast); }, 5000);
      // Pause while the pointer is over it, so it can be read.
      toast.addEventListener("mouseenter", function () { clearTimeout(timer); });
      toast.addEventListener("mouseleave", function () { timer = setTimeout(function () { dismiss(toast); }, 2500); });
    });
  }

  document.addEventListener("click", function (event) {
    var close = event.target.closest(".toast-close");
    if (close) dismiss(close.closest(".toast"));
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") document.querySelectorAll(".toast").forEach(dismiss);
  });

  /* ---- Rings: sweep from empty to their value -------------------------- */

  function animateRings(root) {
    if (reduceMotion.matches) return;
    root.querySelectorAll(".ring-fill:not([data-ready])").forEach(function (fill) {
      fill.setAttribute("data-ready", "");
      var target = fill.getAttribute("stroke-dashoffset");
      fill.style.transition = "none";
      fill.style.strokeDashoffset = fill.getAttribute("stroke-dasharray");
      fill.getBoundingClientRect(); // commit the starting state
      fill.style.transition = "";
      requestAnimationFrame(function () { fill.style.strokeDashoffset = target; });
    });
  }

  /* ---- Wire up on first load and after every htmx swap ------------------- */

  function init(root) {
    labelToggles();
    initToasts(root);
    animateRings(root);
  }

  document.addEventListener("DOMContentLoaded", function () { init(document); });
  document.addEventListener("htmx:load", function (event) { init(event.target); });
})();
