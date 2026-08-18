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
 *   WebExtension API (storage, tabs, scripting等)。実行環境で解決される
 *   名前空間参照 `api`（Chrome/Edgeでは`chrome`、Firefoxでは`browser`）
 *   経由で呼び出す。詳細は下記の `const api = ...` の定義を参照。
 *   および標準WebSocketインターフェース。サードパーティライブラリは読み込まない。
 */

'use strict';

// --- 定数 ---

/**
 * WebExtension名前空間の解決。
 *
 * MDNの明言: 「移植を助けるため、FirefoxのWebExtensions実装はコールバックを
 * 使うchromeと、Promiseを使うbrowserの両方をサポートする」
 * （原文: "As a porting aid, the Firefox implementation of WebExtensions
 * supports `chrome` using callbacks and `browser` using promises."）。
 *
 * つまりFirefoxのchrome.*はコールバック形式のみでPromiseを返さない。
 * 一方、Firefoxのbrowser.*とChrome MV3のchrome.*はどちらもPromiseを返す。
 * そのため実行環境に存在する方（Firefoxならbrowser、Chrome/Edgeならchrome）を
 * 一度だけ選び、以降はこの参照（api）のみを使うことで、同一のコードが
 * Chrome / Edge / Firefoxの3ブラウザで動作する。
 *
 * `browser.runtime`の存在まで確認する理由: Chromeには`browser`という
 * グローバルは存在しないが、将来何らかのスクリプトが`browser`という名前の
 * グローバルを誤って定義した場合に誤検出しないため、WebExtension APIで
 * あることを`runtime`の有無で確認する。
 */
const api = typeof browser !== 'undefined' && browser.runtime ? browser : chrome;

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

/** api.storage.localに永続化するprofileIdのキー名（ISSUES.md P0-1, Phase 3a）。
 *  値が存在しなければcrypto.randomUUID()で生成しこのキーに保存する。 */
const PROFILE_ID_STORAGE_KEY = 'ck_profile_id';

/** api.storage.localから読み込むlabelのキー名（ISSUES.md P0-1, Phase 3a）。
 *  設定UIはPhase 3cで実装する。Phase 3aでは読み出しのみ行う。 */
const LABEL_STORAGE_KEY = 'ck_label';

/** api.storage.localに永続化する最終フォーカス時刻のキー名
 *  （ISSUES.md P1-5, Phase F7）。onWindowFocusChanged()がフォーカス取得の
 *  たびに書き込む。Service Workerが停止していた区間に発生したイベントは
 *  そもそも記録できないため取りこぼすが、記録済みでWebSocketが切れていた
 *  だけのケースでは、再接続後のidentifyでこの値をfocusTsとして送り直せる。 */
const FOCUS_TS_STORAGE_KEY = 'ck_last_focus_ts';

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

/**
 * 直前にサーバーへ通知した（またはWINDOW_ID_NONEとして無視した以外で
 * 最後に処理対象とした）ウィンドウID（ISSUES.md P1-5, Phase F7）。
 * Firefoxでは1回のフォーカス変更でonFocusChangedが複数回発火するため
 * （MDN Chrome incompatibilities）、同一ウィンドウIDへの連続通知を
 * 抑制する目的で保持する。
 * @type {number|null}
 */
let lastFocusedWindowId = null;

// --- ヘルパー ---

/**
 * api.storage.localから設定済みのWebSocketポートを返す。
 * 取得できない場合はDEFAULT_WS_PORTにフォールバックする。
 *
 * エラー方針: 既存はコールバック形式でlastError（実行時のエラー通知）を
 * 検査せず、値が妥当でなければ（エラー時を含め）常にDEFAULT_WS_PORTで
 * resolveしていた（rejectしない設計）。Promise形式ではapi.storage.local.get()
 * がrejectしうるため、同じ挙動を保つためにtry/catchで捕捉し、エラー時も
 * 例外を投げずDEFAULT_WS_PORTを返す。
 * @returns {Promise<number>}
 */
async function getPort() {
  try {
    const result = await api.storage.local.get(['ws_port']);
    const port = result.ws_port;
    if (typeof port === 'number' && port > 0 && port <= 65535) {
      return port;
    }
  } catch {
    // storageの読み出しに失敗してもデフォルトポートで続行する（既存の挙動を維持）。
  }
  return DEFAULT_WS_PORT;
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

// --- プロファイル識別 (ISSUES.md P0-1, Phase 3a) ---

/**
 * api.storage.localから単一のキーの値を取得する。
 * api自体がPromiseを返すため、コールバック→Promise変換は不要（Chrome公式
 * ドキュメント: async/awaitを使うとエラー時はPromiseがrejectされる）。
 * @param {string} key
 * @returns {Promise<*>} 値。キーが存在しない場合はundefined。
 * @throws {Error} api.storage.local.get()が失敗した場合（Promiseがrejectされる）。
 */
async function storageGet(key) {
  const result = await api.storage.local.get([key]);
  return result[key];
}

/**
 * api.storage.localに単一のキーの値を保存する。
 * @param {string} key
 * @param {*} value
 * @returns {Promise<void>}
 * @throws {Error} api.storage.local.set()が失敗した場合（Promiseがrejectされる）。
 */
async function storageSet(key, value) {
  await api.storage.local.set({ [key]: value });
}

/**
 * api.storage.localからprofileIdを読み込む。存在しなければ
 * crypto.randomUUID()で生成し永続化する。
 *
 * 競合について: 理論上、読み出しと生成保存の間に別のService Workerインスタンスが
 * 同じキーへ書き込む競合がありえる。ただしService Workerは1プロファイルにつき
 * 1つしか同時起動しないため、実際にはこの競合は発生しない。仮に発生しても
 * 実害は「識別子が1回だけ変わる」だけであり、Web Locks API等による排他制御を
 * 追加するコストに見合わないと判断し、あえて入れていない。
 *
 * api.storage.local.get()自体が失敗した場合は既存の値の有無を確認できない
 * ため、安全側に倒して新規生成もせずprofileIdなしで続行する
 * （identify全体を諦めることはしない。呼び出し元でフィールドを省略するだけ）。
 *
 * @returns {Promise<string|null>} profileId。取得・生成のいずれかが失敗した場合はnull。
 */
async function getOrCreateProfileId() {
  let existing;
  try {
    existing = await storageGet(PROFILE_ID_STORAGE_KEY);
  } catch (err) {
    console.warn('[ChromeKontrol] Failed to read profileId from storage.local:', sanitiseForLog(String(err)));
    return null;
  }

  if (typeof existing === 'string' && existing) {
    return existing;
  }

  try {
    const generated = crypto.randomUUID();
    await storageSet(PROFILE_ID_STORAGE_KEY, generated);
    return generated;
  } catch (err) {
    // crypto.randomUUID()が使えない環境、またはstorage.set()の失敗。
    // いずれの場合もprofileIdフィールドを省略して続行する。
    console.warn('[ChromeKontrol] Failed to generate/persist profileId:', sanitiseForLog(String(err)));
    return null;
  }
}

/**
 * api.storage.localからlabelを読み込む。
 * キーが存在しない・値が文字列でない・空文字列・空白のみのいずれの場合も
 * nullを返す（サーバー側が空白のみのlabelを拒否するため、この時点で
 * 送信対象から外す）。設定UIはPhase 3cで実装するため、Phase 3aでは
 * 読み出しのみを行う。
 * @returns {Promise<string|null>}
 */
async function getLabel() {
  let value;
  try {
    value = await storageGet(LABEL_STORAGE_KEY);
  } catch (err) {
    console.warn('[ChromeKontrol] Failed to read label from storage.local:', sanitiseForLog(String(err)));
    return null;
  }
  if (typeof value !== 'string' || !value.trim()) {
    return null;
  }
  return value;
}

/**
 * api.identity.getProfileUserInfo()をPromiseベースで呼び出す。
 * async functionで包むことで、api呼び出し自体が同期的にthrowした場合も
 * （コールバック版がtry/catchで常にPromiseを返していたのと同様に）
 * reject済みのPromiseとして呼び出し元に伝わることを保証する。
 *
 * accountStatus: 'ANY' を指定する。デフォルトの'SYNC'はChrome同期が有効な
 * 場合のみメールアドレスを返すため、同期を使っていないプロファイルでは
 * 常に空文字列になってしまう。本来の目的は「このChromeプロファイルにどの
 * アカウントが紐付いているか」を識別することなので、同期状態に関わらず
 * 取得できる'ANY'の方が適している。
 *
 * @returns {Promise<{email: string, id: string}>}
 */
async function getProfileUserInfoAsync() {
  return api.identity.getProfileUserInfo({ accountStatus: 'ANY' });
}

/**
 * 現在のChromeプロファイルに紐付くアカウントのメールアドレスを取得する。
 *
 * api.identity自体が存在しない環境（例: 一部のChromiumベースブラウザ、
 * およびgetProfileUserInfo未対応のFirefox）や権限不足で例外が発生する
 * 場合に備え、存在チェックとtry/catchの両方を行う。未ログイン（プロファイル
 * にアカウントが紐付いていない）状態ではemailが空文字列で返る。空文字列を
 * そのまま送るとサーバー側のemail形式検証で接続が拒否されるため、その場合も
 * nullを返しフィールド自体を省略する。
 *
 * @returns {Promise<string|null>}
 */
async function getEmail() {
  if (!api.identity || typeof api.identity.getProfileUserInfo !== 'function') {
    return null;
  }
  try {
    const info = await getProfileUserInfoAsync();
    const email = info && typeof info.email === 'string' ? info.email : '';
    return email || null;
  } catch (err) {
    console.warn('[ChromeKontrol] Failed to get profile email via api.identity:', sanitiseForLog(String(err)));
    return null;
  }
}

/**
 * 最終フォーカス時刻を取得する（ISSUES.md P1-5, Phase F7）。
 *
 * identifyペイロードに含める"focusTs"フィールド用。優先順位:
 *   1. api.storage.localに永続化された記録（onWindowFocusChanged()が
 *      フォーカス取得のたびに書き込んだもの）があればそれを返す。
 *   2. 記録がなければ api.windows.getLastFocused() を問い合わせ、
 *      focused === true（このプロファイルの何らかのウィンドウが現在
 *      フォーカスされている）ならその場でDate.now()を返す。拡張の
 *      ロード直後でまだonFocusChangedが一度も発火していない場合の
 *      初期値として使う。
 *   3. どちらも取得できなければnullを返す。
 *
 * 全体をtry/catchで囲み、いずれかの取得が失敗しても例外を投げずnullを
 * 返す（getEmail()と同じ防御的な作り。identify全体を諦めさせない）。
 *
 * @returns {Promise<number|null>}
 */
async function getFocusTs() {
  try {
    const stored = await storageGet(FOCUS_TS_STORAGE_KEY);
    if (typeof stored === 'number' && stored > 0) {
      return stored;
    }
    const win = await api.windows.getLastFocused();
    if (win && win.focused === true) {
      return Date.now();
    }
    return null;
  } catch (err) {
    console.warn('[ChromeKontrol] Failed to get focus timestamp:', sanitiseForLog(String(err)));
    return null;
  }
}

/**
 * identifyメッセージのペイロードを構築する。
 *
 * browserは常に含める。profileId / email / label / focusTsは取得できた
 * 場合のみ含め、いずれかの取得に失敗しても全体を諦めず、取得できた
 * フィールドのみで続行する（最低限browserだけは常に送れる。
 * ISSUES.md P0-1の識別子3層設計。focusTsはISSUES.md P1-5, Phase F7）。
 *
 * @returns {Promise<object>}
 */
async function buildIdentifyPayload() {
  const payload = { type: 'identify', browser: detectBrowser() };

  const profileId = await getOrCreateProfileId();
  if (profileId) payload.profileId = profileId;

  const email = await getEmail();
  if (email) payload.email = email;

  const label = await getLabel();
  if (label) payload.label = label;

  const focusTs = await getFocusTs();
  if (focusTs) payload.focusTs = focusTs;

  return payload;
}

/**
 * identifyペイロードのキャッシュ。Service Workerの生存期間中のみ有効。
 * @type {object|null}
 *
 * サーバーは接続後3秒以内にidentifyを受け取らないと切断する
 * (server.pyの_IDENTIFY_TIMEOUT = 3.0)。api.storage.localの読み出しと
 * api.identity.getProfileUserInfo()はいずれも非同期のため、WebSocket
 * 接続後（ws.onopen内）にこれらを呼び始めると、I/Oの遅延次第でタイムアウトに
 * 間に合わないリスクがある。そのため接続前にペイロードを構築し終えてここに
 * キャッシュし、ws.onopenでは同期的にキャッシュを送信するだけにする。
 *
 * Service Workerが停止・再起動されるとこの変数もリセットされ再構築されるが、
 * profileIdはapi.storage.localに永続化されているため同じ値になる。
 *
 * focusTsについての注記（ISSUES.md P1-5, Phase F7）: このキャッシュは
 * Service Workerの生存期間中のみ有効なため、再接続時に古いfocusTsが
 * 送られる可能性がある（例: 再接続の直前にフォーカスが変わっていても、
 * キャッシュ済みのidentifyペイロードにはその変化が反映されない）。ただし
 * onWindowFocusChanged()によるfocus通知が接続確立後に別途飛ぶため実害は
 * 小さい。P2項目として設計者が記録する想定（このコメントはその根拠）。
 */
let cachedIdentifyPayload = null;

/**
 * キャッシュ済みのidentifyペイロードを返す。未構築ならこの場で構築する。
 * @returns {Promise<object>}
 */
async function ensureIdentifyPayload() {
  if (cachedIdentifyPayload) return cachedIdentifyPayload;
  cachedIdentifyPayload = await buildIdentifyPayload();
  return cachedIdentifyPayload;
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
    // ISSUES.md P0-1 (Phase 3a): identifyペイロードをWebSocket接続前に構築・
    // キャッシュする。理由はcachedIdentifyPayloadのJSDoc参照
    // （api.storage.local / api.identity は非同期だが、サーバーは
    // 接続後3秒以内にidentifyを受け取らないと切断するため）。
    const identifyPayload = await ensureIdentifyPayload();

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
      // 複数プロファイル接続時にサーバーが正しいクライアントにコマンドを
      // ルーティングできるよう、即座にこのプロファイルを識別させる。
      // ペイロードは接続前に構築済みのため、ここでは同期的に送信するのみ。
      sendIdentify(identifyPayload);
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
 * navigator.userAgentDataのブランド情報、またはnavigator.userAgentの
 * 文字列を検査して現在のブラウザを検出する。
 *
 * 検出は2段階で行う:
 *   1段目: navigator.userAgentDataのbrandsを検査する。Chromium標準の
 *     User-Agent Client Hints APIであり、ChromeとEdgeの両方がこのAPIを
 *     公開しているが、FirefoxやSafariでは利用できない。
 *       - brandsに "Microsoft Edge" が含まれる → "edge"
 *       - brandsに "Google Chrome" が含まれる → "chrome"
 *   2段目: 1段目に該当しない場合のフォールバック。userAgentDataを
 *     実装していないFirefox向けに、navigator.userAgent文字列に
 *     "Firefox/" が含まれるかを見る。
 *       - 含まれる → "firefox"
 *   どちらにも該当しなければ → "unknown"
 *
 * 注意: ブランドリストの順序には意図的に依存しない。ブラウザの変更に
 * 対して堅牢であるよう、メンバーシップチェックを使用している。
 *
 * @returns {string} ブラウザ名: "chrome", "edge", "firefox", または "unknown"。
 */
function detectBrowser() {
  try {
    const brands = navigator.userAgentData && navigator.userAgentData.brands;
    if (Array.isArray(brands)) {
      const brandNames = brands.map((b) => b.brand || '');
      if (brandNames.includes('Microsoft Edge')) return 'edge';
      if (brandNames.includes('Google Chrome')) return 'chrome';
    }
    if (typeof navigator.userAgent === 'string' && navigator.userAgent.includes('Firefox/')) {
      return 'firefox';
    }
  } catch {
    // 防御的対応: 予期しないエラーはunknownとして扱う。
  }
  return 'unknown';
}

/**
 * サーバーにidentifyペイロードを送信し、この接続を正しいプロファイルとして
 * 登録できるようにする。
 *
 * ペイロードはconnect()内で接続前に構築済み（ensureIdentifyPayload()）の
 * ものを受け取るだけで、ここでは非同期処理を一切行わない。WebSocket接続が
 * 開いた直後に呼び出される。
 *
 * @param {object} payload - あらかじめ構築済みのidentifyメッセージ。
 */
function sendIdentify(payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    console.warn('[ChromeKontrol] Cannot send identify: WebSocket not open.');
    return;
  }
  try {
    ws.send(JSON.stringify(payload));
    // email/labelの値そのものはログに出さない（プライバシー配慮）。
    // どのフィールドを含めて送ったかのみを記録する。
    console.log(
      `[ChromeKontrol] Identified as browser=${payload.browser} ` +
        `(profileId=${payload.profileId ? 'yes' : 'no'}, ` +
        `email=${payload.email ? 'yes' : 'no'}, ` +
        `label=${payload.label ? 'yes' : 'no'})`
    );
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
  const allowedCommands = new Set(['get_dom', 'click', 'get_elements', 'list_tabs']);
  if (!msg || typeof msg.cmd !== 'string' || !allowedCommands.has(msg.cmd)) {
    sendResponse({ result: 'error', message: 'Unknown or missing command.' });
    return;
  }

  // list_tabsコマンド: background.jsで完結する（タブへの転送不要）。
  if (msg.cmd === 'list_tabs') {
    try {
      const allTabs = await api.tabs.query({});
      const tabList = allTabs.map((tab) => ({
        id: tab.id,
        url: tab.url || '',
        title: tab.title || '',
        active: tab.active,
      }));
      sendResponse({ result: 'ok', data: tabList });
    } catch (err) {
      console.error('[ChromeKontrol] list_tabs error:', sanitiseForLog(String(err)));
      sendResponse({ result: 'error', message: 'Failed to list tabs.' });
    }
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

  // オプションのtabIdフィールドを検証する。
  // 指定されていれば数値であること、正の整数であることを確認する。
  if (msg.tabId !== undefined) {
    if (typeof msg.tabId !== 'number' || !Number.isInteger(msg.tabId) || msg.tabId < 0) {
      sendResponse({ result: 'error', message: 'Invalid tabId: must be a non-negative integer.' });
      return;
    }
  }

  // 検証済みコマンドを対象タブにルーティングする。
  await forwardToActiveTab(msg);
}

/**
 * 対象タブを特定し、scripting API経由で検証済みコマンドを転送する。
 * msg.tabIdが指定されていればそのタブIDに直接実行し、未指定ならアクティブタブにフォールバックする。
 * Content Scriptがすべてのページで動作しているとは限らないため（例: chrome:// URL）、
 * api.tabs.sendMessageではなくapi.scripting.executeScriptを使用する。
 * @param {object} msg - 検証済みコマンドオブジェクト。tabIdフィールドはオプション。
 */
async function forwardToActiveTab(msg) {
  let tab;

  if (msg.tabId !== undefined) {
    // tabId指定: 直接そのタブを取得する（アクティブタブクエリをスキップ）。
    try {
      tab = await api.tabs.get(msg.tabId);
    } catch (err) {
      sendResponse({ result: 'error', message: `Tab not found: tabId=${msg.tabId}` });
      return;
    }
  } else {
    // tabId未指定: フォーカス中のウィンドウのアクティブタブを選ぶ。
    //
    // ISSUES.md P1-2: `chrome.tabs.query({ currentWindow: true })` の
    // currentWindowは「この拡張のコードが実行されているウィンドウ」を意味するが、
    // Service Workerはどのウィンドウにも属さないため定義が曖昧になる。実装上は
    // 最後にフォーカスされたウィンドウにフォールバックする挙動が観測できるが、
    // これは仕様として保証された動作ではない。
    //
    // 代わりにgetLastFocused()でウィンドウを明示的に取得し、その中のactiveな
    // タブを選ぶことで意図を明確にする。populate: trueでtabs配列を含め、
    // windowTypes: ['normal']で通常のブラウザウィンドウに限定する
    // （DevToolsやポップアップ等の対象外ウィンドウを除外）。
    //
    // 取得に失敗した場合、または該当するアクティブタブが見つからない場合は、
    // 従来のクエリにフォールバックする。
    let focusedTab;
    try {
      const win = await api.windows.getLastFocused({ populate: true, windowTypes: ['normal'] });
      focusedTab = Array.isArray(win && win.tabs) ? win.tabs.find((t) => t.active) : undefined;
    } catch (err) {
      console.warn(
        '[ChromeKontrol] getLastFocused failed, falling back to currentWindow query:',
        sanitiseForLog(String(err))
      );
    }

    if (focusedTab) {
      tab = focusedTab;
    } else {
      // フォールバック: 従来のクエリ。
      let tabs;
      try {
        tabs = await api.tabs.query({ active: true, currentWindow: true });
      } catch (err) {
        sendResponse({ result: 'error', message: 'Failed to query active tab.' });
        return;
      }

      if (!tabs || tabs.length === 0) {
        sendResponse({ result: 'error', message: 'No active tab found.' });
        return;
      }

      tab = tabs[0];
    }
  }

  // ガード: chrome:// やその他の制限されたURLにはスクリプトを実行できない。
  if (!tab.url || !tab.url.startsWith('http')) {
    sendResponse({ result: 'error', message: 'Target tab URL is not scriptable (non-http).' });
    return;
  }

  try {
    // シリアライズされたコマンドをContent Scriptコンテキストにインジェクトする。
    // eval形式のインジェクションを避けるため、コマンドは関数の引数として渡す。
    const results = await api.scripting.executeScript({
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
 * この関数はapi.scripting.executeScriptによってシリアライズ・インジェクトされる。
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
 * api.alarmsはService Workerがアイドル状態でも発火し、Service Workerを
 * 起動させてWebSocket接続と再接続ロジックを実行できるようにする。
 * アラームは起動のたびに再作成されるため、ブラウザの再起動をまたいでも
 * 重複アラームを蓄積することなく持続する。
 */
function registerKeepaliveAlarm() {
  api.alarms.create(KEEPALIVE_ALARM_NAME, { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
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

// --- フォーカス追跡 (ISSUES.md P1-5, Phase F7) ---

/**
 * ウィンドウのフォーカス変更イベントを処理する。
 *
 * サーバー側が「今どのクライアント（プロファイル）がアクティブか」を
 * 判断するための材料として、このクライアントがフォーカスを得た時刻を
 * 記録・通知する。2つの裏取り済みの挙動に対応する:
 *
 *   1. 一部のLinuxウィンドウマネージャでは、Chromeウィンドウ間の切り替え
 *      でもWINDOW_ID_NONEが直前に送られる（Chrome公式ドキュメント
 *      `chrome.windows` API）。これをフォーカス喪失として記録して
 *      しまうと、ウィンドウ切り替えのたびに記録が更新され、正しい
 *      フォーカス順序が壊れる。そのためWINDOW_ID_NONEは無視し、記録を
 *      更新しない。
 *   2. Firefoxでは1回のフォーカス変更でonFocusChangedが複数回発火する
 *      （MDN Chrome incompatibilities）。同一ウィンドウIDへの連続通知は
 *      抑制し、サーバーへの重複送信を防ぐ（タイムスタンプの上書き自体は
 *      冪等だが、通知の二重送信を避ける）。
 *
 * @param {number} windowId - フォーカスを得たウィンドウのID。
 */
function onWindowFocusChanged(windowId) {
  // WINDOW_ID_NONEのフォールバック比較: api.windows.WINDOW_ID_NONEは定数
  // として存在するはずだが、モックやブラウザによって未定義になる可能性を
  // 考慮し、既知の値である-1との比較も併用する。
  if (windowId === -1 || windowId === api.windows.WINDOW_ID_NONE) return;

  // Firefoxの重複発火対策: 同一ウィンドウIDへの連続通知は無視する。
  if (windowId === lastFocusedWindowId) return;
  lastFocusedWindowId = windowId;

  const ts = Date.now();

  // storageへの永続化は失敗しても続行する（Service Worker停止区間の
  // 取りこぼしとは別に、書き込み自体の失敗でこの通知処理全体を止めない）。
  storageSet(FOCUS_TS_STORAGE_KEY, ts).catch((err) => {
    console.warn('[ChromeKontrol] Failed to persist focus timestamp:', sanitiseForLog(String(err)));
  });

  // WebSocketが開いていなければsendResponse()内部のガードが送信をスキップ
  // する（storageへの記録は上記で既に完了しているため、送信の成否とは
  // 独立している）。
  sendResponse({ type: 'focus', ts });
}

api.alarms.onAlarm.addListener(onAlarm);
api.windows.onFocusChanged.addListener(onWindowFocusChanged);

// --- エントリーポイント ---

// Service Worker起動時に接続を開始し、Keepaliveアラームを登録する。
connect();
registerKeepaliveAlarm();
