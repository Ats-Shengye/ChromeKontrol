# Firefox セットアップ手順（Phase F3b）

対象: `extensions/firefox/` を Firefox に読み込み、動作確認するまでの手順。

この文書は Firefox に ChromeKontrol を一時的なアドオンとして読み込んでいる利用者向けです。実在のメールアドレス・実プロファイル名・業務用途の固有名詞は書かず、`example.com` および一般的な名称のみを使用しています。

---

## 1. 拡張機能の読み込み

Chrome / Edge とは読み込み方法が異なります。**ディレクトリではなくファイルを選択**する点に注意してください。

1. Firefox で `about:debugging#/runtime/this-firefox` を開く
2. 「一時的なアドオンを読み込む」（Load Temporary Add-on）を選択する
3. ファイル選択ダイアログで `<プロジェクトルート>/extensions/firefox/manifest.json` を選択する（`extensions/firefox/` ディレクトリではなく `manifest.json` ファイル自体）

読み込みに成功すると、`about:debugging#/runtime/this-firefox` の一覧に「ChromeKontrol」が表示されます。

> **一時的なアドオンはブラウザを再起動すると消えます。** 署名なしの拡張を恒久的にインストールするには、拡張への署名（Mozilla Add-on 配布経路）が必要です。個人の開発用途で署名を避けたい場合は、Firefox Developer Edition または Nightly で `about:config` の `xpinstall.signatures.required` を `false` に設定することで、署名なし拡張の永続インストールが可能になります（通常版 Firefox では利用できません）。日常的に使う場合は、この設定を行うか、Firefox 起動のたびに手順1〜3を繰り返してください。

---

## 2. サーバー側での接続確認

サーバーを `--serve` モードで起動している状態で、`list_clients` コマンドを実行し、Firefox クライアントが接続していることを確認します。

```bash
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_clients"}'
```

レスポンスに `"browser": "firefox"` のエントリが含まれていれば接続できています。

```jsonc
{
  "result": "ok",
  "data": [
    {
      "key": "firefox:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "browser": "firefox",
      "profileId": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
      "email": null,
      "label": null,
      "displayName": "firefox",
      "aliases": []
    }
  ]
}
```

**`"email": null` は正常です。** Firefox には Chrome の `chrome.identity.getProfileUserInfo()` に相当する API がなく、拡張機能からアカウントのメールアドレスを取得できません。`extensions/firefox/manifest.json` の `permissions` からも `identity` / `identity.email` を除外しています。プロファイルを識別する場合は `profileId`（拡張機能が自動生成し `storage.local` に永続化する UUID）または `label`（後述）を使ってください。

---

## 3. エイリアス設定（`profileId` / `label` の利用）

Firefox は `email` による識別ができないため、複数の Firefox プロファイルを区別したい場合は `profileId` または `label` を使ってエイリアスを設定します。

`~/.config/chromekontrol/config.json` の書式は README.md の「エイリアス設定ファイル」節を参照してください。`profileId` を使う場合の例:

```jsonc
{
  "aliases": {
    "firefox-work": "firefox:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
  }
}
```

`profileId` は手順2の `list_clients` レスポンスで確認できます。`profileId` は前方一致で解決されるため、先頭の数文字だけを指定しても、他の接続中クライアントと衝突しない範囲であれば解決できます。

**設定ファイルはサーバー起動時に1回だけ読み込まれます。** `config.json` を変更したらサーバーを再起動してください。

---

## 4. 動作確認

`list_tabs` でタブ一覧が取得できることを確認します。

```bash
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_tabs","browser":"firefox"}'
```

続けて `get_dom` で実際にページの DOM が取得できることを確認します（`http(s)://` のページを1つ開いた状態で実行してください。`about:` 等の内部ページはスクリプト実行の対象外です）。

```bash
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"get_dom","browser":"firefox"}'
```

いずれも `"result":"ok"` が返れば動作確認は完了です。

---

## 5. トラブルシューティング

### 接続できない・`list_clients` に Firefox クライアントが出てこない

1. **CSP のポート不一致を疑う**: `extensions/firefox/manifest.json` の `content_security_policy` は接続先を既定ポート `ws://127.0.0.1:9765` に固定しています。`chrome.storage.local` の `ws_port` にカスタムポートを設定していたり、サーバーを `--port` オプションで既定以外のポートで起動していたりすると、Firefox は CSP 違反で WebSocket 接続を拒否します（Chrome/Edge は CSP を宣言していないため、この制約を受けません）。ブラウザの「ツール」→「ブラウザコンソール」（`about:debugging#/runtime/this-firefox` の対象拡張の「検証」からも開けます）で `Content Security Policy` 関連のエラーが出ていないか確認してください
2. **拡張が読み込まれているか確認する**: `about:debugging#/runtime/this-firefox` の一覧に ChromeKontrol が表示されているか確認する。表示されていなければ本手順の「1. 拡張機能の読み込み」からやり直す
3. **ブラウザ再起動で消えていないか確認する**: 一時的なアドオンはブラウザプロセスの再起動（クラッシュ復帰・OS再起動含む）で読み込みが解除されます。再起動後は毎回手順1を実施してください
4. **サーバーが起動しているか確認する**: `systemctl --user status chromekontrol` や `curl -s 127.0.0.1:9766 -d '{}'`（トークン省略で401が返れば起動はしている）で疎通を確認する

### `email` が常に `null` になる

前述の通り、Firefox には該当 API がないため正常な挙動です。エラーではありません。`profileId` または `label` を使ってください。

---

## 6. 複数の Firefox プロファイルを使う場合

Firefox のプロファイル切り替え（`about:profiles` や `-P` 起動オプション）を使って複数プロファイルを使い分けている場合、**一時的なアドオンの読み込みはプロファイルごとに個別に必要**です。あるプロファイルで読み込んだ拡張は、別プロファイルには引き継がれません。

各プロファイルを起動するたびに本手順の「1. 拡張機能の読み込み」を実施し、「2. サーバー側での接続確認」で `list_clients` のレスポンスに複数の `"browser": "firefox"` エントリ（`profileId` で区別可能）が並ぶことを確認してください。エイリアス設定で `profileId` ごとに用途名を付けておくと、`target` フィールドでの指定が扱いやすくなります。
