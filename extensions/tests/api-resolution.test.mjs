/**
 * Phase F3a テスト: 名前空間解決（const api = ...）の固定。
 *
 * background.js:
 *   const api = typeof browser !== 'undefined' && browser.runtime ? browser : chrome;
 *
 * 対象範囲（仕様書 phase-f3a-spec.md 4-1）:
 *   - browser（runtimeを持つ）が存在する環境 → api が browser に解決される
 *   - browser が存在しない環境 → api が chrome に解決される
 *   - browser という名前のグローバルが存在するが runtime を持たない場合
 *     → chrome に解決される（誤検出しないこと）
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadBackground } from './helpers/load-background.mjs';

test('api resolution: browser が存在しない環境では chrome に解決される', () => {
  const { api, context } = loadBackground({ namespace: 'chrome' });
  // sandbox.chrome と同一オブジェクト参照であることまで確認する
  // （「resolve される」＝chrome側のモックオブジェクトそのものが選ばれたこと）。
  assert.strictEqual(api, context.chrome);
});

test('api resolution: browser（runtimeあり）が存在する環境では browser に解決される', () => {
  const { api, context } = loadBackground({ namespace: 'browser' });
  assert.strictEqual(api, context.browser);
  // chrome は意図的に未定義のため、api が誤って chrome 側に解決されていないことも
  // 併せて保証される（誤っていれば ReferenceError で本テスト自体が失敗する）。
  assert.strictEqual(context.chrome, undefined);
});

test('api resolution: browser という名前のグローバルが存在してもruntimeを持たなければ誤検出せずchromeに解決される', () => {
  const { api, context } = loadBackground({ namespace: 'browser-no-runtime' });
  assert.strictEqual(api, context.chrome);
  assert.notStrictEqual(api, context.browser);
});
