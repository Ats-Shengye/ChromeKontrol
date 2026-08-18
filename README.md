# ChromeKontrol

個人用途の軽量Chrome/Edge/Firefox MV3拡張機能。CLIからローカルブラウザのDOMを操作します。CDP（Chrome DevTools Protocol）や `chrome.debugger` ではなく `chrome.scripting.executeScript` を使用するため、デバッグバナーや自動化通知を表示せず動作します。

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
- Firefox（MV3、121以降。詳細は [`docs/firefox-setup.md`](docs/firefox-setup.md) 参照）
- マルチブラウザ: 複数同時に接続し、コマンドごとに対象を指定可能

`extensions/chromium/` と `extensions/firefox/` の `background.js` / `content.js` はバイト単位で完全に同一です（Phase F3a で名前空間を `api` 参照に統一したため、同じコードがそのまま3ブラウザで動作します）。差分は `manifest.json` のみで、この同一性は `extensions/tests/parity.test.mjs` で継続的に検証されます。

## クイックスタート

1. Python依存パッケージのインストール

```bash
pip install websockets
```

2. 拡張機能の読み込み

### Chrome / Edge

1. `chrome://extensions`（または `edge://extensions`）を開く
2. 「デベロッパーモード」を有効にする
3. 「パッケージ化されていない拡張機能を読み込む」で `ChromeKontrol/extensions/chromium/` ディレクトリを選択

> **以前のバージョンを読み込んでいる場合**: `chrome://extensions` で一度削除してから新しいパスで再読み込みしてください。unpacked 拡張の ID は読み込み元のパスから導出されるため、パス変更後は別の拡張として扱われ、拡張のストレージ（プロファイル識別子など）も作り直されます。詳細な手順は [`docs/extension-path-migration.md`](docs/extension-path-migration.md) を参照してください。

### Firefox

Chrome / Edge とは読み込み方法が異なります。**ディレクトリではなくファイルを選択**します。

1. `about:debugging#/runtime/this-firefox` を開く
2. 「一時的なアドオンを読み込む」を選択し、`ChromeKontrol/extensions/firefox/manifest.json` を選択

> **一時的なアドオンはブラウザを再起動すると消えます**。恒久的にインストールするには署名が必要（または Developer Edition / Nightly で `xpinstall.signatures.required` を `false` に設定）です。日常的に使う場合は、ブラウザ起動のたびに再読み込みが必要になる点に注意してください。詳しい手順・制約は [`docs/firefox-setup.md`](docs/firefox-setup.md) を参照。

**Firefox固有の制約**:
- `extensions/firefox/manifest.json` の CSP（`content_security_policy`）が接続先を既定ポート `ws://127.0.0.1:9765` に固定しています。`chrome.storage.local` の `ws_port` にカスタムポートを設定した場合、Chrome/Edge では動作しますが Firefox では CSP 違反で接続できません
- Firefox には `chrome.identity.getProfileUserInfo()` に相当する API がなく、`email` によるプロファイル識別ができません（`manifest.json` の `permissions` から `identity` / `identity.email` も除外しています）。複数の Firefox プロファイルを区別する場合は `profileId`（自動生成）または `label`（後述のエイリアス設定で使用）を使ってください

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
| `list_tabs` | `cmd` | 開いている全タブの一覧（`id` / `url` / `title` / `active`）を返す |
| `list_clients` | `cmd` | 接続中クライアントの一覧（`key` / `browser` / `profileId` / `email` / `label` / `displayName` / `aliases`）を返す |

全コマンドで `"browser"` フィールド（`"chrome"` / `"edge"` / `"firefox"`）または `"target"` フィールド（エイリアス名や `"browser:識別子"` 形式。後述「対象クライアントの指定（target）」参照）による対象クライアント指定が可能。**両方を同時に指定するとエラー**になる。

`get_dom` / `click` / `get_elements` は任意で `"tabId"` フィールド（`list_tabs` が返す `id` の値）を受け付ける。省略時はアクティブタブが対象になる。

## 対象クライアントの指定（target）

Chrome の複数プロファイルや Edge を同時に接続している場合、`"browser"` フィールドだけでは「Chromeのどのプロファイルか」まで指定できません。`"target"` フィールドを使うと、用途ベースの別名（エイリアス）や `"browser:識別子"` 形式で具体的なクライアントを指定できます。

```bash
# ブラウザ名だけ指定（該当ブラウザのクライアントが1つだけなら解決する）
curl -s ... -d '{"cmd":"list_tabs","target":"chrome"}'

# browser:identifier 形式（email / emailのローカルパート / label / profileIdの前方一致で解決）
curl -s ... -d '{"cmd":"list_tabs","target":"chrome:work@example.com"}'

# ワイルドカード（そのブラウザのクライアントが1つだけなら解決する）
curl -s ... -d '{"cmd":"list_tabs","target":"edge:*"}'

# エイリアス経由（後述の設定ファイル参照）
curl -s ... -d '{"cmd":"list_tabs","target":"仕事"}'
```

### エイリアス設定ファイル

`~/.config/chromekontrol/config.json` に用途ベースの別名を定義できます。

```jsonc
{
  "aliases": {
    "仕事":   "chrome:work@example.com",
    "サブ":   "chrome:work@example.com",
    "メイン": "chrome:main@example.com",
    "Edge":   "edge:*"
  }
}
```

- キー・値ともに1〜256文字の文字列であること。不正なエントリはそのエントリのみ無視され、他のエントリはそのまま読み込まれます
- ファイルが存在しない場合はエイリアスなしで動作します（正常系のためログは出ません）。JSONが不正、または `"aliases"` がオブジェクトでない場合は警告ログを出しつつエイリアスなしで動作を継続します
- **設定ファイルはサーバー起動時に1回だけ読み込まれます。変更を反映するにはサーバーの再起動が必要です**
- `aliases` 以外のトップレベルキーは無視されます（将来の拡張用に予約）
- 読み込み先パスは環境変数 `CHROME_KONTROL_CONFIG_FILE` で上書き可能です（主にテスト用途）

### 解決順序

1. `target` の値がエイリアスのキーと大文字小文字を無視して完全一致すれば、その値を解決対象文字列とする（再帰解決は1回のみ）
2. 解決対象文字列を最初の `":"` で `browser` 部と識別子部に分割する
3. `browser` 部があればそのブラウザのクライアントに絞り込み、識別子部が `"*"` ならそのまま0/1/複数を判定、それ以外なら次の4段階照合を試す
4. `browser` 部がなければ、識別子部全体がブラウザ名（`chrome`/`edge`/`firefox`）と一致するか確認し、一致すればそのブラウザのクライアントで判定。一致しなければ全クライアントを対象に4段階照合を試す

4段階照合の順序（大文字小文字は無視、Unicode NFC正規化を適用）: `label`完全一致 → `email`完全一致 → `email`のローカルパート完全一致 → `profileId`前方一致。いずれかの段で1件に絞れればそこで確定、複数該当すればエラー（次の段には進まない）。

複数クライアントが該当した場合、または該当クライアントが未接続かつ後述の自動起動が発動しない場合は、候補一覧を含むエラーメッセージを返します（黙って1つを選ぶことはありません）。

### プロファイル自動起動（autoLaunch）

`target` で指定したプロファイルのウィンドウがまだ開かれていない場合、サーバーがブラウザを起動して接続を待つことができます。**既定では無効**です。

```jsonc
{
  "aliases": {
    "仕事": "chrome:work@example.com"
  },
  "autoLaunch": true,
  "profiles": {
    "chrome:work@example.com": "Profile 1",
    "chrome:main@example.com": "Default",
    "edge:*": "Default"
  }
}
```

> **注意**: `autoLaunch` を `true` にすると、サーバーがブラウザプロセスを起動するようになります。実行するブラウザはサーバー側の許可リスト（`google-chrome` / `google-chrome-stable` / `chromium` / `microsoft-edge` / `microsoft-edge-stable`）に固定されており、設定ファイルから任意のコマンドを実行させることはできません。詳細な脅威モデルは [SPEC.md](SPEC.md) を参照してください。

- `"profiles"` の**キーは `"aliases"` の値と同じ形式**（`"browser:識別子"`）です。エイリアス名（`"仕事"` 等）ではなく、エイリアス解決後の文字列をキーにします。上記の例では `"仕事"` → `"chrome:work@example.com"` と解決された後、`"profiles"` の `"chrome:work@example.com"` エントリが参照されます
- 値は Chrome/Edge の実際のプロファイルディレクトリ名（`Default`、`Profile 1` 等）です。`chrome://version` の「プロファイル パス」末尾のディレクトリ名で確認できます
- 発動するのは次の条件をすべて満たした場合のみです: `autoLaunch` が `true`、`target` が指定されている、解決対象文字列が `"profiles"` に登録されている、該当クライアントが未接続（複数該当の曖昧な状態では発動しません）、直近60秒以内に同じプロファイルへの起動を試みていない（クールダウン中でない）、ブラウザ実行ファイルが見つかる
- 起動後は最大30秒、拡張機能の接続を待ちます（通常のコマンドタイムアウト15秒より長めです。ブラウザの起動とService Workerの初期化に時間がかかるため）
- 同一プロファイルへの起動は60秒のクールダウンを設けています。ブラウザを手動で閉じても、クールダウンは時間経過でのみ解除されます
- いずれの条件を満たさない場合も、なぜ自動起動されなかったかを含むエラーメッセージが返ります

## サーブモード

連続してコマンドを送る場合、サーバーを常駐させると起動コストがなくなります。

```bash
python3 server.py --serve
```

HTTP APIへのアクセスには`X-ChromeKontrol-Token`ヘッダーと`Content-Type: application/json`が必須です（CSRF対策）。

### トークン管理（起動ごとに自動ローテーション）

認証トークンは起動のたびに新しく決定され、権限`0600`（所有ユーザーのみ読み書き可）のファイル `~/.config/chromekontrol/token` に書き出されます。**トークン値はstderr / ログに一切出力されません**（`systemd`常駐運用では journald に永続化されてしまうため）。案内されるのはファイルパスのみです。

```bash
python3 server.py --serve
# stderr: "Ready. Token available at /home/you/.config/chromekontrol/token (mode 0600). ..."

curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"get_dom"}'

curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"get_elements","selector":"a","browser":"edge"}'
```

サーバーを再起動するとトークンはローテーションされ、ファイルの内容が上書きされます。`mcp_bridge.mjs`経由の呼び出しはこれを自動検知して追従します（後述「MCP連携」参照）。

**固定トークンで運用したい場合**（環境変数 `CHROME_KONTROL_TOKEN` を設定）:

```bash
export CHROME_KONTROL_TOKEN=your_fixed_token_here
python3 server.py --serve
```

この場合も同じ値がトークンファイルに書き出されます（ファイル経由・環境変数経由のどちらでアクセスしても認証が通ります）。

> **注意**: サーバー側とMCP側（`mcp_bridge.mjs`）の双方で `CHROME_KONTROL_TOKEN` を設定する場合、**両者に同じ値を設定する必要があります**。サーバー側の値とMCP側の値が食い違っていても、サーバーの起動時にはエラーになりません（MCP側からのリクエストが401で拒否されるだけです）。固定トークン運用時は値の一致を手動で確認してください。

> **破壊的変更**: 認証ヘッダーなし、またはContent-Typeが`application/json`以外のリクエストはそれぞれ`401 Unauthorized` / `415 Unsupported Media Type`を返します。既存のcurlスクリプトへのヘッダー追加が必要です。

### systemd user unitで常駐させる

Claude Codeのバックグラウンドタスク等、他プロセスのライフサイクルに依存させたくない場合は systemd user unit として常駐させます。

```bash
# リポジトリ内のテンプレートを配置する（クローン後は毎回必要）
mkdir -p ~/.config/systemd/user
cp systemd/chromekontrol.service ~/.config/systemd/user/chromekontrol.service

systemctl --user daemon-reload
systemctl --user start chromekontrol
systemctl --user status chromekontrol

# 自動起動を有効にする場合（任意）
systemctl --user enable chromekontrol
```

ログは journald に出力されます（トークン値は含まれません）。

```bash
journalctl --user -u chromekontrol -f
```

unit定義は `.venv/bin/python`（`uv`が作成する仮想環境）を使う想定です。`.venv/`は`.gitignore`対象のためリポジトリには含まれません。クローン後は以下のいずれかで依存関係を用意してから起動してください。

```bash
# uvがある場合（推奨。pyproject.toml / uv.lock に従って再現）
uv sync

# uvがない場合の代替手段
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## AIコーディングアシスタントとの連携

JSON のパイプまたは HTTP リクエストを送れるCLIツールなら何でも使えます。

- Claude Code: Bash ツール経由のパイプ、またはサーブモードで curl
- OpenAI Codex CLI: 同上
- Gemini CLI: 同上
- シェルスクリプト / 自動化: `curl` または `echo | python3`

## MCP連携

`mcp_bridge.mjs` は ChromeKontrol の HTTP API を [Model Context Protocol](https://modelcontextprotocol.io/) のツールとして公開する薄いブリッジです。Claude Code等のMCP対応クライアントに登録すると、`curl`を書かずに直接ツール呼び出しとして使えます。

### セットアップ

```bash
npm install
```

MCPクライアント側の設定（例: Claude Codeの `~/.claude.json`）で `start_mcp.sh` を起動コマンドとして登録します。

```json
{
  "mcpServers": {
    "chromekontrol": {
      "command": "bash",
      "args": ["/path/to/ChromeKontrol/start_mcp.sh"]
    }
  }
}
```

`start_mcp.sh` はスクリプトの場所を解決して `node mcp_bridge.mjs` を実行するだけです。トークンの解決（環境変数 → トークンファイルの順）は `mcp_bridge.mjs` が**リクエストのたびに**行います。サーバー再起動でトークンがローテーションされても、次のツール呼び出しが401を受けた時点で自動的にファイルを読み直して1回だけ再試行するため、MCPサーバー（＝Claude Codeセッション）を再起動する必要はありません。

### 提供ツール

| ツール | 説明 |
|--------|------|
| `list_tabs` | 開いている全タブの一覧を返す |
| `get_dom` | タブのHTML全体を取得（プレーンテキストに変換して返す） |
| `get_elements` | CSSセレクタに一致する要素の情報を返す |
| `click` | CSSセレクタに一致する最初の要素をクリック |
| `get_text` | タブのクリーンなテキストを取得（HTMLタグ・script・styleを除去。`get_dom`よりノイズが少ない） |
| `list_clients` | 接続中のブラウザプロファイルと、それぞれに割り当てられたエイリアスを一覧する。他のツールの `target` に渡せる名前がわかる。パラメータなし |

`list_clients` 以外の5ツールが `tabId`（`list_tabs`の`id`）、`browser`（`"chrome"` / `"edge"` / `"firefox"`）、`target`（エイリアス名や `"browser:識別子"` 形式。HTTP APIの「対象クライアントの指定（target）」節と同じ仕様）を任意パラメータとして受け付けます。`target` と `browser` は同時指定不可（サーバー側がエラーを返します）。

起動時に一度だけ `list_clients` を叩き、その時点で接続中だったクライアントのエイリアス名を各ツールの `description` に追記します（`Available target names: ...`）。ChromeKontrolサーバーが未起動の場合や接続状況が変化した場合は、`list_clients` ツールを直接呼び出すことで最新の接続状況を確認できます。

### 接続エラー時の挙動

- **トークンが見つからない**（環境変数・トークンファイルのいずれにもない）: サーバーへの接続を試みず、想定パスを含むエラーメッセージを即座に返します
- **サーバーに接続できない**（未起動・接続拒否）: リトライせず、接続不可であることを即座に返します
- **401（トークン不一致）**: トークンファイルを読み直して1回だけ自動リトライします。再試行後も401なら、サーバーが再起動された可能性がある旨のエラーを返します

## 仕組み

```
CLI（stdin / curl） → server.py（WebSocket + HTTP） → 拡張機能（Service Worker） → ページDOM
```

詳細は [SPEC.md](SPEC.md)（アーキテクチャ・セキュリティモデル）、
用語解説は [GLOSSARY.md](GLOSSARY.md)（CDP / MV3 / chrome.scripting vs chrome.debugger の違い等）を参照。
セキュリティレビューの記録は [Security-Audit.md](Security-Audit.md) にあります。

> **補足**: ソースコードのコメントに現れる `P0-2` / `P1-1` 等の識別子は、開発時に使用した内部の課題管理番号です。診断レポート本体（`ISSUES.md`）は特定環境の構成情報を含むため、このリポジトリには含めていません。

## 設定

| パラメータ | CLIフラグ | 環境変数 | デフォルト |
|-----------|----------|---------|-----------|
| WebSocket ポート | `--port` | `CHROME_KONTROL_PORT` | 9765 |
| HTTP API ポート | `--http-port` | `CHROME_KONTROL_HTTP_PORT` | 9766 |
| HTTP API 認証トークン | なし | `CHROME_KONTROL_TOKEN` | 起動ごとにランダム生成 |
| トークンファイルの書き込み先 | なし | `CHROME_KONTROL_TOKEN_FILE` | `~/.config/chromekontrol/token` |
| エイリアス設定ファイルの読み込み先 | なし | `CHROME_KONTROL_CONFIG_FILE` | `~/.config/chromekontrol/config.json`（起動時に1回だけ読み込み） |

`CHROME_KONTROL_TOKEN_FILE` は `server.py`（書き込み先）と `mcp_bridge.mjs`（読み取り先）の双方で読まれます。片方だけに設定するとパスが食い違い、MCP側が常にトークンなしエラーを返すようになるため、設定する場合は両方に同じ値を指定してください（通常は指定不要。テスト・特殊な配置用途向け）。

## ライセンス

[MIT](LICENSE)
