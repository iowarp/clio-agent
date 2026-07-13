/* CLIO landing site: vanilla JS. Works as static files (file:// or GitHub Pages). */
(function () {
  "use strict";

  /* ---------- Copy-to-clipboard for [data-copy] blocks ---------- */
  var ICON_COPY =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="11" height="11" rx="2"></rect>' +
    '<path d="M5 15V5a2 2 0 0 1 2-2h10"></path></svg>';
  var ICON_CHECK =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M20 6 9 17l-5-5"></path></svg>';

  function initCopy() {
    document.querySelectorAll("[data-copy]").forEach(function (block) {
      var btn = block.querySelector(".copy-btn");
      if (!btn) return;
      btn.innerHTML = ICON_COPY;
      var resetTimer = null;
      btn.addEventListener("click", function () {
        var text = block.getAttribute("data-copy") || "";
        copyText(text).then(function (ok) {
          btn.innerHTML = ok ? ICON_CHECK : ICON_COPY;
          btn.classList.toggle("copied", ok);
          btn.setAttribute("title", ok ? "Copied" : "Copy command");
          if (resetTimer) window.clearTimeout(resetTimer);
          resetTimer = window.setTimeout(function () {
            btn.innerHTML = ICON_COPY;
            btn.classList.remove("copied");
            btn.setAttribute("title", "Copy command");
          }, 1200);
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
  function selectTab(key) {
    document.querySelectorAll(".tab[data-tab]").forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-tab") === key);
    });
    document.querySelectorAll(".tabpane[data-pane]").forEach(function (p) {
      p.classList.toggle("active", p.getAttribute("data-pane") === key);
    });
  }

  function initTabs() {
    var tabs = document.querySelectorAll(".tab[data-tab]");
    if (!tabs.length) return;
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        selectTab(tab.getAttribute("data-tab"));
      });
    });
    // Auto-select the tab for the visitor's OS: Windows → PowerShell, else macOS/Linux.
    selectTab(detectOS() === "windows" ? "win" : "unix");
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
      "<strong>Inspect generated plots</strong>: artifacts shown in context",
      "<strong>Rendered reports &amp; evidence</strong>: tables, checklists, Markdown",
      "<strong>Generated artifacts</strong>: image &amp; file previews inline",
      "<strong>Review proposed edits</strong>: see diffs before anything changes",
      "<strong>Live, streaming responses</strong>: watch the work as it happens"
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

  // Detect CPU architecture as either "arm64" or "x64".
  // Most modern Macs are Apple Silicon, so when uncertain on a Mac we
  // default to arm64; Windows/Linux default to x64.
  function detectArch(os) {
    var hint = (navigator.userAgentData && navigator.userAgentData.architecture) || "";
    hint = hint.toLowerCase();
    if (hint) {
      if (/arm|aarch/.test(hint)) return "arm64";
      if (/x86|amd|x64/.test(hint)) return "x64";
    }
    var ua = ((navigator.userAgent || "") + " " + (navigator.platform || "")).toLowerCase();
    if (/aarch64|arm64|armv8/.test(ua)) return "arm64";
    if (/x86_64|x64|amd64|win64|wow64|intel/.test(ua)) {
      // "Intel" appears in many Apple-Silicon UA strings via Rosetta; keep
      // the Mac default below unless an explicit 64-bit token is present.
      if (os === "macos" && !/x86_64|x64|amd64/.test(ua)) return "arm64";
      return "x64";
    }
    // Uncertain: Apple Silicon is the safe default on Mac; x64 elsewhere.
    return os === "macos" ? "arm64" : "x64";
  }

  var ARCH_TOKENS = {
    arm64: ["arm64", "aarch64"],
    x64: ["x64", "x86_64", "amd64"]
  };

  // Every desktop download control resolves to a Tauri desktop-app asset, whose
  // name always begins with "CLIO.Desktop" (the Windows-ARM lite build is
  // "clio-desktop-…"). Restricting candidates to this family is what keeps the
  // terminal-UI binaries (clio-tui-…-amd64.exe) from ever being handed out as a
  // "desktop installer".
  var DESKTOP_ASSET_RE = /^clio.desktop/i;

  // Each download link maps to the file extension(s) of its release asset,
  // in priority order.
  var DL_EXT = {
    "windows-msi": [".msi"],
    "windows-exe": [".exe"],
    "macos-dmg": [".dmg"],
    "linux-appimage": [".appimage"],
    "linux-deb": [".deb"],
    "linux-rpm": [".rpm"]
  };

  // A "bundled" build embeds the CLIO backend and runs standalone; a "lite"
  // build is attach-only and needs a separately running clio-agent. The release
  // pipeline appends "-bundled" right before the extension for bundled builds.
  function isBundled(name) {
    return name.indexOf("-bundled") !== -1;
  }

  // Pure: resolve the best desktop-app asset for a set of candidate extensions
  // (priority order) and the detected arch. Returns { url, variant } where
  // variant is "bundled" or "lite", or null when nothing matches.
  //
  // Selection order per extension set: bundled+arch → bundled → lite+arch →
  // lite. Preferring the bundled installer means the primary download works
  // without a separate backend; the arch fallbacks keep every card populated.
  function pickAsset(assets, extensions, arch) {
    var tokens = ARCH_TOKENS[arch] || [];
    // Restrict to desktop-app assets (see DESKTOP_ASSET_RE) so a TUI binary can
    // never masquerade as a desktop installer.
    var candidates = [];
    for (var i = 0; i < assets.length; i++) {
      if (DESKTOP_ASSET_RE.test(assets[i].name || "")) candidates.push(assets[i]);
    }

    function find(wantBundled, wantArch) {
      for (var e = 0; e < extensions.length; e++) {
        var ext = extensions[e].toLowerCase();
        for (var c = 0; c < candidates.length; c++) {
          var name = (candidates[c].name || "").toLowerCase();
          if (!name.endsWith(ext)) continue;
          if (isBundled(name) !== wantBundled) continue;
          if (wantArch) {
            var archMatch = false;
            for (var t = 0; t < tokens.length; t++) {
              if (name.indexOf(tokens[t]) !== -1) { archMatch = true; break; }
            }
            if (!archMatch) continue;
          }
          return candidates[c].browser_download_url;
        }
      }
      return null;
    }

    var url = find(true, true) || find(true, false);
    if (url) return { url: url, variant: "bundled" };
    url = find(false, true) || find(false, false);
    if (url) return { url: url, variant: "lite" };
    return null;
  }

  // Human caption for a resolved variant; drives the per-download honesty note.
  function variantNote(variant) {
    if (variant === "bundled") return "Includes the CLIO backend — runs standalone.";
    if (variant === "lite") return "Attach-only build — requires a running clio-agent.";
    return "";
  }

  function initDesktop() {
    var grid = document.getElementById("osGrid");
    if (!grid) return;

    // ----- OS detection: highlight + reorder the matching card -----
    var os = detectOS();
    var arch = detectArch(os);
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
        var variantByKey = {};
        document.querySelectorAll("[data-dl]").forEach(function (link) {
          var key = link.getAttribute("data-dl");
          var exts = DL_EXT[key];
          if (!exts) return;
          var picked = pickAsset(assets, exts, arch);
          // Resolve to the exact asset when found; otherwise fall back to the
          // releases page so every control always works and no card looks empty.
          link.href = (picked && picked.url) || RELEASES_PAGE;
          var variant = picked ? picked.variant : "";
          variantByKey[key] = variant;
          // Expose the resolved variant and label each control honestly, so an
          // attach-only build is never presented as self-contained.
          if (variant) {
            link.setAttribute("data-variant", variant);
            link.setAttribute("title", variantNote(variant));
          }
        });
        // Per-card caption under the primary button, driven by that card's
        // primary download key.
        document.querySelectorAll("[data-dl-note]").forEach(function (note) {
          note.textContent = variantNote(variantByKey[note.getAttribute("data-dl-note")]);
        });
      })
      .catch(function () {
        // graceful fallback: leave the default releases-page hrefs and show all buttons.
      });
  }

  /* ---------- Boot ---------- */
  /* Make the whole OS card act like a button: a click anywhere on it triggers
     that card's primary download (clicks on the actual links still work as-is). */
  function initOsCards() {
    var cards = document.querySelectorAll(".os-card");
    for (var i = 0; i < cards.length; i++) {
      (function (card) {
        card.addEventListener("click", function (e) {
          if (e.target.closest("a")) return; // a real link was clicked
          var primary = card.querySelector(".btn-primary[href], a[data-dl][href]");
          if (primary && primary.href) window.open(primary.href, "_blank", "noopener");
        });
      })(cards[i]);
    }
  }

  function init() {
    initCopy();
    initTabs();
    initNav();
    initSmoothScroll();
    initCarousel();
    initDesktop();
    initOsCards();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }

  // Expose the pure asset-resolution helpers for a Node dry-run/test harness.
  // Guarded so it is inert in the browser (GitHub Pages), where `module` is
  // undefined.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { pickAsset: pickAsset, variantNote: variantNote, DL_EXT: DL_EXT };
  }
})();
