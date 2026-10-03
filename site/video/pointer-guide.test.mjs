import {after, before, test} from 'node:test';
import assert from 'node:assert/strict';
import puppeteer from 'puppeteer';
import {installPointerGuide} from './pointer-guide.mjs';

let browser;
before(async () => {
  browser = await puppeteer.launch({headless:true,executablePath:process.env.DEMO_CHROME_PATH || undefined});
});
after(async () => {await browser?.close();});

test('pointer follows a drag even when the selected surface stops propagation', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<div id="surface" style="position:absolute;inset:0"></div>');
    await page.evaluate(() => {
      window.bubbledMoves = 0;
      document.addEventListener('mousemove', () => {window.bubbledMoves += 1;});
      document.getElementById('surface').addEventListener('mousemove', (event) => event.stopPropagation(), true);
    });
    await page.evaluate(installPointerGuide);
    await page.mouse.move(50,50);
    await page.mouse.down();
    for (const point of [[80,70],[120,90],[160,110]]) {
      await page.mouse.move(...point);
      const state = await page.evaluate(() => ({transform:document.getElementById('demo-pointer').style.transform,bubbledMoves:window.bubbledMoves}));
      assert.equal(state.transform,`translate(${point[0]}px, ${point[1]}px)`);
      assert.equal(state.bubbledMoves,0);
    }
    await page.mouse.up();
  } finally {await page.close();}
});

test('reinstalling the guide leaves one overlay and updates the replacement', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<p>Pointer fixture</p>');
    await page.evaluate(installPointerGuide);
    await page.evaluate(installPointerGuide);
    await page.mouse.move(200,150);
    const state = await page.evaluate(() => ({count:document.querySelectorAll('#demo-pointer').length,transform:document.getElementById('demo-pointer').style.transform}));
    assert.equal(state.count,1);
    assert.equal(state.transform,'translate(200px, 150px)');
  } finally {await page.close();}
});
