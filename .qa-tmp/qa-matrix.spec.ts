import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

import { TRACKS } from '../../src/game/tracks';
import { trackGeometry } from '../../src/game/track';
import { DEFAULT_TUNING } from '../../src/game/tuning';
import type { RaceDebugHandle } from '../../src/engine/debug-overlay';
import { steerToward } from '../support/pilot';
import { startRace } from './support/boot';

const RACE_GLOBAL = '__retroRacerRace';

function readRaceSnapshot(page: Page): Promise<RaceDebugHandle | undefined> {
  return page.evaluate(
    (key) => (globalThis as unknown as Record<string, RaceDebugHandle | undefined>)[key],
    RACE_GLOBAL,
  );
}

async function boot(page: Page): Promise<void> {
  await page.goto('/?debug=1');
  await page.locator('#screen').waitFor({ state: 'attached' });
  await page.waitForFunction(() => '__retroRacerRace' in globalThis, null, { timeout: 15_000 });
}

const ACTIONS = ['throttle', 'brake', 'steerLeft', 'steerRight'] as const;
type Action = (typeof ACTIONS)[number];

class KeyDriver {
  private readonly page: Page;
  private readonly held = new Set<Action>();
  private readonly keyFor: Record<Action, string>;

  constructor(page: Page, binding: 'arrows' | 'wasd') {
    this.page = page;
    this.keyFor =
      binding === 'arrows'
        ? { throttle: 'ArrowUp', brake: 'ArrowDown', steerLeft: 'ArrowLeft', steerRight: 'ArrowRight' }
        : { throttle: 'KeyW', brake: 'KeyS', steerLeft: 'KeyA', steerRight: 'KeyD' };
  }

  async apply(desired: Partial<Record<Action, boolean>>): Promise<void> {
    for (const action of ACTIONS) {
      const want = desired[action] ?? false;
      const have = this.held.has(action);
      if (want === have) continue;
      if (want) {
        await this.page.keyboard.down(this.keyFor[action]);
        this.held.add(action);
      } else {
        await this.page.keyboard.up(this.keyFor[action]);
        this.held.delete(action);
      }
    }
  }

  async releaseAll(): Promise<void> {
    for (const action of [...this.held]) {
      await this.page.keyboard.up(this.keyFor[action]);
      this.held.delete(action);
    }
  }
}

async function driveToFinish(
  page: Page,
  geometry: ReturnType<typeof trackGeometry>,
  binding: 'arrows' | 'wasd',
  options: { pollMs?: number; deadline: number },
): Promise<{ snapshot: RaceDebugHandle | undefined; midRaceCars: number | null }> {
  const { pollMs = 50, deadline } = options;
  const driver = new KeyDriver(page, binding);
  let snapshot: RaceDebugHandle | undefined;
  let midRaceCars: number | null = null;
  try {
    while (Date.now() < deadline) {
      snapshot = await readRaceSnapshot(page);
      if (!snapshot) throw new Error(`globalThis.${RACE_GLOBAL} was never published`);
      if (snapshot.phase === 'finished') break;

      if (snapshot.phase === 'racing' && snapshot.position && snapshot.heading !== null) {
        if (midRaceCars === null && snapshot.lap !== null && snapshot.lap >= 1) {
          midRaceCars = snapshot.cars?.length ?? null;
        }
        await driver.apply(
          steerToward(
            geometry,
            DEFAULT_TUNING.track,
            snapshot.position,
            snapshot.heading,
            snapshot.speed ?? 0,
          ),
        );
      } else {
        await driver.apply({});
      }
      await page.waitForTimeout(pollMs);
    }
  } finally {
    await driver.releaseAll();
  }
  return { snapshot, midRaceCars };
}

const ROWS: { trackIndex: number; aiCount: number; binding: 'arrows' | 'wasd' }[] = TRACKS.flatMap(
  (_, trackIndex) => [
    { trackIndex, aiCount: 0, binding: trackIndex % 2 === 0 ? ('arrows' as const) : ('wasd' as const) },
    { trackIndex, aiCount: 9, binding: trackIndex % 2 === 0 ? ('wasd' as const) : ('arrows' as const) },
  ],
);

for (const { trackIndex, aiCount, binding } of ROWS) {
  const track = TRACKS[trackIndex];
  test(`QA matrix: ${track.id} / ${aiCount} AI / ${binding}`, async ({ page }) => {
    test.setTimeout(180_000);
    const errors: string[] = [];
    page.on('console', (m) => {
      if (m.type() === 'error') errors.push(m.text());
    });
    page.on('pageerror', (e) => errors.push(String(e)));

    await boot(page);
    await startRace(page, { trackIndex, aiCount });

    const geometry = trackGeometry(track);
    const { snapshot: finished, midRaceCars } = await driveToFinish(page, geometry, binding, {
      deadline: Date.now() + 150_000,
    });

    console.log(
      `ROWRESULT ${track.id} ai=${aiCount} binding=${binding} phase=${finished?.phase} midRaceCars=${midRaceCars} errors=${errors.length}`,
    );

    expect(errors, `console errors: ${JSON.stringify(errors)}`).toHaveLength(0);
    expect(finished?.phase, 'never reached finished').toBe('finished');
    expect(midRaceCars, 'grid size seen mid-race').toBe(aiCount + 1);

    await expect
      .poll(async () => (await readRaceSnapshot(page))?.screen, { timeout: 5_000 })
      .toBe('results');
  });
}
