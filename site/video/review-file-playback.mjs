import puppeteer from 'puppeteer';
import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';

// Play every complete encoded clip at normal speed in its real page, then fullscreen.
// Decoded-frame callbacks cover the complete motion; half-second samples preserve
// the whole timeline for visual review, alongside the browser's error telemetry.
const destination = resolve('out/files-playback-review');
const pageUrl = process.env.DEMO_PAGE_URL ?? 'http://127.0.0.1:4107/docs/working-with-files/';
await mkdir(destination, {recursive: true});
const browser = await puppeteer.launch({headless: true, executablePath: process.env.DEMO_CHROME_PATH});
try {
const page = await browser.newPage();
await page.setViewport({width: 1440, height: 1000});
const errors = [];
const mediaCancellations = [];
const names = ['files-pdf-read', 'files-word-edit', 'files-office-review'];
page.on('pageerror', error => errors.push(String(error)));
page.on('requestfailed', request => {
  const reason = request.failure()?.errorText;
  const path = new URL(request.url()).pathname;
  if (reason === 'net::ERR_ABORTED' && request.resourceType() === 'media' && names.some(name => path === `/media/${name}.mp4`)) {
    // Seeking and navigating away cancel Chromium's range/preload requests.
    // Every clip must still complete playback below, without a media error.
    mediaCancellations.push({url: request.url(), reason});
  } else {
    errors.push(`${request.url()}: ${reason}`);
  }
});
await page.goto(pageUrl, {waitUntil: 'networkidle0'});
await page.select('starlight-theme-select select', 'light');
await page.screenshot({path: resolve(destination, 'page-desktop.png'), fullPage: true});
console.log('Page rendered; reviewing complete encoded clips');
const results = [];
for (const mode of ['embedded', 'fullscreen']) {
  for (const name of names) {
    const selector = `video:has(source[src="/media/${name}.mp4"])`;
    await page.$eval(selector, video => video.scrollIntoView({block: 'center'}));
    if (mode === 'fullscreen') {
      await page.locator(selector).click();
      await page.$eval(selector, video => video.requestFullscreen());
    }
    const playback = await page.$eval(selector, async video => {
      video.currentTime = 0;
      video.muted = true;
      video.playbackRate = 1;
      await new Promise(resolve => video.readyState >= 1 ? resolve() : video.addEventListener('loadedmetadata', resolve, {once: true}));
      const canvas = document.createElement('canvas');
      canvas.width = 480;
      canvas.height = Math.round(480 * video.videoHeight / video.videoWidth);
      const context = canvas.getContext('2d');
      let lastSample = -1;
      let decoded = 0;
      const samples = [];
      const frames = (_, metadata) => {
        decoded++;
        if (metadata.mediaTime - lastSample >= 0.48) {
          context.drawImage(video, 0, 0, canvas.width, canvas.height);
          samples.push({time: metadata.mediaTime, image: canvas.toDataURL('image/jpeg', 0.84)});
          lastSample = metadata.mediaTime;
        }
        if (!video.ended) video.requestVideoFrameCallback(frames);
      };
      video.requestVideoFrameCallback(frames);
      const completion = new Promise((resolve, reject) => {
        const watchdog = setTimeout(() => reject(new Error('Playback did not end within its bounded duration')), (video.duration + 10) * 1000);
        video.addEventListener('ended', resolve, {once: true});
        video.addEventListener('ended', () => clearTimeout(watchdog), {once: true});
        video.addEventListener('error', () => reject(new Error(video.error?.message)), {once: true});
      });
      const began = performance.now();
      await video.play();
      await completion;
      const rect = video.getBoundingClientRect();
      return {seconds: video.duration, ended: video.ended, wallSeconds: (performance.now() - began) / 1000,
        decoded, width: video.videoWidth, height: video.videoHeight, display: {width: rect.width, height: rect.height}, fullscreen: !!document.fullscreenElement,
        quality: video.getVideoPlaybackQuality().toJSON?.() ?? {totalVideoFrames: video.getVideoPlaybackQuality().totalVideoFrames, droppedVideoFrames: video.getVideoPlaybackQuality().droppedVideoFrames}, samples};
    });
    if (!playback.ended || playback.decoded < playback.seconds * 15 || Math.abs(playback.wallSeconds - playback.seconds) > 3) throw new Error(`Incomplete playback: ${name} ${mode}`);
    const samples = playback.samples;
    delete playback.samples;
    // Contact pages contain consecutive samples, never a few hand-selected frames.
    for (let index = 0; index < samples.length; index += 12) {
      const sheet = await page.evaluate(async frames => {
        const canvas = document.createElement('canvas');
        canvas.width = 1440; canvas.height = 1408;
        const context = canvas.getContext('2d');
        context.fillStyle = '#ffffff'; context.fillRect(0, 0, canvas.width, canvas.height);
        for (const [index, frame] of frames.entries()) {
          const image = new Image(); image.src = frame.image;
          await image.decode();
          const x = index % 3 * 480; const y = Math.floor(index / 3) * 352;
          context.drawImage(image, x, y, 480, 326);
          context.fillStyle = '#000000'; context.font = '16px sans-serif';
          context.fillText(`${frame.time.toFixed(2)}s`, x + 8, y + 346);
        }
        return canvas.toDataURL('image/jpeg', 0.9);
      }, samples.slice(index, index + 12));
      await writeFile(resolve(destination, `${name}-${mode}-${String(index/12).padStart(2, '0')}.jpg`), Buffer.from(sheet.split(',')[1], 'base64'));
    }
    const result = {name, mode, ...playback, samples: samples.length};
    results.push(result);
    console.log(JSON.stringify(result));
    if (mode === 'fullscreen') await page.evaluate(() => document.exitFullscreen());
  }
}
await page.setViewport({width: 390, height: 844});
await page.goto(pageUrl, {waitUntil: 'networkidle0'});
await page.screenshot({path: resolve(destination, 'page-mobile.png'), fullPage: true});
const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
await writeFile(resolve(destination, 'review.json'), JSON.stringify({results, errors, mediaCancellations, mobileOverflow: overflow}, null, 2));
if (errors.length || overflow) throw new Error(JSON.stringify({errors, overflow}));
} finally {
  await browser.close();
}
