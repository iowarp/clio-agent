// Report whether the check browser can draw with WebGL 1 and 2, and with which renderer.
// Mol* and trame's local view need WebGL. Fails the run when WebGL 2 is missing.
//
//   node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/apps/webgl.mjs --label webgl
export default async (page) => {
  const result = await page.evaluate(() => {
    const out = {};
    for (const kind of ["webgl", "webgl2"]) {
      // One canvas per kind: a canvas that already has one context type refuses another.
      const gl = document.createElement("canvas").getContext(kind);
      if (!gl) {
        out[kind] = null;
        continue;
      }
      const info = gl.getExtension("WEBGL_debug_renderer_info");
      out[kind] = {
        version: gl.getParameter(gl.VERSION),
        renderer: info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER),
      };
    }
    return out;
  });
  console.log(`webgl: ${JSON.stringify(result)}`);
  if (!result.webgl2) throw new Error("the check browser has no WebGL 2");
};
