/**
 * Phase F3a テスト: getEmail() の存在チェックの固定。
 *
 * getEmail() は Firefox では api.identity.getProfileUserInfo が存在しない
 * ため、既存の存在チェック（chrome.identity → api.identity への置き換えのみ、
 * ロジックは変更なし）で null を返す設計（フェーズ仕様 2-3）。
 *
 * 対象範囲（仕様書 phase-f3a-spec.md 4-4）:
 *   - identity が存在しない環境 → null を返す
 *   - identity はあるが getProfileUserInfo が関数でない環境 → null を返す
 *   - getProfileUserInfo が使える環境 → email を返す
 *   - email が空文字列の場合 → null を返す（既存の email || null の挙動）
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadBackground } from './helpers/load-background.mjs';

test('getEmail: identityが存在しない環境（Firefox相当）ではnullを返す', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: { identity: undefined },
  });
  assert.strictEqual(await context.getEmail(), null);
});

test('getEmail: identityはあるがgetProfileUserInfoが関数でない環境ではnullを返す', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: { identity: { getProfileUserInfo: 'not-a-function' } },
  });
  assert.strictEqual(await context.getEmail(), null);
});

test('getEmail: getProfileUserInfoが使える環境ではemailを返す', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      identity: {
        async getProfileUserInfo() {
          return { email: 'user@example.com', id: 'profile-1' };
        },
      },
    },
  });
  assert.strictEqual(await context.getEmail(), 'user@example.com');
});

test('getEmail: emailが空文字列の場合はnullを返す（email || null の挙動）', async () => {
  // 既定モックの identity.getProfileUserInfo は { email: '', id: '' } を返す。
  const { context } = loadBackground({ namespace: 'chrome' });
  assert.strictEqual(await context.getEmail(), null);
});

test('getEmail: getProfileUserInfoがrejectした場合もnullを返す（例外を投げない）', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      identity: {
        async getProfileUserInfo() {
          throw new Error('getProfileUserInfo failed (test)');
        },
      },
    },
  });
  await assert.doesNotReject(async () => {
    assert.strictEqual(await context.getEmail(), null);
  });
});
