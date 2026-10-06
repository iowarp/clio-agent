/** Install a visible pointer that observes real browser events before map handlers. */
export function installPointerGuide() {
  window.__clioDemoPointerCleanup?.();
  document.getElementById('demo-pointer')?.remove();
  const cursor = document.createElement('div');
  cursor.id = 'demo-pointer';
  cursor.style.cssText = 'position:fixed;left:0;top:0;width:28px;height:34px;z-index:2147483647;pointer-events:none;filter:drop-shadow(0 1px 2px #0008);display:none';
  cursor.innerHTML = '<svg viewBox="0 0 28 34"><path d="M3 2L3 27L10 21L16 32L21 29L15 19L25 18Z" fill="white" stroke="#083e48" stroke-width="2"/></svg>';
  document.body.append(cursor);
  const update = (event) => {
    cursor.style.display = 'block';
    cursor.style.transform = `translate(${event.clientX}px,${event.clientY}px)`;
  };
  // The scientific map consumes events during selection at its React root.
  // Observe the real position before that handler, rather than after bubbling.
  const events = ['pointermove', 'mousemove', 'pointerdown', 'pointerup'];
  for (const name of events) window.addEventListener(name, update, true);
  window.__clioDemoPointerCleanup = () => {
    for (const name of events) window.removeEventListener(name, update, true);
    cursor.remove();
  };
}
