# ChromeKontrol

デバッグモードなし、自動化バナーなし、検知リスクなし。CLIからブラウザを操作する軽量拡張。

ChromeKontrol は Chrome/Edge 拡張機能 + Python サーバーの構成で、WebSocket経由でCLIツールからブラウザのDOMを操作できます。CDP や `chrome.debugger` ではなく `chrome.scripting.executeScript` を使用するため、ブラウザにデバッグモードの痕跡が一切残りません。

## なぜ CDP じゃダメなのか

| 手法 | デバッグバナー | `navigator.webdriver` | Anti-bot検知リスク |
|------|:---:|:---:|:---:|
| CDP（Puppeteer, Playwright） | 出る | 出る | 高 |
| `chrome.debugger` API | 出る | 出ない | 中 |
| **ChromeKontrol**（`chrome.scripting`） | **出ない** | **出ない** | **なし** |

## 対応ブラウザ

- Chrome（MV3）
- Edge（MV3）
- マルチブラウザ: 両方同時に接続し、コマンドごとに対象を指定可能

Firefox は非対応です（Manifest V3 / Chromium 拡張機能 API に依存）。

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

HTTP経由でコマンドを送信:

```bash
# 単一ブラウザ
curl -s 127.0.0.1:9766 -d '{"cmd":"get_dom"}'

# ブラウザ指定
curl -s 127.0.0.1:9766 -d '{"cmd":"get_elements","selector":"a","browser":"edge"}'
```

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

## ライセンス

[MIT](LICENSE)
