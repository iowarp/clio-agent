// Shared helpers for the Notebook theme qualification (docs/design/theme/notebook-theme-plan.md).
// The steps modules in this folder import them; each runs under
// `node scripts/cloud/ui_check.mjs --steps <module>` against the cloud stack.

export const VARIANTS = [
  { name: "light", choice: "Light", scheme: "light", id: "light", dark: false },
  { name: "dark", choice: "Dark", scheme: "dark", id: "dark", dark: true },
  { name: "notebook-light", choice: "Notebook", scheme: "light", id: "notebook", dark: false },
  { name: "notebook-dark", choice: "Notebook", scheme: "dark", id: "notebook", dark: true },
];

export const GALLERY = ["chart", "workflow", "status", "data-table", "code", "map"];

export const PROMPT =
  "Without using any tools, reply with a short Python code block that computes the mean of a list, " +
  "then a markdown table of three planets with their radius in km.";

/**
 * The app lists GitHub releases for its update notes. From the cloud's shared
 * egress that unauthenticated call is often rate-limited (HTTP 403), which is
 * not about the theme; answer it with an empty list so console errors report
 * the UI under test.
 */
export async function quietReleaseChecks(page) {
  await page.route(/^https:\/\/api\.github\.com\/repos\/[^/]+\/[^/]+\/releases/, (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: "[]" }),
  );
}

export function origin(page) {
  return new URL(page.url()).origin;
}

/** What <html> says about the applied theme. */
export function appliedTheme(page) {
  return page.evaluate(() => ({
    theme: document.documentElement.dataset.theme ?? null,
    dark: document.documentElement.classList.contains("dark"),
  }));
}

/** Follow a sidebar link, opening the sidebar first where it is a sheet (phone width). */
export async function sidebarLink(page, name) {
  const link = page.getByRole("link", { name, exact: true }).filter({ visible: true }).first();
  if (!(await link.isVisible().catch(() => false))) {
    await page.locator('[data-sidebar="trigger"]').filter({ visible: true }).first().click();
    await link.waitFor({ timeout: 10000 });
  }
  await link.click();
}

/** Pick a theme the way a person does: Settings, Appearance, Theme. */
export async function chooseTheme(page, choice) {
  const group = page.getByRole("radiogroup", { name: "Theme" });
  if (!(await group.isVisible().catch(() => false))) {
    // Inside Settings, open Appearance as a person would; elsewhere, load it.
    if (new URL(page.url()).pathname.startsWith("/settings")) {
      // Wide screens list the sections beside the page; phones put them in a header menu.
      const nav = page.getByRole("link", { name: "Appearance", exact: true }).filter({ visible: true }).first();
      const menu = page.getByRole("button", { name: "Choose settings section" }).filter({ visible: true });
      await nav.or(menu).first().waitFor({ timeout: 20000 });
      if (await nav.isVisible()) await nav.click();
      else {
        await menu.click();
        await page.getByRole("menuitem", { name: "Appearance" }).first().click();
      }
      await group.waitFor({ timeout: 15000 }).catch(() => {});
    }
    if (!(await group.isVisible().catch(() => false)))
      await page.goto(`${origin(page)}/settings/appearance`, { waitUntil: "load" });
    await group.waitFor({ timeout: 20000 });
  }
  await group.getByText(choice, { exact: true }).click();
  await page.waitForTimeout(600);
}

/** A settled conversation with code and a table: the oldest one, sending the prompt if there is none. */
export async function openConversation(page) {
  const agent = `http://127.0.0.1:${process.env.AGENT_PORT || 8787}`;
  const listing = await page.request.get(`${agent}/v1/sessions?workspace_id=ws_default`).catch(() => null);
  const sessions = listing?.ok() ? ((await listing.json()).sessions ?? []) : [];
  const settled = sessions
    .filter((session) => session.message_count >= 2 && session.status === "idle")
    .sort((a, b) => a.created_at.localeCompare(b.created_at))[0];
  if (settled) {
    await page.goto(`${origin(page)}/workspaces/${settled.workspace_id}/sessions/${settled.id}`, { waitUntil: "load" });
  } else {
    await page.goto(`${origin(page)}/`, { waitUntil: "load" });
    const composer = page.locator('textarea, [contenteditable="true"]').first();
    await composer.click();
    await page.keyboard.type(PROMPT);
    await page.keyboard.press("Enter");
    await page.getByText("Done", { exact: true }).last().waitFor({ timeout: 120000 }).catch(() => {});
  }
  await page.locator("pre, table").first().waitFor({ timeout: 30000 }).catch(() => {});
  await page.waitForTimeout(1500);
}

export async function openGallery(page, slug) {
  await page.goto(`${origin(page)}/widget-preview.html#${slug}`, { waitUntil: "load" });
  await page.getByRole("tab", { name: "Components" }).click();
  await page.waitForTimeout(2500);
}

export async function openFlowStates(page) {
  await page.goto(`${origin(page)}/tests/review/flow-states.html`, { waitUntil: "load" });
  await page.locator(".clio-flow-diagram svg").first().waitFor({ timeout: 20000 }).catch(() => {});
  await page.waitForTimeout(1000);
}

/**
 * Every visible element's computed colors and font, keyed by its position in
 * the tree. Two snapshots of the same screen compare element by element.
 */
export function styleSnapshot(page) {
  return page.evaluate(() => {
    const props = ["color", "background-color", "border-top-color", "fill", "stroke", "font-family"];
    const out = {};
    const path = (el) => {
      const parts = [];
      for (let node = el; node && node !== document.documentElement; node = node.parentElement) {
        parts.unshift(`${node.tagName}:${[...(node.parentElement?.children ?? [])].indexOf(node)}`);
      }
      return parts.join("/");
    };
    for (const el of document.querySelectorAll("body *")) {
      const box = el.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      const style = getComputedStyle(el);
      if (style.visibility === "hidden" || style.display === "none") continue;
      out[path(el)] = props.map((p) => style.getPropertyValue(p)).join(" | ");
    }
    return out;
  });
}

/** Elements whose computed style differs between two snapshots of the same screen. */
export function snapshotDiff(before, after) {
  const changed = [];
  for (const [key, value] of Object.entries(after)) {
    if (key in before && before[key] !== value) changed.push({ element: key, inPlace: before[key], reloaded: value });
  }
  return changed;
}

/**
 * Debug-theme audit: every visible element must take its colors and fonts
 * from the theme, which under Debug means red and the marker face. Data
 * encodings and media carry `data-theme-exempt` or are images and canvases.
 */
export function debugAudit(page) {
  return page.evaluate(() => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const rgba = (color) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = "rgba(0, 0, 0, 0)";
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, 1, 1);
      return [...ctx.getImageData(0, 0, 1, 1).data];
    };
    const isRed = ([r, g, b, a]) => {
      if (a === 0) return true;
      const max = Math.max(r, g, b);
      const min = Math.min(r, g, b);
      if (max !== r || max === min) return false;
      const lightness = (max + min) / 510;
      const saturation = (max - min) / 255 / (1 - Math.abs(2 * lightness - 1));
      const hue = (60 * (((g - b) / (max - min)) % 6) + 360) % 360;
      return saturation >= 0.5 && (hue <= 15 || hue >= 345);
    };
    const colorsIn = (value) =>
      value.match(/(?:rgba?|color|oklab|oklch|lab|lch|hsla?)\([^()]*(?:\([^()]*\)[^()]*)*\)/g) ?? [];
    const exempt = (el) =>
      el.closest("[data-theme-exempt], .katex, canvas, img, video, iframe, picture, .maplibregl-canvas-container");
    const describe = (el) => {
      const parts = [];
      for (let node = el; node && node !== document.body && parts.length < 4; node = node.parentElement) {
        const slot = node.getAttribute("data-slot");
        const cls = typeof node.className === "string" ? node.className.split(/\s+/).slice(0, 2).join(".") : "";
        parts.unshift(`${node.tagName.toLowerCase()}${slot ? `[${slot}]` : cls ? `.${cls}` : ""}`);
      }
      return parts.join(" > ");
    };
    const offenders = [];
    let checked = 0;
    for (const el of document.querySelectorAll("body *")) {
      if (exempt(el)) continue;
      const box = el.getBoundingClientRect();
      if (!box.width || !box.height) continue;
      for (const pseudo of [null, "::before", "::after"]) {
        const style = getComputedStyle(el, pseudo);
        if (style.display === "none" || style.visibility === "hidden") continue;
        if (pseudo && (style.content === "none" || style.content === "normal")) continue;
        const checks = [["color", style.color], ["background-color", style.backgroundColor]];
        for (const side of ["top", "right", "bottom", "left"]) {
          if (parseFloat(style.getPropertyValue(`border-${side}-width`)) > 0 && style.getPropertyValue(`border-${side}-style`) !== "none")
            checks.push([`border-${side}-color`, style.getPropertyValue(`border-${side}-color`)]);
        }
        if (style.outlineStyle !== "none" && parseFloat(style.outlineWidth) > 0) checks.push(["outline-color", style.outlineColor]);
        // Containers (svg, g) only pass fill and stroke down; the shapes paint.
        if (el instanceof SVGGeometryElement || el instanceof SVGTextContentElement) {
          if (style.fill && !style.fill.startsWith("none") && !style.fill.startsWith("url")) checks.push(["fill", style.fill]);
          if (style.stroke && !style.stroke.startsWith("none") && !style.stroke.startsWith("url")) checks.push(["stroke", style.stroke]);
        }
        for (const color of colorsIn(style.boxShadow)) checks.push(["box-shadow", color]);
        for (const color of colorsIn(style.backgroundImage)) checks.push(["background-image", color]);
        checked += 1;
        for (const [property, value] of checks) {
          if (!isRed(rgba(value))) offenders.push({ element: describe(el) + (pseudo ?? ""), property, value });
        }
        const ownText = [...el.childNodes].some((node) => node.nodeType === 3 && node.textContent.trim());
        if (!pseudo && ownText && !style.fontFamily.includes("clio-debug-marker"))
          offenders.push({ element: describe(el), property: "font-family", value: style.fontFamily });
      }
    }
    const unique = new Map();
    for (const offender of offenders) {
      const key = `${offender.element}|${offender.property}|${offender.value}`;
      if (!unique.has(key)) unique.set(key, offender);
    }
    return { checked, offenders: [...unique.values()] };
  });
}

/** Measured WCAG contrast of text and tint pairs in the applied theme. */
export function contrastPairs(page, pairs) {
  return page.evaluate((pairs) => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    const probe = document.createElement("span");
    document.body.append(probe);
    const token = (name) => {
      probe.style.color = `var(--${name})`;
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = getComputedStyle(probe).color;
      ctx.fillRect(0, 0, 1, 1);
      return [...ctx.getImageData(0, 0, 1, 1).data];
    };
    const hex = (c) => `#${c.slice(0, 3).map((v) => v.toString(16).padStart(2, "0")).join("")}`;
    const over = (top, bottom) => {
      const a = top[3] / 255;
      return [0, 1, 2].map((i) => Math.round(top[i] * a + bottom[i] * (1 - a))).concat(255);
    };
    const lum = (c) =>
      c.slice(0, 3).map((v) => { const s = v / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; })
        .reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
    const page = token("background");
    const rows = pairs.map(({ text, on, min }) => {
      const background = over(token(on), page);
      const ink = over(token(text), background);
      const [hi, lo] = [lum(ink), lum(background)].sort((a, b) => b - a);
      const ratio = (hi + 0.05) / (lo + 0.05);
      return { text, on, textColor: hex(ink), background: hex(background), ratio: Math.round(ratio * 100) / 100, min, pass: ratio >= min };
    });
    probe.remove();
    return rows;
  }, pairs);
}
