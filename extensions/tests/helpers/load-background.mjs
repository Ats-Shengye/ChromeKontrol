/**
 * Phase F3a テストハーネス: background.js を node:vm の新規コンテキストで
 * 評価し、モックを注入してテスト対象の関数群を取り出すヘルパー。
 *
 * 設計上の注意（実測で確認済み）:
 *   node:vm の runInContext / runInNewContext では、評価したスクリプトの
 *   トップレベル `function` 宣言と `var` はコンテキストのグローバルオブジェクト
 *   （= contextify したサンドボックスオブジェクト）のプロパティになるが、
 *   トップレベルの `const` / `let` はならない（ECMAScriptのグローバル環境
 *   レコードの仕様通り。'use strict' の有無によらない）。
 *
 *   background.js のテスト対象関数（storageGet, storageSet, getPort, getEmail,
 *   getOrCreateProfileId, getProfileUserInfoAsync, detectBrowser 等）はすべて
 *   トップレベルの `function` 宣言なので、評価後にコンテキストオブジェクトから
 *   直接取り出せる。一方 `const api = ...` は const 宣言のため直接は取り出せない。
 *
 *   これに対応するため、background.js の生ソースは一切改変せず、同一の
 *   vm評価内（同じ字句スコープ）の末尾に小さなエピローグを追記して、
 *   `api` を globalThis 経由で明示的にエクスポートする。エピローグは
 *   テストハーネス側の文字列結合であり、background.js ファイル自体への
 *   書き込みは行わない。
 */

import { readFileSync } from 'node:fs';
import * as vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const BACKGROUND_JS_PATH = path.join(__dirname, '..', '..', 'chromium', 'background.js');

/** background.js の生ソース。全テストで使い回すため一度だけ読み込む。 */
const BACKGROUND_SOURCE = readFileSync(BACKGROUND_JS_PATH, 'utf8');

/**
 * background.js のトップレベル const/let（グローバルオブジェクトに乗らない）を
 * テストから参照できるようにするためのエピローグ。
 *
 * getWs（Phase F7 追加）: `let ws = null;` は connect() のトップレベル実行
 * （非同期）によって後から代入されるため、値そのものではなく getter 関数
 * として公開する。エピローグ自体は connect() の非同期処理が完了する前に
 * 同期評価されるため、値を直接 `ws` として export すると常に null になって
 * しまう。getter越しに呼び出し時点の最新値を取れるようにしている。
 */
const EXPORT_EPILOGUE = `
;globalThis.__ck_test_exports__ = { api, DEFAULT_WS_PORT, getWs: () => ws };
`;

/**
 * WebSocket のダミー実装。background.js は末尾で connect() をトップレベル
 * 実行し、その中で `new WebSocket(url)` を生成するため、コンストラクタが
 * 例外を投げないダミーが必須。onAlarm() / onWindowFocusChanged() は
 * ws.readyState を静的定数（CLOSED/CLOSING/OPEN/CONNECTING）と比較するため、
 * それらも持たせる。
 *
 * エクスポートしている理由（Phase F7 で追加）: onWindowFocusChanged() の
 * 「WebSocket が OPEN なら送る」分岐をテストするには、connect() が生成した
 * 実際のインスタンス（loadBackground() の戻り値の getWs() で取得）の
 * readyState をテストから書き換えたり、送信されたメッセージ（sentMessages）
 * を検証したりする必要がある。DummyWebSocket.OPEN 等の定数値をテスト側が
 * マジックナンバーで再定義せずに参照できるよう、クラスごとエクスポートする。
 */
export class DummyWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = DummyWebSocket.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    /** send() が呼ばれるたびにその引数（JSON文字列）を記録する（Phase F7）。 */
    this.sentMessages = [];
  }
  send(data) {
    this.sentMessages.push(data);
  }
  close() {}
}
DummyWebSocket.CONNECTING = 0;
DummyWebSocket.OPEN = 1;
DummyWebSocket.CLOSING = 2;
DummyWebSocket.CLOSED = 3;

/**
 * api.storage.local の既定モック。インメモリのオブジェクトに対して
 * get/set を行う、実際のstorage.local相当の最小実装。
 * @returns {{get: (keys: string[]) => Promise<object>, set: (items: object) => Promise<void>}}
 */
function createDefaultStorageLocal() {
  const store = {};
  return {
    async get(keys) {
      const list = Array.isArray(keys) ? keys : [keys];
      const result = {};
      for (const key of list) {
        if (Object.prototype.hasOwnProperty.call(store, key)) {
          result[key] = store[key];
        }
      }
      return result;
    },
    async set(items) {
      Object.assign(store, items);
    },
  };
}

/**
 * 既定のWebExtension APIモック一式。
 * テストごとに部分上書き（apiOverrides）してシナリオを作る。
 */
function createDefaultApiSurface() {
  return {
    runtime: {},
    storage: { local: createDefaultStorageLocal() },
    identity: {
      async getProfileUserInfo() {
        return { email: '', id: '' };
      },
    },
    tabs: {
      async query() {
        return [];
      },
      async get() {
        return {};
      },
      async sendMessage() {
        return undefined;
      },
    },
    windows: {
      async getLastFocused() {
        return { tabs: [] };
      },
      // Phase F7 (ISSUES.md P1-5): -1 と同値の定数。onWindowFocusChanged()
      // はこの値とのフォールバック比較（`windowId === -1 ||
      // windowId === api.windows.WINDOW_ID_NONE`）を行うため、実ブラウザの
      // API 表面に合わせてここにも定義する。
      WINDOW_ID_NONE: -1,
      onFocusChanged: {
        // 登録されたリスナーの実際の蓄積は loadBackground() 側で行う
        // （下記参照）。ここは apiOverrides で windows を上書きしない
        // すべてのテストに対する既定の no-op スタブ。
        addListener(_fn) {},
      },
    },
    scripting: {
      async executeScript() {
        return [];
      },
    },
    alarms: {
      create() {},
      onAlarm: { addListener() {} },
    },
  };
}

/** プレーンオブジェクトかどうか（配列・関数・nullを除く）。 */
function isPlainObject(value) {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && typeof value !== 'function';
}

/** apiOverrides を既定モックに再帰的にマージする（部分上書き用）。 */
function mergeDeep(base, overrides) {
  const result = { ...base };
  for (const [key, value] of Object.entries(overrides)) {
    // Prototype pollution ガード（セキュリティレビュー Phase F3a M-1, ISSUES.md P2-17）。
    // 現状 apiOverrides はテストコード内のリテラルオブジェクトからしか渡されず
    // Object.entries() が __proto__ を列挙することもないため到達不可能だが、
    // 将来テストが外部フィクスチャを JSON.parse() して apiOverrides に渡す形に
    // 拡張された場合、JSON.parse() は __proto__ を通常のキーとして返しうるため
    // その時点で発火する。安価な防御なので先回りして入れておく。
    if (key === '__proto__' || key === 'constructor') continue;
    if (isPlainObject(value) && isPlainObject(result[key])) {
      result[key] = mergeDeep(result[key], value);
    } else {
      result[key] = value;
    }
  }
  return result;
}

/**
 * background.js を新規 vm コンテキストで評価し、テスト対象を取り出す。
 *
 * @param {object} [options]
 * @param {'chrome'|'browser'|'browser-no-runtime'} [options.namespace='chrome']
 *   どのグローバル名前空間を用意するか。
 *     - 'chrome': `chrome` のみ定義する（`browser` は定義しない）。
 *       api が chrome に解決されること、かつ書き換え漏れの `chrome.`
 *       実行時参照がないこと（あれば chrome は定義済みなので動いてしまうため
 *       これ単体では検出できない）を確認する用途。
 *     - 'browser': `browser`（runtime あり）のみ定義する（`chrome` は
 *       定義しない）。api が browser に解決されることに加え、書き換え漏れの
 *       `chrome.` 実行時参照が万一残っていれば ReferenceError で
 *       検出できる（chrome グローバルが存在しないため）。
 *     - 'browser-no-runtime': `browser`（runtimeなし）と `chrome` の両方を
 *       定義する。api が誤検出せず chrome に解決されることを確認する用途。
 * @param {object} [options.apiOverrides] - 既定APIモックへの部分上書き。
 *   例: { storage: { local: { get: async () => { throw new Error('boom'); } } } }
 * @param {object} [options.navigatorOverrides] - navigator モックへの部分上書き。
 * @param {boolean} [options.navigatorThrows] - true の場合、navigator への
 *   プロパティアクセス自体が例外を投げるモックにする（detectBrowser()の
 *   防御的try/catchのテスト用）。
 * @returns {{ api: object, DEFAULT_WS_PORT: number, context: object,
 *   getWs: () => (DummyWebSocket|null), focusListeners: Function[] }}
 *   context からは storageGet / storageSet / getPort / getEmail /
 *   getOrCreateProfileId / getProfileUserInfoAsync / detectBrowser /
 *   getFocusTs / onWindowFocusChanged 等のトップレベル関数を直接取り出せる
 *   （例: context.getPort）。
 *   getWs()（Phase F7）: connect() が生成した現在の ws（DummyWebSocket
 *   インスタンス、まだ生成前なら null）を返す。connect() は非同期のため、
 *   呼び出し直後は null になりうる——先に `await new Promise((r) =>
 *   setTimeout(r, 0))` 等でマイクロタスクキューを空にしてから呼ぶこと。
 *   focusListeners（Phase F7）: `api.windows.onFocusChanged.addListener()`
 *   に登録された関数を登録順に保持する配列。background.js はトップレベルで
 *   1回だけ登録するため、通常は `focusListeners[0]` が
 *   onWindowFocusChanged 本体になる。
 */
export function loadBackground(options = {}) {
  const { namespace = 'chrome', apiOverrides = {}, navigatorOverrides = {}, navigatorThrows = false } = options;

  const apiSurface = mergeDeep(createDefaultApiSurface(), apiOverrides);

  // Phase F7 (ISSUES.md P1-5): windows.onFocusChanged.addListener() で
  // 登録されたリスナーをテストから呼び出せるよう、配列へ蓄積する実装に
  // 差し替える。createDefaultApiSurface() 側の既定実装は no-op のため、
  // apiOverrides で windows.onFocusChanged 自体を上書きしない限り、ここで
  // 必ず実際に蓄積する実装に置き換わる。
  const focusListeners = [];
  if (apiSurface.windows && apiSurface.windows.onFocusChanged) {
    apiSurface.windows.onFocusChanged.addListener = (fn) => {
      focusListeners.push(fn);
    };
  }

  const sandbox = {
    console: { log() {}, warn() {}, error() {} },
    crypto: { randomUUID: () => '00000000-0000-4000-8000-000000000000' },
    // isAllowedOrigin() が `new URL(url)` を呼ぶ（Phase F7 で判明: これが
    // 未定義だと isAllowedOrigin() は例外→catchでfalseを返し続け、
    // connect() が `ws = new WebSocket(url)` に到達する前に早期returnして
    // しまう。vm.createContext() のサンドボックスはNode.jsのWeb系グローバル
    // を継承しないため、Node.js組み込みのURLクラスをそのまま渡す）。
    URL,
    // scheduleReconnect() が使う。テストが再接続の発火を待たないよう、
    // コールバックを一切呼ばないダミーにする。
    setTimeout: () => 0,
    clearTimeout: () => {},
    WebSocket: DummyWebSocket,
  };

  if (navigatorThrows) {
    sandbox.navigator = new Proxy(
      {},
      {
        get() {
          throw new Error('navigator access denied (test)');
        },
      }
    );
  } else {
    sandbox.navigator = {
      userAgentData: undefined,
      userAgent: 'Mozilla/5.0',
      ...navigatorOverrides,
    };
  }

  if (namespace === 'chrome') {
    sandbox.chrome = apiSurface;
  } else if (namespace === 'browser') {
    sandbox.browser = apiSurface;
  } else if (namespace === 'browser-no-runtime') {
    sandbox.browser = { notRuntime: true };
    sandbox.chrome = apiSurface;
  } else {
    throw new Error(`Unknown namespace option: ${namespace}`);
  }

  const context = vm.createContext(sandbox);
  vm.runInContext(BACKGROUND_SOURCE + EXPORT_EPILOGUE, context, {
    filename: 'background.js (test harness)',
  });

  const exported = context.__ck_test_exports__;
  return {
    api: exported.api,
    DEFAULT_WS_PORT: exported.DEFAULT_WS_PORT,
    context,
    getWs: exported.getWs,
    focusListeners,
  };
}
