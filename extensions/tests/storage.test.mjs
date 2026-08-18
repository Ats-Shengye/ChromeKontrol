/**
 * Phase F3a テスト: storageGet / storageSet が Promise ベースで動くこと。
 *
 * 対象範囲（仕様書 phase-f3a-spec.md 4-2）:
 *   - chrome 環境（Promiseを返すモック）で値を取得・保存できる
 *   - browser 環境（Promiseを返すモック）でも同様に動く
 *   - API が reject した場合に呼び出し元へ例外が伝わる
 *
 * 書き換え後の storageGet / storageSet は chrome.runtime.lastError の
 * 検査を行わず、api.storage.local.get/set() 自体の reject をそのまま
 * 呼び出し元に伝播させる設計（フェーズ仕様 2-1）。ここではその伝播を固定する。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadBackground } from './helpers/load-background.mjs';

for (const namespace of ['chrome', 'browser']) {
  test(`storageGet/storageSet: ${namespace}環境で値を取得・保存できる`, async () => {
    const { context } = loadBackground({ namespace });

    // 未設定キーは undefined を返す（既存の chrome.storage.local.get の挙動と同じ）。
    assert.strictEqual(await context.storageGet('missing_key'), undefined);

    await context.storageSet('ck_test_key', 'test_value');
    assert.strictEqual(await context.storageGet('ck_test_key'), 'test_value');
  });
}

test('storageGet: APIがrejectした場合、呼び出し元へ例外が伝わる', async () => {
  const { context } = loadBackground({
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

  await assert.rejects(() => context.storageGet('any_key'), /storage\.local\.get failed \(test\)/);
});

test('storageSet: APIがrejectした場合、呼び出し元へ例外が伝わる', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      storage: {
        local: {
          set: async () => {
            throw new Error('storage.local.set failed (test)');
          },
        },
      },
    },
  });

  await assert.rejects(() => context.storageSet('any_key', 'value'), /storage\.local\.set failed \(test\)/);
});
