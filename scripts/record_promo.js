// Records site-v3/_internal/promo.html into a 24.4s MP4 ready for X (1080p H.264).
//
// Requires: Microsoft Edge (installed by default on Windows) and ffmpeg in PATH.
// Usage:
//   npx -y playwright-core@latest --help >$null  # (only to confirm npx works)
//   node scripts/record_promo.js
//
// Output: sai-promo-x.mp4 in the repo root.

const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const PROMO = 'file:///' + path.join(ROOT, 'site-v3', '_internal', 'promo.html').replace(/\\/g, '/');
const OUT = path.join(ROOT, 'sai-promo-x.mp4');
const LOOP_SECONDS = 24.4; // one full loop, ends on the end card (restart is at 24.5s)

let chromium;
try {
  chromium = require('playwright-core').chromium;
} catch {
  console.log('installing playwright-core…');
  execFileSync('npm', ['i', '--no-save', '--no-audit', '--no-fund', 'playwright-core'], {
    cwd: ROOT, stdio: 'inherit', shell: true,
  });
  chromium = require('playwright-core').chromium;
}

(async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sai-promo-'));
  const browser = await chromium.launch({ channel: 'msedge' });
  const ctx = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    deviceScaleFactor: 1,
    recordVideo: { dir: tmp, size: { width: 1920, height: 1080 } },
  });
  const page = await ctx.newPage();
  const recStart = Date.now();
  await page.goto(PROMO);
  // recording starts at page creation, but the animation timers only start
  // once the inline script runs during load — trim that lead-in below
  const leadIn = ((Date.now() - recStart) / 1000).toFixed(2);
  console.log('recording one loop (~25s)…');
  await page.waitForTimeout(LOOP_SECONDS * 1000 + 800);
  const video = page.video();
  await ctx.close();
  const webm = await video.path();
  await browser.close();

  console.log('encoding MP4…');
  execFileSync('ffmpeg', [
    '-v', 'error', '-y', '-i', webm, '-ss', leadIn, '-t', String(LOOP_SECONDS),
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '18',
    '-pix_fmt', 'yuv420p', '-r', '30', '-movflags', '+faststart',
    OUT,
  ], { stdio: 'inherit' });

  fs.rmSync(tmp, { recursive: true, force: true });
  console.log('done: ' + OUT);
})().catch(e => { console.error(e); process.exit(1); });
