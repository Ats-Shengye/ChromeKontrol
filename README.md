# ChromeKontrol

個人用途の軽量Chrome/Edge MV3拡張機能。CLIからローカルブラウザのDOMを操作します。CDP（Chrome DevTools Protocol）や `chrome.debugger` ではなく `chrome.scripting.executeScript` を使用するため、デバッグバナーや自動化通知を表示せず動作します。

> **用途**: 開発・個人用途のローカルブラウザ制御を想定。Anti-bot検知回避や、スクレイピング検出回避を目的とした使用は意図していません。

## 動作経路の比較（Chromium系のみ）

| 手法 | デバッグバナー | `navigator.webdriver` |
|------|:---:|:---:|
| CDP（Puppeteer, Playwright） | 出る | 出る |
| `chrome.debugger` API | 出る | 出ない |
| **ChromeKontrol**（`chrome.scripting`） | **出ない** | **出ない** |

## 対応ブラウザ

- Chrome（MV3）
- Edge（MV3）
- マルチブラウザ: 両方同時に接続し、コマンドごとに対象を指定可能

Firefox 用は別リポジトリ（[FirefoxKontrol](https://github.com/Ats-Shengye/FirefoxKontrol)）。

## クイックスタート

1. Python依存パッケージのインストール

```bash
pip install websockets
```

2. 拡張機能の読み込み

1. `chrome://extensions`（または `edge://extensions`）を開く
2. 「デベロッパーモード」を有効にする
3. 「パッケージ化されていない拡張機能を読み込む」で `ChromeKontrol/` ディレクトリを選択

3. コマンド実行

```bash
echo '{"cmd":"get_dom"}' | python3 server.py
```

## コマンド一覧

| コマンド | フィールド | 説明 |
|---------|-----------|------|
| `get_dom` | `cmd` | アクティブタブのHTML全体を取得 |
| `click` | `cmd`, `selector` | CSSセレクタに一致する最初の要素をクリック |
| `get_elements` | `cmd`, `selector` | 一致する要素の情報（tag, text, href, id, class）を返す |

全コマンドで `"browser"` フィールド（`"chrome"` / `"edge"`）によるブラウザ指定が可能。

## サーブモード

連続してコマンドを送る場合、サーバーを常駐させると起動コストがなくなります。

```bash
python3 server.py --serve
```

起動時にstderrへ認証トークンが表示されます。HTTP APIへのアクセスには`X-ChromeKontrol-Token`ヘッダーと`Content-Type: application/json`が必須です（CSRF対策）:

```bash
# トークンを固定する場合（~/.bashrc等に追加）
export CHROME_KONTROL_TOKEN=your_fixed_token_here

# サーバー起動
python3 server.py --serve
```

**環境変数でトークンを固定している場合**（stderrにトークン値は表示されません）:

```bash
# 環境変数がそのままヘッダー値として使えます
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $CHROME_KONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"get_dom"}'

curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $CHROME_KONTROL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"get_elements","selector":"a","browser":"edge"}'
```

**環境変数未設定の場合**（起動時にstderrへコピペ可能な形式で表示されます）:

```bash
# stderrに "TOKEN=xxx; curl ..." 形式で出力されるので、そのままコピペ可能
# 例: TOKEN=abc123...; curl -s 127.0.0.1:9766 -H "X-ChromeKontrol-Token: $TOKEN" ...
TOKEN=<起動時のstderrから取得>
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"get_dom"}'
```

> **破壊的変更**: 認証ヘッダーなし、またはContent-Typeが`application/json`以外のリクエストはそれぞれ`401 Unauthorized` / `415 Unsupported Media Type`を返します。既存のcurlスクリプトへのヘッダー追加が必要です。

## AIコーディングアシスタントとの連携

JSON のパイプまたは HTTP リクエストを送れるCLIツールなら何でも使えます。

- Claude Code: Bash ツール経由のパイプ、またはサーブモードで curl
- OpenAI Codex CLI: 同上
- Gemini CLI: 同上
- シェルスクリプト / 自動化: `curl` または `echo | python3`

## 仕組み

```
CLI（stdin / curl） → server.py（WebSocket + HTTP） → 拡張機能（Service Worker） → ページDOM
```

詳細は [SPEC.md](SPEC.md)（アーキテクチャ・セキュリティモデル）、
用語解説は [GLOSSARY.md](GLOSSARY.md)（CDP / MV3 / chrome.scripting vs chrome.debugger の違い等）を参照。

## 設定

| パラメータ | CLIフラグ | 環境変数 | デフォルト |
|-----------|----------|---------|-----------|
| WebSocket ポート | `--port` | `CHROME_KONTROL_PORT` | 9765 |
| HTTP API ポート | `--http-port` | `CHROME_KONTROL_HTTP_PORT` | 9766 |
| HTTP API 認証トークン | なし | `CHROME_KONTROL_TOKEN` | 起動ごとにランダム生成 |

## ライセンス

[MIT](LICENSE)
