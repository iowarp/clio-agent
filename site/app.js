/* CLIO landing site — vanilla JS. Works as static files (file:// or GitHub Pages). */
(function () {
  "use strict";

  /* ---------- Copy-to-clipboard for [data-copy] blocks ---------- */
  function initCopy() {
    document.querySelectorAll("[data-copy]").forEach(function (block) {
      var btn = block.querySelector(".copy-btn");
      if (!btn) return;
      btn.addEventListener("click", function () {
        var text = block.getAttribute("data-copy") || "";
        copyText(text).then(function (ok) {
          var original = "Copy";
          btn.textContent = ok ? "Copied!" : "Copy failed";
          btn.classList.toggle("copied", ok);
          window.setTimeout(function () {
            btn.textContent = original;
            btn.classList.remove("copied");
          }, 1600);
        });
      });
    });
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(
        function () { return true; },
        function () { return legacyCopy(text); }
      );
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    try {
      var ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "absolute";
      ta.style.left = "-9999px";
      document.body.appendChild(ta);
      ta.select();
      var ok = document.execCommand("copy");
      document.body.removeChild(ta);
      return ok;
    } catch (e) {
      return false;
    }
  }

  /* ---------- Install tabs (Unix / Windows) ---------- */
  function initTabs() {
    var tabs = document.querySelectorAll(".tab[data-tab]");
    if (!tabs.length) return;
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        var key = tab.getAttribute("data-tab");
        document.querySelectorAll(".tab[data-tab]").forEach(function (t) {
          t.classList.toggle("active", t === tab);
        });
        document.querySelectorAll(".tabpane[data-pane]").forEach(function (p) {
          p.classList.toggle("active", p.getAttribute("data-pane") === key);
        });
      });
    });
  }

  /* ---------- Mobile nav toggle ---------- */
  function initNav() {
    var toggle = document.getElementById("navToggle");
    var links = document.getElementById("navLinks");
    if (!toggle || !links) return;
    toggle.addEventListener("click", function () {
      links.classList.toggle("open");
    });
    links.querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function () { links.classList.remove("open"); });
    });
  }

  /* ---------- Smooth-scroll for in-page anchors ---------- */
  function initSmoothScroll() {
    document.querySelectorAll('a[href^="#"]').forEach(function (a) {
      a.addEventListener("click", function (e) {
        var id = a.getAttribute("href");
        if (id === "#" || id.length < 2) return;
        var target = document.querySelector(id);
        if (!target) return;
        e.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      });
    });
  }

  /* ---------- Screenshot carousel ---------- */
  function initCarousel() {
    var root = document.getElementById("carousel");
    if (!root) return;
    var slides = Array.prototype.slice.call(root.querySelectorAll(".slide"));
    if (!slides.length) return;

    var captions = [
      "<strong>Inspect generated plots</strong> — artifacts shown in context",
      "<strong>Rendered reports &amp; evidence</strong> — tables, checklists, Markdown",
      "<strong>Generated artifacts</strong> — image &amp; file previews inline",
      "<strong>Review proposed edits</strong> — see diffs before anything changes",
      "<strong>Live, streaming responses</strong> — watch the work as it happens"
    ];

    var prev = document.getElementById("carPrev");
    var next = document.getElementById("carNext");
    var caption = document.getElementById("carCaption");
    var dotsWrap = document.getElementById("carDots");
    var index = 0;
    var timer = null;
    var DELAY = 6000;

    // build dots
    var dots = slides.map(function (_, i) {
      var d = document.createElement("button");
      d.className = "dot";
      d.type = "button";
      d.setAttribute("aria-label", "Go to slide " + (i + 1));
      d.addEventListener("click", function () { go(i, true); });
      dotsWrap.appendChild(d);
      return d;
    });

    function render() {
      slides.forEach(function (s, i) { s.classList.toggle("active", i === index); });
      dots.forEach(function (d, i) { d.classList.toggle("active", i === index); });
      if (caption) caption.innerHTML = captions[index] || "";
    }

    function go(i, userInitiated) {
      index = (i + slides.length) % slides.length;
      render();
      if (userInitiated) restart();
    }

    function step() { go(index + 1, false); }

    function start() { if (!timer) timer = window.setInterval(step, DELAY); }
    function stop() { if (timer) { window.clearInterval(timer); timer = null; } }
    function restart() { stop(); start(); }

    if (prev) prev.addEventListener("click", function () { go(index - 1, true); });
    if (next) next.addEventListener("click", function () { go(index + 1, true); });

    root.addEventListener("mouseenter", stop);
    root.addEventListener("mouseleave", start);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) stop(); else start();
    });

    render();
    start();
  }

  /* ---------- Desktop OS detection + GitHub release asset resolution ---------- */
  var REPO = "iowarp/clio-agent";
  var RELEASES_PAGE = "https://github.com/" + REPO + "/releases/latest";

  function detectOS() {
    var ua = (navigator.userAgent || "") + " " + (navigator.platform || "");
    ua = ua.toLowerCase();
    if (/(iphone|ipad|ipod|android)/.test(ua)) return "mobile";
    if (/mac|darwin/.test(ua)) return "macos";
    if (/win/.test(ua)) return "windows";
    if (/linux|x11/.test(ua)) return "linux";
    return "unknown";
  }

  // Each download link maps to the file extension(s) of its release asset,
  // in priority order. The first matching asset wins.
  var DL_EXT = {
    "windows-msi": [".msi"],
    "windows-exe": [".exe"],
    "macos-dmg": [".dmg"],
    "linux-appimage": [".appimage"],
    "linux-deb": [".deb"],
    "linux-rpm": [".rpm"]
  };

  function pickAsset(assets, extensions) {
    // extensions in priority order; return first matching browser_download_url
    for (var e = 0; e < extensions.length; e++) {
      var ext = extensions[e].toLowerCase();
      for (var a = 0; a < assets.length; a++) {
        var name = (assets[a].name || "").toLowerCase();
        if (name.endsWith(ext)) return assets[a].browser_download_url;
      }
    }
    return null;
  }

  function initDesktop() {
    var grid = document.getElementById("osGrid");
    if (!grid) return;

    // ----- OS detection: highlight + reorder the matching card -----
    var os = detectOS();
    if (os === "macos" || os === "windows" || os === "linux") {
      var card = grid.querySelector('.os-card[data-os="' + os + '"]');
      if (card) {
        card.classList.add("is-detected");
        grid.insertBefore(card, grid.firstChild);
      }
    }
    // mobile/unknown → no highlight, keep default order.

    // ----- Resolve download hrefs from the latest GitHub release -----
    fetch("https://api.github.com/repos/" + REPO + "/releases/latest", {
      headers: { Accept: "application/vnd.github+json" }
    })
      .then(function (r) {
        if (!r.ok) throw new Error("release fetch failed: " + r.status);
        return r.json();
      })
      .then(function (data) {
        var assets = (data && data.assets) || [];
        document.querySelectorAll("[data-dl]").forEach(function (link) {
          var key = link.getAttribute("data-dl");
          var exts = DL_EXT[key];
          if (!exts) return;
          var url = pickAsset(assets, exts);
          if (url) {
            link.href = url;
          } else {
            // No matching asset in this release — hide the dead link.
            link.style.display = "none";
          }
        });
      })
      .catch(function () {
        // graceful fallback: leave the default releases-page hrefs and show all buttons.
      });
  }

  /* ---------- Boot ---------- */
  function init() {
    initCopy();
    initTabs();
    initNav();
    initSmoothScroll();
    initCarousel();
    initDesktop();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
