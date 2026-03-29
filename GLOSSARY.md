# ChromeKontrol 用語集

本プロジェクトで使用する技術用語・概念の一覧。
コードリーディングの補助資料として使用。

updated: 2026-03-29

## 基本用語

| 用語 | 説明 |
| --- | --- |
| CDP（Chrome DevTools Protocol） | Puppeteer / Playwright が Chrome 制御に使用する低レベルプロトコル。`--remote-debugging-port` 付きで起動が必要。自動化バナー表示 + `navigator.webdriver = true` がセットされ、Anti-bot の検知対象になる。ChromeKontrol は CDP を使用しない |
| MV3（Manifest V3） | 現行の Chrome 拡張機能プラットフォーム。バックグラウンドページ廃止、約30秒で停止する Service Worker に置換。ChromeKontrol は `chrome.alarms` + サーバー側 ping で維持 |
| Service Worker | MV3 拡張機能のバックグラウンドスクリプト。イベント駆動で DOM アクセス不可、メモリ上の永続状態なし。`background.js` がこれに該当 |
| コンテンツスクリプト | 拡張機能から Web ページに注入されるスクリプト。ページ JS とは隔離された Isolated World で動作。ChromeKontrol の `content.js` は意図的に空 |

## Chrome Extension API

| API | 説明 |
| --- | --- |
| `chrome.scripting.executeScript` | タブのページコンテキストに関数を注入する API。デバッグモード不要、画面上のインジケーターなし。ChromeKontrol の DOM アクセスの中核 |
| `chrome.debugger` | タブに DevTools デバッガーをアタッチする API。「この拡張機能はブラウザのデバッグを開始しました」通知バーが表示され、Web サイトから検知可能。ChromeKontrol は一切使用しない |
| `chrome.alarms` | 定期タイマー API。Service Worker が停止しても発火するため、キープアライブに利用（30秒周期） |
| `chrome.storage.local` | 拡張機能ローカルストレージ。WebSocket ポート番号の永続化に使用 |

## 動作モード

| モード | 説明 |
| --- | --- |
| ワンショットモード | デフォルト。stdin から JSON コマンドを1つ読み取り、拡張機能に転送、stdout にレスポンス出力して終了 |
| サーブモード（`--serve`） | 常駐動作。HTTP API（デフォルト 9766）で POST 受付。起動レイテンシなしで連続コマンド実行に対応 |

## プロトコル

| 用語 | 説明 |
| --- | --- |
| Identify ハンドシェイク | 接続時の初期メッセージ交換。拡張機能が WebSocket 接続直後に `{"type": "identify", "browser": "chrome"}` を送信し、サーバーがブラウザ名で接続を登録 |
| マルチブラウザルーティング | 単一サーバーから Chrome / Edge を同時制御。コマンドの `"browser"` フィールドで対象指定。1接続時は自動選択 |

## コマンド一覧

| コマンド | フィールド | 説明 |
| --- | --- | --- |
| `get_dom` | `cmd` | アクティブタブの `outerHTML` 全体を返す。500KB超で切り詰め + DOM要約付加 |
| `click` | `cmd`, `selector` | CSS セレクタに一致する最初の要素をクリック |
| `get_elements` | `cmd`, `selector` | 一致する要素の情報（tag, text, href, id, className）を配列で返す |

## ファイル構成

| ファイル | 役割 |
| --- | --- |
| `server.py` | WebSocket / HTTP サーバー（Python）。ワンショット / サーブ両対応 |
| `background.js` | MV3 Service Worker。WebSocket 中継 + コマンドルーティング + DOM操作注入 |
| `content.js` | コンテンツスクリプト（意図的に空。DOM操作は `executeScript` で注入） |
| `manifest.json` | 拡張機能マニフェスト（MV3） |
| `requirements.txt` | Python 依存パッケージ（`websockets`、ハッシュ固定） |

## セキュリティ対策

| 項目 | 実装 |
| --- | --- |
| ネットワーク分離 | WebSocket / HTTP ともに `127.0.0.1` にのみバインド |
| Origin 検証 | WebSocket ハンドシェイク時に localhost 限定ホワイトリストで検証 |
| コマンドホワイトリスト | `get_dom` / `click` / `get_elements` のみ受付 |
| セレクタ長制限 | CSS セレクタ 512 文字上限 |
| ブラウザ名制限 | `chrome` / `edge` / `unknown` のホワイトリストで制限 |
| メッセージサイズ制限 | 受信 5 MiB 上限でメモリ枯渇防止（GHSA-6g87-ff9q-v847 対策） |
| ログインジェクション防止 | ASCII / Unicode 制御文字を除去してログ出力 |
| コードインジェクション防止 | コマンド引数は関数パラメータとして渡し、文字列結合を回避 |
| HTTP ヘッダー制限 | 8 KiB 上限 + 10秒デッドライン |
| 並行リクエスト直列化 | `asyncio.Lock` でレスポンス混入を防止 |
| レスポンスヘッダー | `Cache-Control: no-store` + `X-Content-Type-Options: nosniff` |
