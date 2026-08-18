# ChromeKontrol 仕様書

## 概要

ChromeKontrol は、Chrome/Edge/Firefox 拡張機能とローカル WebSocket サーバーを介して、CLIツールからユーザーのブラウザを操作する軽量ブリッジです。CDP（Chrome DevTools Protocol）に一切依存しないため、デバッグフラグ不要、自動化バナーなし、`navigator.webdriver` 検知なしで動作します。

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
  - executeCommand を対象タブに注入（tabId指定 or アクティブタブ）
  - 結果をサーバーに返却
        |
        v  api.scripting.executeScript
        |
  ページコンテキスト（対象タブ）
  - DOM操作（get_dom, get_elements, click）
  - タブ一覧取得（list_tabs、background.jsで完結）
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
| `get_dom` | `cmd` | 対象タブの `outerHTML` 全体を返す。500KBで切り詰め、DOM要約を付加。 |
| `click` | `cmd`, `selector` | CSSセレクタに一致する最初の要素をクリック。 |
| `get_elements` | `cmd`, `selector` | 一致する要素の配列を返す（tag, text, href, id, className）。 |
| `list_tabs` | `cmd` | 全タブの一覧を返す（id, url, title, active）。background.jsで完結しタブへの注入なし。 |

全コマンドで `browser` フィールド（`"chrome"` / `"edge"` / `"firefox"`）によるブラウザ指定が可能。
`get_dom`, `click`, `get_elements` は `tabId` フィールド（非負整数）でタブを指定可能。省略時はアクティブタブにフォールバック。

### リクエスト形式

```json
{
  "cmd": "get_elements",
  "selector": "a.nav-link",
  "browser": "chrome",
  "tabId": 42
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

複数のブラウザ拡張機能（Chrome / Edge / Firefox）が同時に接続可能。各拡張機能は接続直後に `identify` メッセージを送信:

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
- コマンドは固定のホワイトリスト（`get_dom`, `click`, `get_elements`, `list_tabs`）で検証。
- CSSセレクタは512文字上限。
- ブラウザ名はホワイトリスト（`chrome`, `edge`, `firefox`）で制限。
- `tabId` は非負整数、server.py / background.js の両層で二重検証（Python側は `bool` 型を明示除外）。
- 受信メッセージは 5 MiB 上限でメモリ枯渇を防止。

### 出力サニタイズ
- ログメッセージは ASCII/Unicode 制御文字を除去し、ログインジェクションを防止。
- ページコンテキストの生エラー詳細は呼び出し側に公開しない。

### 拡張機能のセキュリティ
- `content.js` は意図的に空。DOM操作は `api.scripting.executeScript` で注入し、影響範囲を限定。
- コマンド引数は関数パラメータとして渡し、文字列結合によるコードインジェクションを防止。
- `tabs` パーミッション: `list_tabs` コマンドおよび `tabId` 指定によるタブ特定に使用。従来の `activeTab` に加え、全タブのメタデータ（URL, title）へのアクセスが可能になる。この権限拡大は脅威モデルの「同一マシン上の他プロセスからの攻撃はout-of-scope」前提のもとで受容。

### HTTP サーバー
- POST メソッドのみ受け付け。
- Content-Length 必須、上限あり。
- ヘッダー読み取りは 8 KiB 上限、10秒デッドライン。
- 並行リクエストは `asyncio.Lock` で直列化し、レスポンスの混入を防止。
- レスポンスヘッダーに `Cache-Control: no-store` および `X-Content-Type-Options: nosniff` を付加。

## 脅威モデル

### 保護対象（in-scope）
- 他オリジンのWebサイトからの CSRF 攻撃（訪問者の意図しない操作）
- 外部ネットワークからの直接接続（localhost限定バインドで防止）
- 悪意のあるcontent scriptによる拡張機能経由のDOM操作（executeScript関数引数渡しで分離）
- メモリ枯渇DoS（メッセージサイズ上限、ヘッダー上限、タイムアウト）
- ログインジェクション、タイミング攻撃
- 同じbrowser_nameを用いた偽identifyによる正規接続の奪取（後着拒否で防止）

### 保護対象外（out-of-scope）
- **同一マシン上の他プロセスからの攻撃**: localhost HTTPサーバーに `lsof -i` 等で到達可能な悪意プロセスが存在する環境は想定外。シングルユーザー開発マシン前提。この前提により、`list_tabs` による全タブURL列挙および `tabId` 指定による非アクティブタブのDOM取得も、ローカル信頼境界内の操作として受容する
- **Firefox/Chrome本体の脆弱性**: ブラウザ本体がRCE等で侵害された場合の防御は本ツールのスコープ外
- **ユーザーが誤って弱いトークンを設定する運用ミス**: 32文字未満警告は出すが拒否はしない
- **拡張機能自体の改ざん検知**: manifest署名等による改ざん検出は未実装（Chrome/Edge Web Store経由の配布で担保）

### プロファイル自動起動（外部プロセス起動、ISSUES.md P0-1 Phase 2c）

サーバーは `target` で指定されたプロファイルが未接続の場合、条件を満たせばブラウザプロセスを自動起動できる。これは本プロジェクトで唯一、サーバーが外部プロセスを起動する機能であり、以下の設計で脅威モデルの範囲内に収めている。

- **既定で無効**。設定ファイル（`~/.config/chromekontrol/config.json`）の `"autoLaunch": true` を明示的に設定した場合のみ動作する。既定値・非boolean値・キー非存在はすべて無効として扱う。
- **実行ファイルは設定ファイルから指定できない**。ブラウザ実行ファイル名はサーバー側の許可リスト（`chrome`: `google-chrome` / `google-chrome-stable` / `chromium`、`edge`: `microsoft-edge` / `microsoft-edge-stable`）に固定され、`shutil.which()` でこの順にPATHから解決する。設定ファイルに書けるのは `"profiles"` マッピングのプロファイル**ディレクトリ名**（例: `"Profile 1"`）のみであり、実行するコマンドそのものは一切ユーザー入力から決まらない。
- **Firefox は自動起動の対象外**。`browser` フィールドやターゲット指定では利用できるが、上記の許可リストには追加していない。理由: (1) 検証環境のFirefoxはflatpak版で起動が`flatpak run org.mozilla.firefox`というサブコマンド形式になり、単一の実行ファイル名を前提とするこの仕組みと構造が合わない、(2) プロファイル指定の形式が異なる（Chrome/Edgeは`--profile-directory=<名前>`、Firefoxは`-P <名前>`または`--profile <パス>`）、(3) 実環境のFirefoxプロファイル名に非ASCII文字や半角スペースが含まれ、プロファイルディレクトリ名の検証を通らないものがある。`target` に `firefox:...` を指定して未接続の場合は、「自動起動に対応していない」旨のエラーを返し、ブラウザは手動で起動する必要がある。
- **プロファイルディレクトリ名は文字種・長さを検証する**（1〜64文字、ASCII英数字・半角スペース・ハイフン・アンダースコアのみ、先頭ハイフン禁止、`/` `\` 禁止、`.`/`..` 禁止）。
- **`subprocess.Popen()` は `shell=False`（引数は配列渡し）で呼び出す**ため、シェルメタ文字（`;` `|` `&` `$` 等）は構造的に解釈されず、シェルインジェクションは成立しない。プロファイルディレクトリ名に半角スペースが含まれても1つの引数として正しく扱われる。
- 起動したプロセスの `stdin` / `stdout` / `stderr` はすべて `DEVNULL` に向け、`wait()` はしない（独立プロセスとして動作させる）。
- 同一プロファイルへの起動は60秒のクールダウンを設け、連続起動を防止する。

**`config.json` を書き換えられる攻撃者に対する保護範囲**: 上記の設計により、任意コマンド実行・シェルインジェクション・引数インジェクションのいずれも防止される。ただし、この防御は「設定ファイルを書き換えられるが任意コマンド実行はできない攻撃者」を対象としたものであり、それ以上の防御は行わない。同一ユーザー権限を持つ攻撃者（`config.json` を書き換えられる時点でホームディレクトリ全体に書き込み可能）は、そもそも本ツールを経由せずとも任意コード実行が可能なため、引き続き脅威モデル外（「同一マシン上の他プロセスからの攻撃」節を参照）とする。

---

## MV3 Service Worker のキープアライブ

MV3 の Service Worker は約30秒の無操作で停止される。ChromeKontrol は以下の2層で対処:

1. `api.alarms`（クライアント側）: 30秒周期の定期アラームで Service Worker を起こし、WebSocket が切断されていれば再接続。
2. Ping フレーム（サーバー側、サーブモード）: サーバーが20秒間隔で WebSocket ping を送信し、接続を維持。

## 依存関係

- Python: `websockets`（単一依存、`requirements.txt` でハッシュ固定）
- 拡張機能: 外部依存なし。全て WebExtension API のネイティブ機能。

### 名前空間の抽象化（Phase F3a）

`background.js` は実行環境で解決される単一の名前空間参照 `api` 経由ですべての
WebExtension API を呼び出す。

```javascript
const api = typeof browser !== 'undefined' && browser.runtime ? browser : chrome;
```

Firefox の `chrome.*` はコールバック形式のみで Promise を返さない一方、
Firefox の `browser.*` と Chrome MV3 の `chrome.*` はどちらも Promise を返す
（MDN: "As a porting aid, the Firefox implementation of WebExtensions supports
`chrome` using callbacks and `browser` using promises."）。実行環境に存在する
方を一度だけ選ぶことで、`background.js` / `content.js` は Chrome / Edge /
Firefox の3ブラウザで完全に同一のファイルとして動作する（差分ファイルを
持たない）。ポリフィル等の外部依存は追加していない。

### Firefox 固有の制約（Phase F3b）

サーバー側（`server.py`）は Phase F2 で Firefox クライアントを受理できるよう
対応済み（`ALLOWED_BROWSERS` に `firefox` を含む）。Phase F3b で
`extensions/firefox/` を追加し、拡張側も揃った。ただし以下は Chrome/Edge と
挙動が異なる、または対応していない。

- **`identity` API 非対応**: Firefox には `chrome.identity.getProfileUserInfo()`
  に相当する API がなく、`email` によるプロファイル識別ができない。
  `extensions/firefox/manifest.json` の `permissions` から `identity` /
  `identity.email` を除外している（最小権限の原則。存在しないAPIの権限を
  要求しない）。複数の Firefox プロファイルを区別する場合は `profileId`
  （自動生成、`ck_profile_id` として `storage.local` に永続化）または
  `label`（ユーザー設定）を使う。
- **CSP による既定ポート固定**: `extensions/firefox/manifest.json` は
  `content_security_policy.extension_pages` の `connect-src` を
  `ws://127.0.0.1:9765`（既定ポート）に固定している。MV3 の
  `extension_pages` CSP における `connect-src` のポートワイルドカード
  （`ws://127.0.0.1:*`）の扱いが MDN 上で明記されていないため、動作が
  確実な既定ポート固定を選んでいる。`chrome.storage.local` の `ws_port` に
  カスタムポートを設定した場合、Chrome/Edge は CSP を宣言していないため
  制約なく動作するが、Firefox は CSP 違反で接続できない。
- **一時的なアドオンの永続性**: 署名なしの拡張は
  `about:debugging#/runtime/this-firefox` の「一時的なアドオンを読み込む」
  でのみ読み込め、ブラウザを再起動すると消える。恒久的な運用には署名、
  または Developer Edition / Nightly での
  `xpinstall.signatures.required=false` が必要。
- **プロファイル自動起動（`autoLaunch`）の対象外**: 理由は本ドキュメントの
  「プロファイル自動起動（外部プロセス起動、ISSUES.md P0-1 Phase 2c）」
  節を参照。

## ファイル構成

```
ChromeKontrol/
  server.py                       WebSocket/HTTP サーバー（Python）
  extensions/
    chromium/
      background.js              MV3 Service Worker（拡張機能）
      content.js                 コンテンツスクリプト（意図的に空）
      manifest.json              拡張機能マニフェスト（MV3、Chrome/Edge用）
    firefox/
      background.js              chromium/ とバイト単位で同一
      content.js                 chromium/ とバイト単位で同一
      manifest.json              拡張機能マニフェスト（MV3、Firefox用。
                                   permissions / background / CSP /
                                   browser_specific_settings が異なる）
    tests/
      parity.test.mjs            chromium/ と firefox/ の同一性を検証
  requirements.txt                Python 依存パッケージ（ハッシュ固定）
```
