// A visible mouse pointer for recorded walkthroughs.
//
// Headless Chromium draws no cursor, so a recording shows things happening with nothing
// pointing at them. `showCursor(page)` draws an arrow in the top page that follows every
// move made through `page.mouse` (moves inside iframes too, since it is driven from here,
// not from page events), shows a ring on press, and makes every `locator.click()` glide to
// its target first instead of jumping there.

const GLIDE_STEPS = 18;

export async function showCursor(page) {
  await page.addInitScript(() => {
    if (window.top !== window) return;
    const install = () => {
      if (document.getElementById('__walkthrough_cursor')) return;
      const style = document.createElement('style');
      style.textContent = `
        #__walkthrough_cursor { position: fixed; left: 0; top: 0; width: 22px; height: 22px;
          z-index: 2147483647; pointer-events: none; transform: translate(-100px, -100px);
          will-change: transform; }
        #__walkthrough_cursor svg { width: 22px; height: 22px; display: block;
          filter: drop-shadow(0 1px 2px rgba(0,0,0,.6)); transition: transform 80ms; }
        #__walkthrough_cursor.down svg { transform: scale(.82); }
        .__walkthrough_ring { position: fixed; width: 34px; height: 34px; margin: -17px 0 0 -17px;
          border-radius: 50%; border: 3px solid #ea7b2a; z-index: 2147483646;
          pointer-events: none; animation: __walkthrough_ring 520ms ease-out forwards; }
        @keyframes __walkthrough_ring { from { transform: scale(.4); opacity: .95 }
          to { transform: scale(1.5); opacity: 0 } }`;
      const cursor = document.createElement('div');
      cursor.id = '__walkthrough_cursor';
      cursor.innerHTML =
        '<svg viewBox="0 0 22 22"><path d="M3 2 L3 18 L7.5 13.8 L10.6 20.4 L13.3 19.2 ' +
        'L10.3 12.7 L16.6 12.7 Z" fill="#fff" stroke="#111" stroke-width="1.5" ' +
        'stroke-linejoin="round"/></svg>';
      document.documentElement.append(style, cursor);
    };
    window.__walkthroughCursor = {
      move(x, y) {
        install();
        const el = document.getElementById('__walkthrough_cursor');
        if (el) el.style.transform = `translate(${x - 3}px, ${y - 2}px)`;
      },
      press(down, x, y) {
        install();
        document.getElementById('__walkthrough_cursor')?.classList.toggle('down', down);
        if (!down) return;
        const ring = document.createElement('div');
        ring.className = '__walkthrough_ring';
        ring.style.left = `${x}px`;
        ring.style.top = `${y}px`;
        document.documentElement.append(ring);
        setTimeout(() => ring.remove(), 600);
      },
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install);
    else install();
  });

  const viewport = page.viewportSize() || { width: 1600, height: 1000 };
  let at = { x: viewport.width / 2, y: viewport.height / 2 };
  const draw = (call, ...args) =>
    page
      .evaluate(([name, values]) => window.__walkthroughCursor?.[name](...values), [call, args])
      .catch(() => {});

  const mouse = page.mouse;
  const move = mouse.move.bind(mouse);
  const down = mouse.down.bind(mouse);
  const up = mouse.up.bind(mouse);
  mouse.move = async (x, y, options = {}) => {
    const steps = Math.max(1, options.steps ?? 1);
    const from = at;
    for (let i = 1; i <= steps; i += 1) {
      const px = from.x + ((x - from.x) * i) / steps;
      const py = from.y + ((y - from.y) * i) / steps;
      await move(px, py);
      await draw('move', px, py);
    }
    at = { x, y };
  };
  mouse.down = async (options) => {
    await draw('press', true, at.x, at.y);
    return down(options);
  };
  mouse.up = async (options) => {
    await draw('press', false, at.x, at.y);
    return up(options);
  };

  // Every locator click glides to its target first. Locators from frames share the class.
  const proto = Object.getPrototypeOf(page.locator('body'));
  if (!proto.__walkthroughClick) {
    const click = proto.click;
    proto.__walkthroughClick = click;
    proto.click = async function glideThenClick(options = {}) {
      const wait = Math.min(options.timeout ?? 5000, 5000);
      const box = await this.boundingBox({ timeout: wait }).catch(() => null);
      if (box) {
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, {
          steps: GLIDE_STEPS,
        });
        await page.waitForTimeout(120);
        await draw('press', true, box.x + box.width / 2, box.y + box.height / 2);
        setTimeout(() => draw('press', false, 0, 0), 150);
      }
      return click.call(this, options);
    };
  }
}
