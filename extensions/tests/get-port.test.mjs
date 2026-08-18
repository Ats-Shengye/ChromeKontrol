/**
 * Phase F3a テスト: getPort() のフォールバック挙動の固定。
 *
 * getPort() は他の3つのラッパー（storageGet/storageSet/getProfileUserInfoAsync）と
 * エラー方針が異なる。api.storage.local.get() が reject しても例外を投げず、
 * 必ず DEFAULT_WS_PORT (9765) で解決する（フェーズ仕様 2-2 の最重要ケース）。
 *
 * 対象範囲（仕様書 phase-f3a-spec.md 4-3）:
 *   - 妥当なポート値が保存されている → その値を返す
 *   - 値が未設定 / 範囲外（0以下、65536以上）/ 数値でない → DEFAULT_WS_PORT を返す
 *   - API が reject した場合も DEFAULT_WS_PORT を返す（例外を投げない）
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadBackground } from './helpers/load-background.mjs';

test('getPort: 妥当なポート値が保存されていればその値を返す', async () => {
  const { context, DEFAULT_WS_PORT } = loadBackground({ namespace: 'chrome' });
  await context.storageSet('ws_port', 12345);
  const port = await context.getPort();
  assert.strictEqual(port, 12345);
  assert.notStrictEqual(port, DEFAULT_WS_PORT);
});

test('getPort: 値が未設定ならDEFAULT_WS_PORTを返す', async () => {
  const { context, DEFAULT_WS_PORT } = loadBackground({ namespace: 'chrome' });
  assert.strictEqual(await context.getPort(), DEFAULT_WS_PORT);
  assert.strictEqual(DEFAULT_WS_PORT, 9765);
});

for (const badValue of [0, -1, 65536, 100000, 'not-a-number', null, {}]) {
  test(`getPort: 範囲外/非数値の値（${JSON.stringify(badValue)}）はDEFAULT_WS_PORTにフォールバックする`, async () => {
    const { context, DEFAULT_WS_PORT } = loadBackground({ namespace: 'chrome' });
    await context.storageSet('ws_port', badValue);
    assert.strictEqual(await context.getPort(), DEFAULT_WS_PORT);
  });
}

test('getPort: APIがrejectしても例外を投げずDEFAULT_WS_PORTを返す（最重要ケース）', async () => {
  const { context, DEFAULT_WS_PORT } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      storage: {
        local: {
          get: async () => {
            throw new Error('storage.local.get failed (test)');
          },
        },
      },
    },
  });

  // reject があっても getPort() 自体は reject せず、常に値を返すことを確認する。
  await assert.doesNotReject(async () => {
    const port = await context.getPort();
    assert.strictEqual(port, DEFAULT_WS_PORT);
  });
});
