/**
 * ChromeKontrol - background.js (Service Worker)
 *
 * 目的     : ローカルサーバー (server.py) へのWebSocket接続を管理し、
 *            アクティブタブのContent Scriptにコマンドを中継する。
 * 理由     : Manifest V3ではバックグラウンドページの代わりにService Workerが必要。
 *            Service Workerはリレーとして動作する: WSサーバー -> Content Script -> WSサーバー。
 * 関連     : content.js (DOM操作), server.py (WebSocketサーバー)
 *
 * セキュリティ注記: 接続はlocalhostオリジンのみに制限される。
 *   ポートはstorage (server.py起動時に設定) から読み取るため、ハードコードされない。
 *   悪意あるWebサイトによるコマンド偽装を防ぐため、
 *   すべての受信メッセージに対してオリジン検証を実行する。
 *
 * 依存関係: 外部ランタイム依存なし。使用するAPIはすべてネイティブの
 *   Chrome Extension API (chrome.storage, chrome.tabs, chrome.scripting)
 *   および標準WebSocketインターフェース。サードパーティライブラリは読み込まない。
 */

'use strict';

// --- 定数 ---

/** デフォルトのWebSocketポート。server.pyのDEFAULT_PORTと一致させる必要がある。 */
const DEFAULT_WS_PORT = 9765;

/** 再接続間隔（ミリ秒）。指数バックオフの基底値。 */
const RECONNECT_BASE_MS = 1000;

/** 再接続間隔の上限。過剰な再接続を防止する。 */
const RECONNECT_MAX_MS = 5000;

/** Keepaliveアラーム名（安定した値である必要がある。アラーム識別子として使用）。 */
const KEEPALIVE_ALARM_NAME = 'chromekontrol:keepalive';

/** Keepaliveアラームの周期（分単位、0.5 = 30秒）。 */
const KEEPALIVE_PERIOD_MINUTES = 0.5;

/** 許可するWebSocketオリジン。localhostのバリアントのみ受け入れる。
 *  セキュリティ設計: localhost以外のオリジンを明示的に拒否することで、
 *  リモート攻撃者がこの拡張機能経由でコマンドを中継するのを防ぐ。
 *
 *  注意: ws://0.0.0.0 は意図的に除外している。server.pyは127.0.0.1にのみ
 *  バインドするため、0.0.0.0は有効な接続先にならない。
 *  含めてもアローリストが広がるだけで実用的なメリットはない。 */
const ALLOWED_ORIGINS = new Set([
  'ws://127.0.0.1',
  'ws://localhost',
  'ws://[::1]',
]);

// --- 状態 ---

/** @type {WebSocket|null} アクティブなWebSocket接続。 */
let ws = null;

/** 現在の再接続遅延（ミリ秒）。接続成功時にリセットされる。 */
let reconnectDelay = RECONNECT_BASE_MS;

/** 再接続タイマーのハンドル。 */
let reconnectTimer = null;

/**
 * 並行するconnect()呼び出しを防ぐガードフラグ。
 * connect()開始時にtrueに設定され、open/error/catch時にクリアされる。
 */
let isConnecting = false;

// --- ヘルパー ---

/**
 * chrome.storage.localから設定済みのWebSocketポートを返す。
 * 取得できない場合はDEFAULT_WS_PORTにフォールバックする。
 * @returns {Promise<number>}
 */
async function getPort() {
  return new Promise((resolve) => {
    chrome.storage.local.get(['ws_port'], (result) => {
      const port = result.ws_port;
      if (typeof port === 'number' && port > 0 && port <= 65535) {
        resolve(port);
      } else {
        resolve(DEFAULT_WS_PORT);
      }
    });
  });
}

/**
 * WebSocket URLが許可されたlocalhostオリジンを使用しているか検証する。
 * セキュリティ注記: 悪意あるContent Scriptによってstorageが改ざんされた場合でも、
 * ローカル以外のサーバーへの接続を防止する。
 * @param {string} url - 検証するWebSocket URL。
 * @returns {boolean}
 */
function isAllowedOrigin(url) {
  try {
    const parsed = new URL(url);
    const origin = `${parsed.protocol}//${parsed.hostname}`;
    return ALLOWED_ORIGINS.has(origin);
  } catch {
    return false;
  }
}

/**
 * ログ出力に安全に使用できるよう文字列をサニタイズする。
 * ログインジェクションを防止するためASCII制御文字を除去する。
 * @param {string} str
 * @returns {string}
 */
function sanitiseForLog(str) {
  // タブと改行を除く制御文字 (U+0000-U+001F, U+007F) を除去する。
  return String(str).replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '');
}

// --- WebSocketライフサイクル ---

/**
 * ローカルサーバーへのWebSocket接続を開く。
 * 失敗時には指数バックオフを実装する。
 */
async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }

  // 並行するconnect()呼び出しを防ぐガード（例: 高速リトライのトリガー）。
  if (isConnecting) return;
  isConnecting = true;

  try {
    const port = await getPort();
    const url = `ws://127.0.0.1:${port}`;

    // セキュリティ: 接続前にオリジンを検証する。
    if (!isAllowedOrigin(url)) {
      console.error('[ChromeKontrol] Refused connection to non-localhost URL:', sanitiseForLog(url));
      isConnecting = false;
      return;
    }

    console.log(`[ChromeKontrol] Connecting to ${url} …`);
    ws = new WebSocket(url);

    ws.onopen = () => {
      console.log('[ChromeKontrol] Connected.');
      reconnectDelay = RECONNECT_BASE_MS;
      isConnecting = false;
      // 複数ブラウザ接続時にサーバーが正しいクライアントにコマンドを
      // ルーティングできるよう、即座にこのブラウザを識別させる。
      sendIdentify();
    };

    ws.onmessage = (event) => {
      handleServerMessage(event.data);
    };

    ws.onerror = (_err) => {
      // 機密性のあるURL情報を含む可能性があるため、生のエラーオブジェクトのログ出力を避ける。
      console.warn('[ChromeKontrol] WebSocket error occurred.');
      isConnecting = false;
    };

    ws.onclose = () => {
      console.log(`[ChromeKontrol] Disconnected. Reconnecting in ${reconnectDelay}ms …`);
      scheduleReconnect();
    };
  } catch (err) {
    console.error('[ChromeKontrol] connect() threw unexpectedly:', sanitiseForLog(String(err)));
    isConnecting = false;
  }
}

/** 指数バックオフで再接続を予約する。 */
function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    // 次回失敗時の遅延を増加させる。RECONNECT_MAX_MSを上限とする。
    reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
    connect();
  }, reconnectDelay);
}

/**
 * navigator.userAgentDataのブランド情報を検査して現在のブラウザを検出する。
 *
 * Chromium標準のUser-Agent Client Hints APIを使用する。ChromeとEdgeの両方が
 * このAPIを公開している。FirefoxやSafariでは利用できない。
 *
 * 検出ロジック:
 *   - brandsに "Microsoft Edge" が含まれる → "edge"
 *   - brandsに "Google Chrome" が含まれる → "chrome"
 *   - それ以外                             → "unknown"
 *
 * 注意: ブランドリストの順序には意図的に依存しない。ブラウザの変更に
 * 対して堅牢であるよう、メンバーシップチェックを使用している。
 *
 * @returns {string} ブラウザ名: "chrome", "edge", または "unknown"。
 */
function detectBrowser() {
  try {
    const brands = navigator.userAgentData && navigator.userAgentData.brands;
    if (Array.isArray(brands)) {
      const brandNames = brands.map((b) => b.brand || '');
      if (brandNames.includes('Microsoft Edge')) return 'edge';
      if (brandNames.includes('Google Chrome')) return 'chrome';
    }
  } catch {
    // 防御的対応: 予期しないエラーはunknownとして扱う。
  }
  return 'unknown';
}

/**
 * サーバーに識別メッセージを送信し、この接続を正しいブラウザ名で
 * 登録できるようにする。
 *
 * WebSocket接続が開いた直後に呼び出される。
 */
function sendIdentify() {
  const browser = detectBrowser();
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[ChromeKontrol] Cannot send identify: WebSocket not open.');
    return;
  }
  try {
    ws.send(JSON.stringify({ type: 'identify', browser }));
    console.log(`[ChromeKontrol] Identified as browser=${browser}`);
  } catch (err) {
    console.error('[ChromeKontrol] Failed to send identify:', sanitiseForLog(String(err)));
  }
}

/**
 * JSONレスポンスをサーバーに送信する。
 * @param {object} payload
 */
function sendResponse(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[ChromeKontrol] Cannot send: WebSocket not open.');
    return;
  }
  try {
    ws.send(JSON.stringify(payload));
  } catch (err) {
    console.error('[ChromeKontrol] Failed to send response:', sanitiseForLog(String(err)));
  }
}

// --- コマンドルーティング ---

/**
 * サーバーからの生JSONメッセージをパースしてディスパッチする。
 * セキュリティ注記: この境界ではサーバーからの入力をすべて信頼できないものとして扱い、
 * Content Scriptに転送する前に検証する。
 * @param {string} raw
 */
async function handleServerMessage(raw) {
  let msg;
  try {
    msg = JSON.parse(raw);
  } catch {
    console.warn('[ChromeKontrol] Received non-JSON message; discarding.');
    sendResponse({ result: 'error', message: 'Invalid JSON command.' });
    return;
  }

  // コマンドフィールドを検証する。
  const allowedCommands = new Set(['get_dom', 'click', 'get_elements']);
  if (!msg || typeof msg.cmd !== 'string' || !allowedCommands.has(msg.cmd)) {
    sendResponse({ result: 'error', message: 'Unknown or missing command.' });
    return;
  }

  // 必要な場合にselectorフィールドを検証する。
  if ((msg.cmd === 'click' || msg.cmd === 'get_elements') && typeof msg.selector !== 'string') {
    sendResponse({ result: 'error', message: 'Missing or invalid selector.' });
    return;
  }

  // 過度に長い文字列のインジェクションを防ぐためのselector長ガード。
  if (typeof msg.selector === 'string' && msg.selector.length > 512) {
    sendResponse({ result: 'error', message: 'Selector exceeds maximum length (512).' });
    return;
  }

  // 検証済みコマンドをアクティブタブにルーティングする。
  await forwardToActiveTab(msg);
}

/**
 * アクティブタブを検索し、scripting API経由で検証済みコマンドを転送する。
 * Content Scriptがすべてのページで動作しているとは限らないため（例: chrome:// URL）、
 * chrome.tabs.sendMessageではなくchrome.scripting.executeScriptを使用する。
 * @param {object} msg - 検証済みコマンドオブジェクト。
 */
async function forwardToActiveTab(msg) {
  let tabs;
  try {
    tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  } catch (err) {
    sendResponse({ result: 'error', message: 'Failed to query active tab.' });
    return;
  }

  if (!tabs || tabs.length === 0) {
    sendResponse({ result: 'error', message: 'No active tab found.' });
    return;
  }

  const tab = tabs[0];

  // ガード: chrome:// やその他の制限されたURLにはスクリプトを実行できない。
  if (!tab.url || !tab.url.startsWith('http')) {
    sendResponse({ result: 'error', message: 'Active tab URL is not scriptable (non-http).' });
    return;
  }

  try {
    // シリアライズされたコマンドをContent Scriptコンテキストにインジェクトする。
    // eval形式のインジェクションを避けるため、コマンドは関数の引数として渡す。
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: executeCommand,
      args: [msg],
    });

    if (!results || results.length === 0 || results[0] === undefined) {
      sendResponse({ result: 'error', message: 'Content script returned no result.' });
      return;
    }

    sendResponse(results[0].result);
  } catch (err) {
    console.error('[ChromeKontrol] executeScript error:', sanitiseForLog(String(err)));
    sendResponse({ result: 'error', message: 'Script execution failed.' });
  }
}

// --- インジェクションされる関数（ページコンテキストで実行） ---

/**
 * ターゲットページ内で検証済みコマンドを実行する。
 * この関数はchrome.scripting.executeScriptによってシリアライズ・インジェクトされる。
 * 自己完結している必要がある（background.jsスコープのクロージャは使えない）。
 *
 * 設計判断: content_scriptメッセージングではなくexecuteScriptに関数引数を渡す方式を
 * 採用することで、永続的なメッセージリスナーが不要になり、Service Workerのライフサイクルに
 * よってContent Scriptがアンロードされたページでも確実に動作する。
 *
 * @param {{ cmd: string, selector?: string }} msg
 * @returns {{ result: string, data?: string, message?: string }}
 */
function executeCommand(msg) {
  'use strict';

  /**
   * エラーメッセージに安全に使用できるよう文字列からASCII制御文字を除去する。
   * インラインで定義する必要がある -- この関数はページコンテキストで実行され、
   * background.jsスコープ（sanitiseForLogを含む）にアクセスできないため。
   * @param {string} str
   * @returns {string}
   */
  // eslint-disable-next-line no-unused-vars
  function sanitiseMsg(str) {
    return String(str).replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '');
  }

  /**
   * outerHTMLが大きすぎる場合にDOMの要約を構築する。
   * 構造的なヒントとして要素数と先頭N個のタグ名を提供する。
   * @param {Document} doc
   * @returns {string}
   */
  function buildDomSummary(doc) {
    const all = doc.querySelectorAll('*');
    const total = all.length;
    // 先頭200要素からユニークなタグ名を収集する。
    // Object.create(null)でプロトタイプ汚染を回避する（例: __proto__, constructor）。
    const tagCounts = Object.create(null);
    const sampleSize = Math.min(200, total);
    for (let i = 0; i < sampleSize; i++) {
      const tag = all[i].tagName.toLowerCase();
      tagCounts[tag] = (tagCounts[tag] || 0) + 1;
    }
    const tagSummary = Object.entries(tagCounts)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 15)
      .map(([tag, count]) => `${tag}(${count})`)
      .join(', ');
    return `[DOM summary: ${total} elements total. Top tags (sampled): ${tagSummary}]`;
  }

  /**
   * 切り詰め前のHTML最大長。
   * 500 KB（文字数）を実用的な上限として設定: 実際のほとんどのページをカバーしつつ、
   * IPCペイロードを管理可能な範囲に保つ。この制限を超えたコンテンツは
   * 削除され、プレーンテキストのDOM要約に置き換えられるため、
   * 切り詰められた部分が受信側でHTMLとして処理されることはない。
   */
  const MAX_HTML_LENGTH = 500_000; // 文字数（約500 KB）

  try {
    if (msg.cmd === 'get_dom') {
      const html = document.documentElement.outerHTML;
      if (html.length > MAX_HTML_LENGTH) {
        const summary = buildDomSummary(document);
        return {
          result: 'ok',
          data: html.slice(0, MAX_HTML_LENGTH) + '\n\n<!-- truncated -->\n\n' + summary,
        };
      }
      return { result: 'ok', data: html };
    }

    if (msg.cmd === 'click') {
      let el;
      try {
        el = document.querySelector(msg.selector);
      } catch {
        return { result: 'error', message: `Invalid selector: ${sanitiseMsg(msg.selector)}` };
      }
      if (!el) {
        return { result: 'error', message: `Element not found: ${sanitiseMsg(msg.selector)}` };
      }
      el.click();
      return { result: 'ok' };
    }

    if (msg.cmd === 'get_elements') {
      let elements;
      try {
        elements = document.querySelectorAll(msg.selector);
      } catch {
        return { result: 'error', message: `Invalid selector: ${sanitiseMsg(msg.selector)}` };
      }
      const items = Array.from(elements).map((el) => {
        const entry = {
          tag: el.tagName.toLowerCase(),
          text: (el.textContent || '').trim().slice(0, 200),
        };
        // セキュリティ注記: el.hrefは解決済みの絶対URLであり、クエリ文字列に
        // 認証トークンやセッション識別子が埋め込まれている可能性がある。
        // この値はそのまま呼び出し元に返される -- 適切な注意をもって取り扱うこと。
        if (el.href) entry.href = el.href;
        if (el.id) entry.id = el.id;
        const cls = el.className;
        if (typeof cls === 'string' && cls) entry.className = cls;
        return entry;
      });
      return { result: 'ok', data: items };
    }

    // background.jsの検証が正しければ到達しない。
    return { result: 'error', message: 'Unhandled command in content context.' };
  } catch (err) {
    // 生のエラー詳細を呼び出し元に公開しない。
    return { result: 'error', message: 'Internal error during command execution.' };
  }
}

// --- Keepalive (MV3 Service Worker) ---

/**
 * Service Workerを生存させ続けるための定期アラームを登録する。
 *
 * 設計メモ: MV3のService Workerは約30秒の非アクティブ状態で終了される。
 * chrome.alarmsはService Workerがアイドル状態でも発火し、Service Workerを
 * 起動させてWebSocket接続と再接続ロジックを実行できるようにする。
 * アラームは起動のたびに再作成されるため、ブラウザの再起動をまたいでも
 * 重複アラームを蓄積することなく持続する。
 */
function registerKeepaliveAlarm() {
  chrome.alarms.create(KEEPALIVE_ALARM_NAME, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
}

/**
 * Keepaliveアラームのティックを処理する。
 * WebSocketが現在開いていない、または接続中でない場合に再接続する。
 * @param {chrome.alarms.Alarm} alarm
 */
function onAlarm(alarm) {
  if (alarm.name !== KEEPALIVE_ALARM_NAME) return;
  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
    connect();
  }
}

chrome.alarms.onAlarm.addListener(onAlarm);

// --- エントリーポイント ---

// Service Worker起動時に接続を開始し、Keepaliveアラームを登録する。
connect();
registerKeepaliveAlarm();
