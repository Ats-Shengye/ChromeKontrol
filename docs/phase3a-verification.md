# Phase 3a 実機確認手順書

> **注記（Phase F1 以降）**: この文書の作成後、拡張の配置場所が `extensions/chromium/` に移動しました（`background.js` / `manifest.json` は `extensions/chromium/background.js` / `extensions/chromium/manifest.json` を指します）。パス変更前の拡張を読み込んでいる場合、以下の「再読み込み」ボタンでは反映されません。初回のみ `chrome://extensions` で一度削除し、新しいパスで読み込み直す必要があります（詳細は [`docs/extension-path-migration.md`](extension-path-migration.md) 参照）。それ以外の手順（本文書の内容）は Phase 3a 時点の検証記録として変更していません。

対象: `background.js` / `manifest.json` の変更（ISSUES.md P0-1「拡張側のプロファイル識別」、P1-2「currentWindowの不定性」修正）。

この文書は実装エージェント（自動テストの対象外である Chrome 拡張 API 部分）の代わりに、人手で確認すべき項目をまとめたものです。実在のメールアドレスやアカウント名は書かず、`example.com` および `Default` / `Profile 1` のような一般的なプロファイル名のみを使用しています。

前提: サーバー (`server.py --serve`) が起動していること。以下のコマンド例では HTTP API ポート `9766`（デフォルト）、トークンは `~/.config/chromekontrol/token` から読み取る前提とします。

```bash
TOKEN=$(cat ~/.config/chromekontrol/token)
```

---

## 1. 拡張のリロード

変更した `background.js` / `manifest.json` を反映させるには、インストール済みの拡張機能を手動でリロードする必要があります。以下の3箇所すべてで実施してください。

1. **Chrome / Default プロファイル**
   - `chrome://extensions` を開く
   - ChromeKontrol のカードにある「再読み込み」ボタン（円形矢印アイコン）をクリック
2. **Chrome / Profile 1**（別プロファイルのウィンドウで同様に）
   - そのプロファイルのウィンドウで `chrome://extensions` を開く
   - 同様に再読み込み
3. **Edge / Default プロファイル**
   - `edge://extensions` を開く
   - 同様に再読み込み

拡張を「無効化 → 有効化」するのではなく「再読み込み」ボタンを使うこと（無効化するとその間 Service Worker が完全に停止し、identify のキャッシュも消えるため、動作としては同じですが手順としては再読み込みの方が確実です）。

---

## 2. 権限変更の確認

`manifest.json` に `identity` / `identity.email` を追加したため、再読み込み時に Chrome / Edge から権限確認のダイアログが出る可能性があります。

- 「新しい権限が必要です」といった趣旨の警告が出た場合は、内容を確認したうえで許可してください（`chrome.identity.getProfileUserInfo()` を使うために必要な権限です）
- 警告が出ずに再読み込みが完了した場合も問題ありません（unpacked 拡張ではダイアログが出ない、または自動許可される場合があります）
- 万一、権限エラーで Service Worker がクラッシュする場合は `chrome://extensions` の「エラー」ボタンでスタックトレースを確認し、`identity` 権限まわりのエラーかどうかを確認してください

### 2-1. `identity` 権限が削減できるかの検証（セキュリティレビュー L-11）

現在 `manifest.json` には `identity` と `identity.email` の両方が宣言されていますが、**`identity.email` だけで足りる可能性があります**。

Chrome の公式ドキュメントは `getProfileUserInfo()` について「Requires the `identity.email` manifest permission」と記載していますが、`identity` 権限との併記が必須かは明示されていません。実装時点では外部ドキュメントを参照できない制約があったため、安全側に倒して両方を宣言しています。

`identity` 権限は `chrome.identity.launchWebAuthFlow()`（任意の OAuth 認証フローの起動）へのアクセスも開きます。最小権限の原則からは、不要なら削るべきです。

**手順 3〜5 が成功したあと**、以下を試してください。

```bash
# 1. manifest.json から "identity" の行を削除する（"identity.email" は残す）
#    permissions が ["activeTab", "alarms", "identity.email", "scripting", "storage", "tabs"] になる

# 2. 3プロファイルすべてで拡張を再読み込みする（手順1と同じ）

# 3. email が引き続き取得できるか確認する
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_clients"}'
```

- **`email` が入っていれば**: `identity` は不要。削除した状態を採用してコミットしてください
- **`email` が `null` になっていれば**: `identity` も必要。`manifest.json` を元に戻してください

`email` が取得できなくなっても接続自体は維持されます（`getEmail()` が失敗した場合は `email` フィールドを省略して identify を送る設計のため）。したがってこの検証で拡張が壊れることはありません。

---

## 3. `list_clients` で `profileId` / `email` が入っていることを確認する

各プロファイルを再読み込みしたあと、少し待って（数秒〜キープアライブアラームの周期分）から以下を実行します。

```bash
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_clients"}' | python3 -m json.tool
```

期待される出力（値は環境依存。`email` は Chrome にアカウントを紐付けている場合のみ入ります）:

```json
{
  "result": "ok",
  "data": [
    {
      "key": "chrome:3f2c1d8a-...",
      "browser": "chrome",
      "profileId": "3f2c1d8a-...",
      "email": "someone@example.com",
      "label": null,
      "displayName": "someone@example.com",
      "aliases": []
    }
  ]
}
```

確認ポイント:

- `profileId` が UUID 形式の文字列で入っていること（`null` のままなら `chrome.storage.local` の読み書きに失敗している可能性。Service Worker の DevTools コンソールで `console.warn` のログを確認）
- Chrome にアカウントを紐付けている場合、`email` が入っていること。紐付けていない場合は `email` が `null`（フィールド自体が省略されている）のままで、これは仕様どおりの動作です
- `key` が `chrome:<profileId>` の形式になっていること（旧仕様の `chrome` 単独ではないこと）

---

## 4. Chrome の2プロファイルが同時に接続できていることの確認

手順3の `list_clients` の結果で、`browser: "chrome"` のエントリが **2件** 返ってくることを確認します（`Default` と `Profile 1` それぞれ）。

- 2件とも `key` が異なる値（`chrome:<profileId1>` と `chrome:<profileId2>`）であること
- これが Phase 3a の主目的（P0-1: 複数プロファイルの同時接続）の成立確認です。1件しか返らない場合は、いずれかのプロファイルの拡張機能がリロードされていないか、Service Worker が停止したままになっている可能性があります。該当プロファイルのウィンドウを一度フォーカスしてキープアライブアラームを発火させてから再確認してください

---

## 5. `target` で個別のプロファイルを指定できることの確認

手順3で確認した `email`（または `profileId` の先頭部分）を使って、個別のプロファイルを指定できることを確認します。

```bash
# emailで指定（実際にはlist_clientsで確認した値に置き換える）
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_tabs","target":"chrome:someone@example.com"}' | python3 -m json.tool

# profileIdの前方一致で指定
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_tabs","target":"chrome:3f2c1d8a"}' | python3 -m json.tool
```

確認ポイント:

- 指定したプロファイル側のタブ一覧のみが返ること（もう一方のプロファイルのタブが混ざらないこと）
- 両方のプロファイルで異なる `target` を指定し、それぞれ正しいプロファイル側の結果が返ることを相互に確認する
- 存在しない値を指定した場合、または該当が複数ある値（例: `"target":"chrome"` を2プロファイル接続中に指定）を指定した場合はエラーと候補一覧が返ること（README.md「対象クライアントの指定（target）」参照）

---

## 6. `label` を手動設定する方法（暫定手段）

Phase 3c でオプションページが実装されるまでの暫定手段です。拡張の Service Worker に対して直接 `chrome.storage.local` に値を書き込みます。

1. `chrome://extensions` で ChromeKontrol の「Service Worker」リンク（「詳細」を開くと表示される、または拡張カード上に直接表示される）をクリックし、DevTools を開く
2. Console タブで以下を実行する

   ```js
   chrome.storage.local.set({ ck_label: "作業用" })
   ```

   （`"作業用"` の部分は任意のラベル名。空文字列・空白のみは無効なので避けること）

3. 設定を反映させるには、拡張の再読み込み、または Service Worker の再起動が必要です
   - 簡単なのは `chrome://extensions` から拡張を再読み込みする方法です
   - もしくは Service Worker の DevTools コンソールで以下を実行して再接続だけを促す方法もあります（拡張全体の再読み込みより軽量です）

     ```js
     chrome.storage.local.set({ ck_label: "作業用" }).then(() => {
       // 次回の接続からlabelが反映される。現在の接続を切って再接続させる。
       chrome.runtime.reload();
     });
     ```

4. 手順3の `list_clients` を再度実行し、該当エントリの `label` に設定した値が入っていることを確認する
5. ラベルを消したい場合は `chrome.storage.local.remove('ck_label')` を実行する

**注意**: `ck_label` に設定した値は `list_clients` のレスポンスや `target` のエイリアス解決に使われます。半角スペースのみ・空文字列は送信対象から除外されるため、意味のある文字列を設定してください。

---

## 7. ロールバック手順

問題が発生した場合の切り戻し手順です。

### 拡張を旧版に戻す

```bash
cd ~/ドキュメント/Code/ChromeKontrol
git log --oneline -- background.js manifest.json | head -5
git diff HEAD~1 -- background.js manifest.json   # 差分を確認してから
git checkout HEAD~1 -- background.js manifest.json
```

その後、`chrome://extensions` / `edge://extensions` で各プロファイルの拡張を再読み込みしてください。

### 権限だけを戻す（`background.js` の新機能は維持し、権限起因の問題だけを切り分けたい場合）

`manifest.json` の `permissions` から `"identity"` と `"identity.email"` を削除し、拡張を再読み込みしてください。この場合 `chrome.identity` が存在しないパスに入るため、`email` フィールドが送られなくなりますが（`getEmail()` が `chrome.identity` の存在チェックで `null` を返す）、`profileId` によるプロファイル識別自体は継続して機能します。

### サーバー側への影響はない

この Phase 3a では `server.py` を変更していないため、サーバー側のロールバックは不要です。拡張側だけを戻せば十分です。
