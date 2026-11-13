"""The snapshot CLIO's sandbox document takes of the view it hosts (CM1, CM2).

A view is the app author's page in a frame on its own sandbox origin
(:mod:`clio_agent.gact.mcp_app_sandbox_origin`), and the frame has
``allow-same-origin``, so CLIO's own outer sandbox document is same-origin with the view
and with any frame the view nests from that origin (a view's service page, MA23). The
host asks the sandbox document for a snapshot (``clio/view-snapshot``, never forwarded to
the view); the script below paints what the view shows onto one canvas and answers with a
PNG and what it could and could not paint. Nothing is changed in the view, and the view
is not asked.

What it paints, in order: element backgrounds, then every visible ``canvas``, ``img``,
``video`` and ``svg`` (``svg`` with its computed presentation styles), then HTML text that
sits on one line, each clipped to its frame and to every ancestor that clips its overflow;
an element that is hidden, fully transparent or clipped away entirely is not painted, as
the person does not see it. A WebGL canvas that keeps no drawing buffer
(``preserveDrawingBuffer: false``, as vtk.js uses) reads blank after the browser shows it,
so its context's draw calls are watched, the view is nudged to redraw (its frame shrinks
by one pixel for two frames), and the canvas is copied when the app's drawing task ends.

Every gap is typed, never silent: ``skipped`` lists ``frame_cross_origin``,
``webgl_frame_not_redrawn``, ``webgl_canvas_idle`` (a blank canvas beside one that redrew),
``cross_origin_pixels``, ``not_drawable`` and
``wrapped_text_not_drawn``; a refused snapshot answers ``{error: {reason, message}}``
(``view_not_laid_out``, ``view_not_readable``, ``snapshot_tainted``, ``snapshot_failed``).
"""

from __future__ import annotations

__all__ = ["SNAPSHOT_METHOD", "SNAPSHOT_SCRIPT"]

#: The host's request to the sandbox document (answered there, never sent to the view).
SNAPSHOT_METHOD = "clio/view-snapshot"

SNAPSHOT_SCRIPT = r"""
const clioViewSnapshot = (() => {
  const MAX_SIDE = 4096;
  const DRAWS = ['drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced',
    'drawRangeElements', 'blitFramebuffer', 'clear'];
  const frame = (win) => new Promise((done) => win.requestAnimationFrame(() => done()));
  const wait = (ms) => new Promise((done) => setTimeout(done, ms));
  const px = (value) => parseFloat(value) || 0;

  // Every window of the view this document may read, with its offset in the view.
  function windows(win, x, y, out, skipped) {
    out.push({ win, x, y });
    for (const element of win.document.querySelectorAll('iframe, frame')) {
      const box = element.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      let child;
      try {
        child = element.contentWindow;
        if (!child || !child.document || !child.document.documentElement) continue;
      } catch {
        skipped.push({ what: 'frame', reason: 'frame_cross_origin' });
        continue;
      }
      const style = win.getComputedStyle(element);
      windows(child, x + box.left + px(style.borderLeftWidth) + px(style.paddingLeft),
        y + box.top + px(style.borderTopWidth) + px(style.paddingTop), out, skipped);
    }
    return out;
  }

  function webgl(canvas) {
    // getContext with the type a canvas already uses returns that context; a 2D canvas
    // answers null. A sized, blank canvas with no context at all is left alone.
    for (const type of ['webgl2', 'webgl']) {
      try {
        const gl = canvas.getContext(type);
        if (gl) return gl;
      } catch { /* not this type */ }
    }
    return null;
  }

  function blank(source, width, height) {
    const probe = document.createElement('canvas');
    probe.width = Math.max(1, Math.min(64, width));
    probe.height = Math.max(1, Math.min(64, height));
    const context = probe.getContext('2d', { willReadFrequently: true });
    context.drawImage(source, 0, 0, probe.width, probe.height);
    const data = context.getImageData(0, 0, probe.width, probe.height).data;
    for (let i = 3; i < data.length; i += 4) if (data[i] !== 0) return false;
    return true;
  }

  // A WebGL canvas that keeps no drawing buffer is read right after the app draws it:
  // its draw calls are watched, the view is nudged to redraw, and the frame is copied
  // when the app's drawing task ends.
  async function redrawn(entries, nudge) {
    const copies = new Map();
    const restores = [];
    for (const { canvas, gl, win } of entries) {
      let queued = false;
      const copy = () => {
        queued = false;
        const out = document.createElement('canvas');
        out.width = canvas.width;
        out.height = canvas.height;
        out.getContext('2d').drawImage(canvas, 0, 0);
        copies.set(canvas, out);
      };
      for (const name of DRAWS) {
        const original = gl[name];
        if (typeof original !== 'function') continue;
        // Bound to the context itself: an app may call a method it took off the context.
        gl[name] = function (...args) {
          const value = original.apply(gl, args);
          if (!queued) {
            queued = true;
            win.queueMicrotask(copy);
          }
          return value;
        };
        restores.push(() => { gl[name] = original; });
      }
    }
    try {
      await nudge();
      copies.clear();
      // Until every canvas redrew, or the ones that did have been quiet for a while (a view
      // may keep a canvas it never draws on).
      const until = performance.now() + 2000;
      let drawn = 0;
      let quietSince = performance.now();
      while (performance.now() < until) {
        await frame(window);
        if (entries.every(({ canvas }) => copies.get(canvas)?.width === canvas.width)) break;
        if (copies.size !== drawn) {
          drawn = copies.size;
          quietSince = performance.now();
        } else if (drawn && performance.now() - quietSince > 400) break;
      }
      // One more frame so a redraw in several passes ends on its last one.
      await frame(window);
    } finally {
      for (const restore of restores) restore();
    }
    return copies;
  }

  const SVG_STYLE = ['fill', 'fill-opacity', 'stroke', 'stroke-width', 'stroke-opacity',
    'stroke-dasharray', 'opacity', 'font-family', 'font-size', 'font-weight', 'text-anchor',
    'dominant-baseline', 'visibility', 'display'];
  async function svgImage(svg, box, win) {
    const clone = svg.cloneNode(true);
    const from = [svg, ...svg.querySelectorAll('*')];
    const to = [clone, ...clone.querySelectorAll('*')];
    from.forEach((node, i) => {
      const style = win.getComputedStyle(node);
      to[i].setAttribute('style', SVG_STYLE.map((name) => `${name}:${style.getPropertyValue(name)}`).join(';'));
    });
    clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg');
    clone.setAttribute('width', String(box.width));
    clone.setAttribute('height', String(box.height));
    const text = new XMLSerializer().serializeToString(clone);
    const image = new Image();
    image.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(text);
    await image.decode();
    return image;
  }

  const clear = (color) => !color || color === 'transparent' || /rgba\([^)]*,\s*0\)$/.test(color);

  // The background a page shows: its body's, else its root's (a theme sets either).
  function pageBackground(win) {
    for (const node of [win.document.body, win.document.documentElement]) {
      if (!node) continue;
      const color = win.getComputedStyle(node).backgroundColor;
      if (!clear(color)) return color;
    }
    return '';
  }

  // HTML is drawn approximately: boxes with a background, and text that sits on one line.
  // What of an element's box the person sees: nothing when it or an ancestor is hidden or
  // fully transparent, else the box cut by every ancestor that clips its overflow and
  // contains it (a collapsed panel with its content still laid out inside shows nothing).
  function shown(element, box, win) {
    if (element.checkVisibility &&
      !element.checkVisibility({ opacityProperty: true, visibilityProperty: true })) return null;
    let left = box.left, top = box.top, right = box.right, bottom = box.bottom;
    let mode = win.getComputedStyle(element).position;
    for (let el = element.parentElement; el && mode !== 'fixed'; el = el.parentElement) {
      const style = win.getComputedStyle(el);
      const contains = mode !== 'absolute' || style.position !== 'static' || style.transform !== 'none';
      if (!contains || style.display === 'contents') continue;
      const clip = el.getBoundingClientRect();
      if (style.overflowX !== 'visible') { left = Math.max(left, clip.left); right = Math.min(right, clip.right); }
      if (style.overflowY !== 'visible') { top = Math.max(top, clip.top); bottom = Math.min(bottom, clip.bottom); }
      if (right - left < 1 || bottom - top < 1) return null;
      mode = style.position;
    }
    return { left, top, width: right - left, height: bottom - top };
  }

  function paintBackgrounds(context, frames, media) {
    let count = 0;
    for (const { win, x, y } of frames) {
      const all = win.document.body ? win.document.body.querySelectorAll('*') : [];
      context.save();
      context.beginPath();
      context.rect(x, y, win.innerWidth, win.innerHeight);
      context.clip();
      // The frame's own page background first (a themed view sets it on html or body).
      const page = pageBackground(win);
      if (page) {
        context.fillStyle = page;
        context.fillRect(x, y, win.innerWidth, win.innerHeight);
      }
      for (let i = 0; i < all.length && count < 6000; i += 1) {
        const element = all[i];
        if (media.has(element) || element.closest('svg')) continue;
        const style = win.getComputedStyle(element);
        if (style.visibility === 'hidden' || clear(style.backgroundColor)) continue;
        const box = element.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;
        const seen = shown(element, box, win);
        if (!seen) continue;
        context.fillStyle = style.backgroundColor;
        context.fillRect(x + seen.left, y + seen.top, seen.width, seen.height);
        count += 1;
      }
      context.restore();
    }
  }

  function paintText(context, frames) {
    let drawn = 0, missed = 0;
    for (const { win, x, y } of frames) {
      const doc = win.document;
      if (!doc.body) continue;
      context.save();
      context.beginPath();
      context.rect(x, y, win.innerWidth, win.innerHeight);
      context.clip();
      const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node && drawn + missed < 4000; node = walker.nextNode()) {
        const text = node.nodeValue ? node.nodeValue.replace(/\s+/g, ' ').trim() : '';
        const parent = node.parentElement;
        if (!text || !parent || parent.closest('svg, script, style, noscript, canvas')) continue;
        const style = win.getComputedStyle(parent);
        if (style.visibility === 'hidden') continue;
        const range = doc.createRange();
        range.selectNodeContents(node);
        const rects = range.getClientRects();
        if (!rects.length) continue;
        if (rects.length > 1) { missed += 1; continue; }
        const box = rects[0];
        if (box.width < 1 || box.height < 1) continue;
        const seen = shown(parent, box, win);
        if (!seen) continue;
        context.save();
        context.beginPath();
        context.rect(x + seen.left, y + seen.top, seen.width, seen.height);
        context.clip();
        context.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
        context.fillStyle = style.color;
        context.textBaseline = 'middle';
        context.fillText(text, x + box.left, y + box.top + box.height / 2, box.width + 2);
        context.restore();
        drawn += 1;
      }
      context.restore();
    }
    return { drawn, missed };
  }

  return async function capture(view, params = {}) {
    const width = view.innerWidth, height = view.innerHeight;
    if (!width || !height) return { error: { reason: 'view_not_laid_out', message: 'The view has no size on screen.' } };
    const side = Math.min(MAX_SIDE, Number(params.max_side) || MAX_SIDE);
    const scale = Math.max(0.25, Math.min(Number(params.scale) || 1, side / Math.max(width, height)));
    const skipped = [];
    const frames = windows(view, 0, 0, [], skipped);
    const items = [];
    const live = [];
    for (const { win, x, y } of frames) {
      for (const element of win.document.querySelectorAll('canvas, img, video, svg')) {
        if (element.tagName.toLowerCase() === 'svg' && element.parentElement?.closest('svg')) continue;
        const box = element.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) continue;
        if (box.right <= 0 || box.bottom <= 0 || box.left >= win.innerWidth || box.top >= win.innerHeight) continue;
        const style = win.getComputedStyle(element);
        if (style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
        const seen = shown(element, box, win);
        if (!seen) continue;
        const clip = { x: x + Math.max(0, seen.left), y: y + Math.max(0, seen.top) };
        clip.w = Math.min(x + win.innerWidth, x + seen.left + seen.width) - clip.x;
        clip.h = Math.min(y + win.innerHeight, y + seen.top + seen.height) - clip.y;
        if (clip.w < 1 || clip.h < 1) continue;
        const item = { element, win, x, y, box, kind: element.tagName.toLowerCase(), clip };
        items.push(item);
        if (item.kind === 'canvas' && element.width && element.height) {
          let empty = false;
          try { empty = blank(element, element.width, element.height); } catch { empty = false; }
          if (empty) {
            const gl = webgl(element);
            const attributes = gl?.getContextAttributes?.();
            if (gl && attributes && !attributes.preserveDrawingBuffer) live.push({ canvas: element, gl, win, item });
          }
        }
      }
    }
    let copies = new Map();
    if (live.length) copies = await redrawn(live, params.nudge || (async () => {}));
    const out = document.createElement('canvas');
    out.width = Math.round(width * scale);
    out.height = Math.round(height * scale);
    const context = out.getContext('2d');
    // Under a view with no background of its own: the host's themed background.
    context.fillStyle = params.background || '#ffffff';
    context.fillRect(0, 0, out.width, out.height);
    context.scale(scale, scale);
    const painted = new Set();
    const counts = { canvas: 0, image: 0, svg: 0, video: 0, text: 0 };
    paintBackgrounds(context, frames, new Set(items.map((item) => item.element)));
    for (const item of items) {
      const { element, box, x, y, kind, clip } = item;
      let source = element;
      try {
        if (kind === 'canvas' && copies.has(element)) source = copies.get(element);
        else if (kind === 'canvas' && live.some((entry) => entry.canvas === element)) {
          // Nothing drew when asked: the whole view is missing, or this canvas is one the
          // view keeps blank beside one that did draw.
          skipped.push({ what: 'canvas', reason: copies.size ? 'webgl_canvas_idle' : 'webgl_frame_not_redrawn' });
          continue;
        }
        if (kind === 'img' && !(element.complete && element.naturalWidth)) continue;
        if (kind === 'svg') source = await svgImage(element, box, item.win);
        const probe = document.createElement('canvas');
        probe.width = 1; probe.height = 1;
        const probeContext = probe.getContext('2d');
        probeContext.drawImage(source, 0, 0, 1, 1);
        probeContext.getImageData(0, 0, 1, 1); // throws when the source is another origin's
        context.save();
        context.beginPath();
        context.rect(clip.x, clip.y, clip.w, clip.h);
        context.clip();
        context.drawImage(source, x + box.left, y + box.top, box.width, box.height);
        context.restore();
        painted.add(element);
        counts[kind === 'img' ? 'image' : kind] += 1;
      } catch (error) {
        skipped.push({ what: kind === 'img' ? 'image' : kind, reason: error?.name === 'SecurityError' ? 'cross_origin_pixels' : 'not_drawable' });
      }
    }
    const text = paintText(context, frames);
    counts.text = text.drawn;
    if (text.missed) skipped.push({ what: 'text', reason: 'wrapped_text_not_drawn', count: text.missed });
    let data;
    try { data = out.toDataURL('image/png'); } catch {
      return { error: { reason: 'snapshot_tainted', message: 'The view shows pixels from another origin that cannot be read.' } };
    }
    return {
      image: data, width, height, scale,
      painted: counts, skipped,
    };
  };
})();
"""
