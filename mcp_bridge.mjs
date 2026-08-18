#!/usr/bin/env node
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join } from "node:path";

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

// ISSUES.md P0-2: トークンはキャッシュせず、コマンド送信のたびにファイル/環境変数から
// 読み直す。これにより並行リクエスト間の同期機構が不要になり、サーバー再起動による
// トークンローテーション後も401リトライで自動追従できる。
const HTTP_PORT = parseInt(process.env.CHROME_KONTROL_HTTP_PORT || "9766", 10);
const BASE_URL = `http://127.0.0.1:${HTTP_PORT}/`;
const DEFAULT_TOKEN_FILE = join(homedir(), ".config", "chromekontrol", "token");

/**
 * トークンファイルの解決先パスを返す。
 * CHROME_KONTROL_TOKEN_FILE環境変数が設定されていればそれを優先する
 * （server.py側の同名環境変数と同じ挙動。テスト用途）。
 */
function tokenFilePath() {
  const override = process.env.CHROME_KONTROL_TOKEN_FILE;
  return override && override.trim() !== "" ? override : DEFAULT_TOKEN_FILE;
}

/**
 * 認証トークンを解決する。呼び出しごとに評価し、結果はキャッシュしない。
 *
 * 解決順序:
 *   1. 環境変数 CHROME_KONTROL_TOKEN（手動起動時の固定トークン運用を残すため優先）
 *   2. トークンファイル（server.pyが起動ごとに書き出す。パスはtokenFilePath()参照）
 *   3. どちらも見つからなければ null
 *
 * @returns {Promise<string | null>} 解決できたトークン、またはnull。
 */
async function readToken() {
  const envToken = process.env.CHROME_KONTROL_TOKEN;
  if (envToken && envToken.trim() !== "") {
    return envToken;
  }
  try {
    const content = await readFile(tokenFilePath(), "utf-8");
    const trimmed = content.trim();
    return trimmed !== "" ? trimmed : null;
  } catch {
    // ファイルが存在しない、または読み取り権限がない。トークンなしとして扱う
    // （呼び出し元がエラーメッセージにtokenFilePath()を含めて案内する）。
    return null;
  }
}

/**
 * ChromeKontrolサーバーへ単一のHTTPリクエストを送る（リトライなし）。
 *
 * @param {string} token - X-ChromeKontrol-Tokenヘッダーに使う値。
 * @param {Record<string, unknown>} cmd - 送信するコマンドオブジェクト。
 * @returns {Promise<Response>} fetchのレスポンス（呼び出し元がステータスを見る）。
 */
async function postCommand(token, cmd) {
  return fetch(BASE_URL, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-ChromeKontrol-Token": token,
    },
    body: JSON.stringify(cmd),
  });
}

/**
 * コマンドをChromeKontrolサーバーへ送信し、レスポンスのdataオブジェクトを返す。
 *
 * エラー時は常に `{ result: "error", message: string }` 形式のオブジェクトを返す
 * （サーバー側のエラーレスポンス形式と一貫させ、各ツールハンドラの
 * `data.result === "ok"` チェックがそのまま機能するようにする）。
 *
 * 戦略の使い分け（ISSUES.md P0-2）:
 *   - トークンが解決できない: fetchを試みず即座にエラーを返す。
 *   - fetch自体が失敗（サーバー未起動・接続拒否等）: リトライしない。
 *     トークンの問題ではなく接続自体の問題であり、読み直しても解決しないため。
 *   - HTTP 401（トークン不一致）: トークンを読み直して1回だけリトライする。
 *     サーバー再起動によるトークンローテーションを想定した唯一のリトライケース。
 *
 * @param {Record<string, unknown>} cmd - 送信するコマンドオブジェクト（cmd, selector等）。
 * @returns {Promise<Record<string, unknown>>} レスポンスdata、またはエラーオブジェクト。
 */
async function sendCommand(cmd) {
  const filePath = tokenFilePath();
  const token = await readToken();
  if (token === null) {
    return {
      result: "error",
      message:
        `No ChromeKontrol token available. Is the server running? ` +
        `Set $CHROME_KONTROL_TOKEN, or ensure the server can write to ${filePath}.`,
    };
  }

  let res;
  try {
    res = await postCommand(token, cmd);
  } catch {
    return {
      result: "error",
      message: "Could not connect to the ChromeKontrol server. Is it running? " +
        "(systemctl --user status chromekontrol)",
    };
  }

  if (res.status === 401) {
    // トークンがローテーションされた可能性がある（サーバー再起動）。読み直して1回だけ再試行する。
    const retriedToken = await readToken();
    if (retriedToken === null) {
      return {
        result: "error",
        message:
          `Authentication failed and no token is currently available. ` +
          `Expected a token at ${filePath} or in $CHROME_KONTROL_TOKEN.`,
      };
    }

    let retryRes;
    try {
      retryRes = await postCommand(retriedToken, cmd);
    } catch {
      return {
        result: "error",
        message: "Could not connect to the ChromeKontrol server on retry. Is it running?",
      };
    }

    if (retryRes.status === 401) {
      return {
        result: "error",
        message:
          "Authentication failed. The server may have restarted with a new token; " +
          "the automatic retry did not resolve it. Check that " +
          `${filePath} is current.`,
      };
    }
    return retryRes.json();
  }

  return res.json();
}

const server = new McpServer({
  name: "chromekontrol",
  version: "1.0.0",
});

// server.py の ALLOWED_BROWSERS（server.py:183）と値を一致させること。
// ブラウザが追加された場合はここも更新が必要（二重管理。ISSUES.md 到達目標
// 「MCPからの可視性」Phase F5で許容と判断済み。頻繁な変更ではないため）。
const BROWSER_ENUM = ["chrome", "edge", "firefox"];

const browserParam = z.enum(BROWSER_ENUM).optional().describe("Target browser");

// max(256) は server.py の MAX_TARGET_LENGTH（server.py:188）に合わせる。
// "browser" との排他制約はここでは表現しない（Zodの refine で弾くと
// Zod由来の汎用メッセージに置き換わり、server.py側の明示的なエラー
// メッセージ（_validate_command、server.py:429-430）が呼び出し元に
// 届かなくなるため。排他チェックはサーバー側に一任し、ここでは
// description で注意書きするに留める）。
const targetParam = z
  .string()
  .max(256)
  .optional()
  .describe(
    'Alias or "browser:profileId" to select a specific browser profile. ' +
      'Cannot be combined with "browser". Use list_clients to see available names.'
  );

const LIST_TABS_DESCRIPTION = "List all open browser tabs";
const listTabsTool = server.registerTool(
  "list_tabs",
  {
    description: LIST_TABS_DESCRIPTION,
    inputSchema: { browser: browserParam, target: targetParam },
  },
  async ({ browser, target }) => {
    const cmd = { cmd: "list_tabs" };
    if (browser) cmd.browser = browser;
    if (target) cmd.target = target;
    const data = await sendCommand(cmd);
    return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
  }
);

const GET_DOM_DESCRIPTION = "Get the full DOM HTML of a tab";
const getDomTool = server.registerTool(
  "get_dom",
  {
    description: GET_DOM_DESCRIPTION,
    inputSchema: {
      tabId: z.number().int().nonnegative().optional().describe("Tab ID from list_tabs"),
      browser: browserParam,
      target: targetParam,
    },
  },
  async ({ tabId, browser, target }) => {
    const cmd = { cmd: "get_dom" };
    if (tabId !== undefined) cmd.tabId = tabId;
    if (browser) cmd.browser = browser;
    if (target) cmd.target = target;
    const data = await sendCommand(cmd);
    if (data.result === "ok" && typeof data.data === "string") {
      const text = data.data.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();
      return { content: [{ type: "text", text: text.slice(0, 50000) }] };
    }
    return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
  }
);

const GET_ELEMENTS_DESCRIPTION = "Query elements by CSS selector and return their text content";
const getElementsTool = server.registerTool(
  "get_elements",
  {
    description: GET_ELEMENTS_DESCRIPTION,
    inputSchema: {
      selector: z.string().max(512).describe("CSS selector"),
      tabId: z.number().int().nonnegative().optional().describe("Tab ID from list_tabs"),
      browser: browserParam,
      target: targetParam,
    },
  },
  async ({ selector, tabId, browser, target }) => {
    const cmd = { cmd: "get_elements", selector };
    if (tabId !== undefined) cmd.tabId = tabId;
    if (browser) cmd.browser = browser;
    if (target) cmd.target = target;
    const data = await sendCommand(cmd);
    return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
  }
);

const CLICK_DESCRIPTION = "Click an element matching a CSS selector";
const clickTool = server.registerTool(
  "click",
  {
    description: CLICK_DESCRIPTION,
    inputSchema: {
      selector: z.string().max(512).describe("CSS selector of the element to click"),
      tabId: z.number().int().nonnegative().optional().describe("Tab ID from list_tabs"),
      browser: browserParam,
      target: targetParam,
    },
  },
  async ({ selector, tabId, browser, target }) => {
    const cmd = { cmd: "click", selector };
    if (tabId !== undefined) cmd.tabId = tabId;
    if (browser) cmd.browser = browser;
    if (target) cmd.target = target;
    const data = await sendCommand(cmd);
    return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
  }
);

const GET_TEXT_DESCRIPTION = "Get clean text content of a tab (strips HTML tags)";
const getTextTool = server.registerTool(
  "get_text",
  {
    description: GET_TEXT_DESCRIPTION,
    inputSchema: {
      tabId: z.number().int().nonnegative().optional().describe("Tab ID from list_tabs"),
      browser: browserParam,
      target: targetParam,
    },
  },
  async ({ tabId, browser, target }) => {
    const cmd = { cmd: "get_dom" };
    if (tabId !== undefined) cmd.tabId = tabId;
    if (browser) cmd.browser = browser;
    if (target) cmd.target = target;
    const data = await sendCommand(cmd);
    if (data.result === "ok" && typeof data.data === "string") {
      const text = data.data
        .replace(/<script[\s\S]*?<\/script>/gi, "")
        .replace(/<style[\s\S]*?<\/style>/gi, "")
        .replace(/<[^>]*>/g, "\n")
        .replace(/&nbsp;/g, " ")
        .replace(/&amp;/g, "&")
        .replace(/&lt;/g, "<")
        .replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/\n{3,}/g, "\n\n")
        .replace(/[ \t]+/g, " ")
        .trim();
      return { content: [{ type: "text", text: text.slice(0, 50000) }] };
    }
    return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
  }
);

// list_clients はパラメータを取らない。config から inputSchema を省略する
// （InputArgs のデフォルト型引数が undefined のため、これが正しい形。
// 空オブジェクト {} を渡す必要はない。mcp.d.ts:150 参照）。
server.registerTool(
  "list_clients",
  {
    description:
      "List connected browser profiles and their assigned aliases. " +
      "Shows which names can be passed to other tools' `target` parameter.",
  },
  async () => {
    const data = await sendCommand({ cmd: "list_clients" });
    // MCP経由のレスポンスからのみemail（PII）を除去する。
    // HTTP API（server.py）側は後方互換のため無変更のまま。
    //
    // なぜ経路によって扱いを分けるか（Security-Guidelines.md S4/S15）:
    //   HTTP API（curl等）はターミナルで人間が一度見て終わる、揮発性の高い経路。
    //   MCP経由のレスポンスはLLMの入力として取り込まれ、Claude Codeの
    //   トランスクリプト・JSONL生ログ・会話履歴として永続化され、セッション
    //   共有時には第三者にも露出しうる。list_clientsの用途はtargetパラメータに
    //   渡せる名前（alias/key）を知ることであり、emailそのものは不要。
    if (data.result === "ok" && Array.isArray(data.data)) {
      for (const client of data.data) {
        // server.py の ClientInfo.display_name（server.py:527-540）は
        // label > email > profileId先頭8文字 > browser の優先順位で決まる。
        // labelが未設定でemailが設定されているクライアントは displayName が
        // emailそのものになる。email本体を消してもdisplayNameが素通りすると
        // PII漏洩の抜け穴になるため、ここでも同じ値を検出して代替値に置き換える
        // （フィールド自体を落とさないのは、target選択のUX——「どのクライアント
        // か一目でわかる」——を維持するため。server.py側の代替優先順位に揃える）。
        if (client.displayName && client.displayName === client.email) {
          client.displayName = client.profileId ? client.profileId.slice(0, 8) : client.browser;
        }
        delete client.email;
      }
    }
    return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
  }
);

// target/browser を受け付ける5ツール。起動時の applyClientHint() で
// description にエイリアス名のヒントを追記する対象（list_clients自体は対象外）。
const TOOLS_WITH_TARGET = [
  { tool: listTabsTool, baseDescription: LIST_TABS_DESCRIPTION },
  { tool: getDomTool, baseDescription: GET_DOM_DESCRIPTION },
  { tool: getElementsTool, baseDescription: GET_ELEMENTS_DESCRIPTION },
  { tool: clickTool, baseDescription: CLICK_DESCRIPTION },
  { tool: getTextTool, baseDescription: GET_TEXT_DESCRIPTION },
];

/**
 * 起動時に一度だけ list_clients を叩き、接続中クライアントに割り当てられた
 * エイリアス名を各ツールの description に追記する（ISSUES.md P0-4, Phase F5）。
 *
 * 失敗しても起動を止めない。ChromeKontrolサーバーが未起動でもMCPサーバー自体は
 * 立ち上がる必要があるため、sendCommand がエラーオブジェクトを返した場合や
 * レスポンス形式が想定と異なる場合は何もせず description は静的なままにする。
 * sendCommand自体は例外を投げない設計だが、res.json()のパース失敗等
 * 想定外の例外にも備えてtry/catchで囲む。
 *
 * エイリアスのみを列挙する。email・profileIdはdescriptionに含めない
 * （descriptionはMCPクライアント側のログに残るため、環境固有情報を最小化する）。
 *
 * リアルタイム更新は本フェーズのスコープ外（起動時1回のみ）。接続状況の
 * 変化は list_clients ツールでの随時確認、または曖昧な target 指定時に
 * サーバーが返す候補列挙エラー（Phase 2b実装済み）の2経路でカバーする。
 */
async function applyClientHint() {
  let data;
  try {
    data = await sendCommand({ cmd: "list_clients" });
  } catch {
    return;
  }
  if (data.result !== "ok" || !Array.isArray(data.data)) return;

  const names = new Set();
  for (const client of data.data) {
    for (const alias of client?.aliases ?? []) {
      if (typeof alias === "string") names.add(alias);
    }
  }
  if (names.size === 0) return;

  const hint = ` Available target names: ${[...names].sort().join(", ")}.`;
  for (const { tool, baseDescription } of TOOLS_WITH_TARGET) {
    // RegisteredTool.update() の引数フィールド名は paramsSchema
    // （registerTool の config は inputSchema）。SDK内で名前が違うので注意
    // （mcp.d.ts:150 の registerTool config vs mcp.d.ts:278 の update）。
    // 今回は description のみ更新するため実害はないが、将来スキーマを
    // 動的に変える場合はこの差異を踏まえること。
    tool.update({ description: baseDescription + hint });
  }
}

// server.connect() の前に await する。接続後だとクライアントの最初の
// tools/list より前に description 更新が間に合わない可能性があるため
// （検証済み: sendToolListChanged() は isConnected() でガードされており、
// 接続前に呼ばれても例外なく無視される。mcp.js:765-767 参照）。
await applyClientHint();

const transport = new StdioServerTransport();
await server.connect(transport);
