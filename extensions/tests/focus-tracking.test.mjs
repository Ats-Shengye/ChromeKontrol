/**
 * Phase F7 テスト: onWindowFocusChanged() / getFocusTs() の挙動を固定する。
 *
 * 対象範囲（ISSUES.md P1-5, phase-f7-spec.md）:
 *   - WINDOW_ID_NONE（-1）では通知を送らず storage も更新しない
 *   - 通常のウィンドウIDでは storage に ts を記録し、WebSocket が OPEN なら通知を送る
 *   - 同一ウィンドウIDへの連続したフォーカス取得では2回目以降送らない（Firefoxの重複発火対策）
 *   - 別のウィンドウIDに変わったら再度送る（抑制が過剰でないこと）
 *   - WebSocket が OPEN でない場合は送信しないが、storage への記録は行う
 *   - getFocusTs() が storage の記録を優先し、なければ getLastFocused() の
 *     focused === true で Date.now() を返す
 *   - getFocusTs() が両方失敗した場合に null を返し、例外を投げない
 *
 * connect() の非同期処理待ちについて: background.js はトップレベルで
 * connect() を呼び、その中で `ws = new WebSocket(url)` を非同期に設定する
 * （ensureIdentifyPayload() 等、複数階層の await を経由するため）。
 * loadBackground() から戻った直後は getWs() がまだ null を返しうるため、
 * 各テストの冒頭で `await flushMicrotasks()` を挟み、ws の生成を待つ。
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadBackground, DummyWebSocket } from './helpers/load-background.mjs';

/**
 * connect() 内の Promise チェーン（マイクロタスク）が全て解決されるのを待つ。
 * setTimeout(fn, 0) はマクロタスクとしてスケジュールされるため、Node.jsの
 * イベントループはそれを実行する前に、その時点までに積まれた全ての
 * マイクロタスク（await チェーン）を処理し終える。
 * @returns {Promise<void>}
 */
function flushMicrotasks() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

// ---------------------------------------------------------------------------
// onWindowFocusChanged()
// ---------------------------------------------------------------------------

test('onWindowFocusChanged: WINDOW_ID_NONE (-1) では通知を送らずstorageも更新しない', async () => {
  const { context, getWs, focusListeners } = loadBackground({ namespace: 'chrome' });
  await flushMicrotasks();
  const ws = getWs();
  ws.readyState = DummyWebSocket.OPEN;

  focusListeners[0](-1);

  assert.deepEqual(ws.sentMessages, []);
  assert.strictEqual(await context.storageGet('ck_last_focus_ts'), undefined);
});

test('onWindowFocusChanged: 通常のウィンドウIDではstorageに記録し、WebSocketがOPENなら通知を送る', async () => {
  const { context, getWs, focusListeners } = loadBackground({ namespace: 'chrome' });
  await flushMicrotasks();
  const ws = getWs();
  ws.readyState = DummyWebSocket.OPEN;

  const before = Date.now();
  focusListeners[0](7);
  const after = Date.now();

  assert.strictEqual(ws.sentMessages.length, 1);
  const sent = JSON.parse(ws.sentMessages[0]);
  assert.strictEqual(sent.type, 'focus');
  assert.ok(sent.ts >= before && sent.ts <= after, `ts (${sent.ts}) should be within [${before}, ${after}]`);

  const stored = await context.storageGet('ck_last_focus_ts');
  assert.strictEqual(stored, sent.ts);
});

test('onWindowFocusChanged: 同一ウィンドウIDへの連続したフォーカス取得では2回目以降送らない', async () => {
  const { getWs, focusListeners } = loadBackground({ namespace: 'chrome' });
  await flushMicrotasks();
  const ws = getWs();
  ws.readyState = DummyWebSocket.OPEN;

  focusListeners[0](3);
  assert.strictEqual(ws.sentMessages.length, 1);

  // Firefox's onFocusChanged fires multiple times for a single focus change;
  // the second/third call with the same windowId must be suppressed.
  focusListeners[0](3);
  focusListeners[0](3);
  assert.strictEqual(ws.sentMessages.length, 1, 'repeated notifications for the same windowId must be suppressed');
});

test('onWindowFocusChanged: 別のウィンドウIDに変わったら再度送る（抑制が過剰でない）', async () => {
  const { getWs, focusListeners } = loadBackground({ namespace: 'chrome' });
  await flushMicrotasks();
  const ws = getWs();
  ws.readyState = DummyWebSocket.OPEN;

  focusListeners[0](3);
  focusListeners[0](3); // suppressed
  focusListeners[0](9); // different window: must send again

  assert.strictEqual(ws.sentMessages.length, 2);
  const first = JSON.parse(ws.sentMessages[0]);
  const second = JSON.parse(ws.sentMessages[1]);
  assert.strictEqual(first.type, 'focus');
  assert.strictEqual(second.type, 'focus');
});

test('onWindowFocusChanged: WINDOW_ID_NONEを挟んでも直前とは別扱いにならない（連続抑制の対象外）', async () => {
  const { getWs, focusListeners } = loadBackground({ namespace: 'chrome' });
  await flushMicrotasks();
  const ws = getWs();
  ws.readyState = DummyWebSocket.OPEN;

  focusListeners[0](3); // sent, lastFocusedWindowId = 3
  focusListeners[0](-1); // ignored entirely: lastFocusedWindowId stays 3
  focusListeners[0](3); // same windowId as before -1: suppressed

  assert.strictEqual(ws.sentMessages.length, 1);
});

test('onWindowFocusChanged: WebSocketがOPENでない場合は送信しないがstorageへの記録は行う', async () => {
  const { context, getWs, focusListeners } = loadBackground({ namespace: 'chrome' });
  await flushMicrotasks();
  const ws = getWs();
  // Default readyState after connect() completes is CONNECTING, not OPEN.
  assert.strictEqual(ws.readyState, DummyWebSocket.CONNECTING);

  focusListeners[0](3);

  assert.deepEqual(ws.sentMessages, []);
  const stored = await context.storageGet('ck_last_focus_ts');
  assert.strictEqual(typeof stored, 'number');
  assert.ok(stored > 0);
});

test('onWindowFocusChanged: WebSocketがCLOSEDの場合も送信しないがstorageへの記録は行う', async () => {
  const { context, getWs, focusListeners } = loadBackground({ namespace: 'chrome' });
  await flushMicrotasks();
  const ws = getWs();
  ws.readyState = DummyWebSocket.CLOSED;

  focusListeners[0](4);

  assert.deepEqual(ws.sentMessages, []);
  const stored = await context.storageGet('ck_last_focus_ts');
  assert.strictEqual(typeof stored, 'number');
});

// ---------------------------------------------------------------------------
// getFocusTs()
// ---------------------------------------------------------------------------

test('getFocusTs: storageに記録があればそれを優先して返す', async () => {
  const { context } = loadBackground({ namespace: 'chrome' });
  await context.storageSet('ck_last_focus_ts', 555);
  const result = await context.getFocusTs();
  assert.strictEqual(result, 555);
});

test('getFocusTs: storageに記録がなくgetLastFocused().focused===trueならDate.now()を返す', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      windows: {
        async getLastFocused() {
          return { focused: true, tabs: [] };
        },
      },
    },
  });

  const before = Date.now();
  const result = await context.getFocusTs();
  const after = Date.now();

  assert.strictEqual(typeof result, 'number');
  assert.ok(result >= before && result <= after);
});

test('getFocusTs: storage記録がなくgetLastFocused().focusedがfalseならnullを返す', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      windows: {
        async getLastFocused() {
          return { focused: false, tabs: [] };
        },
      },
    },
  });
  assert.strictEqual(await context.getFocusTs(), null);
});

test('getFocusTs: 両方失敗した場合はnullを返し、例外を投げない', async () => {
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

  await assert.doesNotReject(async () => {
    assert.strictEqual(await context.getFocusTs(), null);
  });
});

test('getFocusTs: getLastFocusedがrejectしてもnullを返し、例外を投げない', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      windows: {
        async getLastFocused() {
          throw new Error('getLastFocused failed (test)');
        },
      },
    },
  });

  await assert.doesNotReject(async () => {
    assert.strictEqual(await context.getFocusTs(), null);
  });
});

test('getFocusTs: storageの値が0以下の数値なら無効値として扱いgetLastFocusedへフォールバックする', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      windows: {
        async getLastFocused() {
          return { focused: true, tabs: [] };
        },
      },
    },
  });
  await context.storageSet('ck_last_focus_ts', 0);
  const result = await context.getFocusTs();
  assert.strictEqual(typeof result, 'number');
  assert.notStrictEqual(result, 0);
});

// ---------------------------------------------------------------------------
// buildIdentifyPayload() との統合: focusTs が含まれる/含まれない
// ---------------------------------------------------------------------------

test('buildIdentifyPayload: getFocusTsが値を返せばペイロードにfocusTsとして含まれる', async () => {
  const { context } = loadBackground({ namespace: 'chrome' });
  await context.storageSet('ck_last_focus_ts', 12345);
  const payload = await context.buildIdentifyPayload();
  assert.strictEqual(payload.focusTs, 12345);
});

test('buildIdentifyPayload: getFocusTsがnullを返せばペイロードにfocusTsフィールド自体が現れない', async () => {
  const { context } = loadBackground({
    namespace: 'chrome',
    apiOverrides: {
      windows: {
        async getLastFocused() {
          return { focused: false, tabs: [] };
        },
      },
    },
  });
  const payload = await context.buildIdentifyPayload();
  assert.strictEqual('focusTs' in payload, false);
});
