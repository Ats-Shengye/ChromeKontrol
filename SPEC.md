# ChromeKontrol 仕様書

## 概要

ChromeKontrol は、Chrome/Edge 拡張機能とローカル WebSocket サーバーを介して、CLIツールからユーザーのブラウザを操作する軽量ブリッジです。CDP（Chrome DevTools Protocol）に一切依存しないため、デバッグフラグ不要、自動化バナーなし、`navigator.webdriver` 検知なしで動作します。

## アーキテクチャ

```
CLIツール（stdin / curl）
        |
        v
  server.py（Python）
  - WebSocket サーバー（ポート 9765）
  - HTTP API サーバー（ポート 9766、サーブモード時のみ）
        |
        v  WebSocket（localhost限定）
        |
  background.js（MV3 Service Worker）
  - WebSocket経由でコマンドを受信
  - executeCommand をアクティブタブに注入
  - 結果をサーバーに返却
        |
        v  chrome.scripting.executeScript
        |
  ページコンテキスト（アクティブタブ）
  - DOM操作（get_dom, get_elements, click）
```

## 動作モード

### ワンショットモード（デフォルト）

stdin から JSON コマンドを1つ読み取り、拡張機能に送信、レスポンスを stdout に出力して終了。

```bash
echo '{"cmd":"get_dom"}' | python3 server.py
```

### サーブモード（`--serve`）

サーバーが常駐し、ポート 9766 の HTTP リスナーが POST リクエストを受け付け、WebSocket 経由で拡張機能に転送。起動ごとのレイテンシを排除。

```bash
python3 server.py --serve
curl -s 127.0.0.1:9766 -d '{"cmd":"get_dom"}'
```

## コマンド一覧

| コマンド | フィールド | 説明 |
|---------|-----------|------|
| `get_dom` | `cmd` | アクティブタブの `outerHTML` 全体を返す。500KBで切り詰め、DOM要約を付加。 |
| `click` | `cmd`, `selector` | CSSセレクタに一致する最初の要素をクリック。 |
| `get_elements` | `cmd`, `selector` | 一致する要素の配列を返す（tag, text, href, id, className）。 |

全コマンドで `browser` フィールド（`"chrome"` / `"edge"`）によるブラウザ指定が可能。

### リクエスト形式

```json
{
  "cmd": "get_elements",
  "selector": "a.nav-link",
  "browser": "chrome"
}
```

### レスポンス形式

```json
{
  "result": "ok",
  "data": [...]
}
```

エラー時:

```json
{
  "result": "error",
  "message": "Element not found: .nonexistent"
}
```

## マルチブラウザ対応

複数のブラウザ拡張機能が同時に接続可能。各拡張機能は接続直後に `identify` メッセージを送信:

```json
{"type": "identify", "browser": "chrome"}
```

- コマンドに `browser` が指定されている場合、対応するクライアントにルーティング。
- `browser` 省略時、接続中のクライアントが1つだけならそのクライアントを自動選択。
- 複数接続中に `browser` を省略するとエラーを返す。

## 設定

| パラメータ | CLIフラグ | 環境変数 | デフォルト |
|-----------|----------|---------|-----------|
| WebSocket ポート | `--port` | `CHROME_KONTROL_PORT` | 9765 |
| HTTP API ポート | `--http-port` | `CHROME_KONTROL_HTTP_PORT` | 9766 |

優先順位: CLIフラグ > 環境変数 > デフォルト値

## セキュリティモデル

### ネットワーク分離
- WebSocket / HTTP リスナーはともに `127.0.0.1` にのみバインド。
- WebSocket の Origin ヘッダーを localhost 限定のホワイトリストで検証。
- `ws://0.0.0.0` はホワイトリストから明示的に除外。

### 入力検証
- コマンドは固定のホワイトリスト（`get_dom`, `click`, `get_elements`）で検証。
- CSSセレクタは512文字上限。
- ブラウザ名はホワイトリスト（`chrome`, `edge`）で制限。
- 受信メッセージは 5 MiB 上限でメモリ枯渇を防止。

### 出力サニタイズ
- ログメッセージは ASCII/Unicode 制御文字を除去し、ログインジェクションを防止。
- ページコンテキストの生エラー詳細は呼び出し側に公開しない。

### 拡張機能のセキュリティ
- `content.js` は意図的に空。DOM操作は `chrome.scripting.executeScript` で注入し、影響範囲を限定。
- コマンド引数は関数パラメータとして渡し、文字列結合によるコードインジェクションを防止。

### HTTP サーバー
- POST メソッドのみ受け付け。
- Content-Length 必須、上限あり。
- ヘッダー読み取りは 8 KiB 上限、10秒デッドライン。
- 並行リクエストは `asyncio.Lock` で直列化し、レスポンスの混入を防止。
- レスポンスヘッダーに `Cache-Control: no-store` および `X-Content-Type-Options: nosniff` を付加。

## MV3 Service Worker のキープアライブ

MV3 の Service Worker は約30秒の無操作で停止される。ChromeKontrol は以下の2層で対処:

1. `chrome.alarms`（クライアント側）: 30秒周期の定期アラームで Service Worker を起こし、WebSocket が切断されていれば再接続。
2. Ping フレーム（サーバー側、サーブモード）: サーバーが20秒間隔で WebSocket ping を送信し、接続を維持。

## 依存関係

- Python: `websockets`（単一依存、`requirements.txt` でハッシュ固定）
- 拡張機能: 外部依存なし。全て Chrome Extension API のネイティブ機能。

## ファイル構成

```
ChromeKontrol/
  server.py         WebSocket/HTTP サーバー（Python）
  background.js     MV3 Service Worker（拡張機能）
  content.js        コンテンツスクリプト（意図的に空）
  manifest.json     拡張機能マニフェスト（MV3）
  requirements.txt  Python 依存パッケージ（ハッシュ固定）
```
