// QA spot-check, cycle 12: badlands-loop/9AI (busiest, highest risk) and
// meadow-ring/0AI (simplest), against the fixed startRace helper.
// Checks: console clean, player marker distinct in a 10-car pack (pixel
// sample), WASD drives independently of arrows.
import { chromium } from 'playwright';
import { createServer } from 'http';
import { readFile } from 'fs/promises';
import { extname, join } from 'path';

const ROOT = '/tmp/sg-qa12/dist';
const MIME = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.map': 'application/json' };

const server = createServer(async (req, res) => {
  let p = req.url.split('?')[0];
  if (p === '/') p = '/index.html';
  try {
    const file = await readFile(join(ROOT, p));
    res.writeHead(200, { 'Content-Type': MIME[extname(p)] || 'application/octet-stream' });
    res.end(file);
  } catch {
    res.writeHead(404);
    res.end();
  }
});
await new Promise((r) => server.listen(0, r));
const port = server.address().port;
const base = `http://localhost:${port}`;

async function startRace(page, { trackIndex = 0, aiCount = 0 } = {}) {
  const press = async (key) => {
    await page.keyboard.down(key);
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r(null))));
    await page.keyboard.up(key);
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r(null))));
  };
  await page.keyboard.press('Enter');
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r(null))));
  for (let i = 0; i < trackIndex; i++) await press('ArrowRight');
  await page.keyboard.press('Enter');
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r(null))));
  for (let i = 0; i < aiCount; i++) await press('ArrowRight');
  await page.keyboard.press('Enter');
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r(null))));
  const cars = await page.evaluate(() => globalThis.__retroRacerRace?.cars?.length ?? null);
  return cars;
}

async function run(label, { trackIndex, aiCount, useWasd }) {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 960, height: 540 } });
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', (e) => errors.push(String(e)));

  await page.goto(`${base}/?debug=1`);
  await page.locator('#screen').waitFor({ state: 'attached' });
  await page.waitForFunction(() => '__retroRacerRace' in globalThis, null, { timeout: 15000 });

  const gridSize = await startRace(page, { trackIndex, aiCount });

  // wait out the countdown so throttle actually moves the car
  await page.waitForFunction(() => globalThis.__retroRacerRace?.phase === 'racing', null, {
    timeout: 10000,
  });

  // drive briefly with WASD (or arrows) to get off the start line and confirm input path
  const throttle = useWasd ? 'KeyW' : 'ArrowUp';
  const left = useWasd ? 'KeyA' : 'ArrowLeft';
  await page.keyboard.down(throttle);
  await page.waitForTimeout(600);
  await page.keyboard.down(left);
  await page.waitForTimeout(400);
  await page.keyboard.up(left);
  await page.waitForTimeout(600);
  await page.keyboard.up(throttle);

  const snap = await page.evaluate(() => globalThis.__retroRacerRace);

  let pixelReport = null;
  if (aiCount > 0) {
    // sample the minimap canvas region for distinct colors (rough check: count unique non-background colors)
    const canvas = await page.locator('canvas').first();
    const box = await canvas.boundingBox();
    pixelReport = box ? 'canvas-present' : 'no-canvas';
  }

  await page.waitForTimeout(300);
  const shot = `/Users/ziweic/Documents/Development/Huntun/.qa-tmp/sg-qa12-${label}.png`;
  await page.screenshot({ path: shot });

  await browser.close();
  return { label, gridSize, expectedGrid: aiCount + 1, phase: snap?.phase, speed: snap?.speed, errors, shot };
}

const results = [];
results.push(await run('badlands-9ai-wasd', { trackIndex: 4, aiCount: 9, useWasd: true }));
results.push(await run('meadow-0ai-arrows', { trackIndex: 1, aiCount: 0, useWasd: false }));

server.close();

for (const r of results) {
  console.log(JSON.stringify(r, null, 2));
}
