# 拡張の読み込みパス移行手順（Phase F1）

対象: `background.js` / `content.js` / `manifest.json` の配置場所が、リポジトリルート直下から `extensions/chromium/` に変更された移行（ISSUES.md P0-3 対応）。

この文書は Chrome / Edge に unpacked 拡張として ChromeKontrol を読み込んでいる利用者向けです。実在のメールアドレスやアカウント名は書かず、`example.com` および `Default` / `Profile 1` のような一般的なプロファイル名のみを使用しています。

---

## 1. 移行が必要な理由

ChromeKontrol は Chrome 拡張（`manifest.json` / `background.js` / `content.js`）と Python サーバー（`server.py` / `tests/`）を同一リポジトリで管理しています。従来これらは同一ディレクトリに同居しており、Python がテスト実行時に生成する `__pycache__` ディレクトリが Chrome / Edge の拡張ロード規約（`_` で始まるファイル・ディレクトリ名を拒否）に抵触し、拡張が読み込めなくなる不具合が発生していました（ISSUES.md P0-3）。

これを構造的に解決するため、拡張ファイル一式を専用ディレクトリ `extensions/chromium/` へ移動しました。Python 側が何を生成しても拡張のロードに影響しなくなります。

**副作用（重要）**: unpacked 拡張の拡張IDは、読み込み元のディレクトリパスから導出されます。パスが変わると Chrome / Edge は**別の拡張**として扱うため、以下が発生します。

- 拡張IDが変わる
- `chrome.storage.local` が新しい領域になり、既存の `ck_profile_id` / `ck_label`（プロファイル識別情報）が失われる
- 拡張が再接続すると**新しい `profileId` が生成される**
- その結果、サーバー側の設定ファイル（`~/.config/chromekontrol/config.json`）に登録済みのエイリアスが、どのクライアントにも解決しなくなる

つまり、単にパスを変えて再読み込みするだけでは済まず、**エイリアス設定の再対応付けが必要**です。以下の手順に沿って移行してください。

---

## 2. 移行前に記録しておくこと

エイリアスの再設定時に「どの `profileId` がどのアカウント・用途に対応していたか」を照合できるよう、移行前に現在のクライアント一覧を控えておきます。

サーバーが `--serve` モードで稼働している状態で、`list_clients` コマンドを実行します。

```bash
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_clients"}'
```

レスポンスに含まれる各クライアントの `key` / `profileId` / `email` / `label` / `aliases` を控えておいてください。これが移行後の対照表になります（例: 「`profileId: XXXX` は `label: 仕事用` だった」というメモ）。

あわせて `~/.config/chromekontrol/config.json` の内容（特に `aliases` セクション）も控えておくと、移行後の再設定がスムーズです。

---

## 3. 各ブラウザでの手順

拡張を「無効化 → 有効化」や「再読み込み」ボタンでは反映できません。**パスが変わるため、一度削除してから新しいパスで読み込み直す**必要があります。

複数のブラウザプロファイルを使っている場合は、**プロファイルごとに**以下を実施してください。

### Chrome

1. `chrome://extensions` を開く
2. ChromeKontrol のカードで「削除」を選択する
3. 「デベロッパーモード」が有効になっていることを確認する
4. 「パッケージ化されていない拡張機能を読み込む」を選択し、`<プロジェクトルート>/extensions/chromium/` ディレクトリを指定する

### Edge

1. `edge://extensions` を開く
2. 同様に「削除」→「パッケージ化されていない拡張機能を読み込む」で `<プロジェクトルート>/extensions/chromium/` を指定する

権限確認のダイアログが表示された場合は、内容を確認したうえで許可してください（`identity` / `identity.email` 等、既存の権限に対する再確認です。新しい権限が追加されているわけではありません）。

---

## 4. 移行後の確認

拡張を再読み込みし、サーバーに再接続させた後、再度 `list_clients` を実行します。

```bash
curl -s 127.0.0.1:9766 \
  -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
  -H "Content-Type: application/json" \
  -d '{"cmd":"list_clients"}'
```

手順2で控えた対照表（`email` / `label` などの識別情報）を参照しながら、各クライアントの**新しい** `profileId` を確認してください。

その後、`~/.config/chromekontrol/config.json` の `aliases` セクションを、新しい `profileId`（または `email` / `label`）を使った `"browser:識別子"` 形式の値に更新します。設定ファイルの書式は README.md「エイリアス設定ファイル」の節を参照してください。

**設定ファイルはサーバー起動時に1回だけ読み込まれます。** `config.json` を更新したら、サーバーを再起動して変更を反映してください。

---

## 5. ロールバック手順

移行を取り消す場合は、`git` でファイルを元の場所に戻します。

```bash
git mv extensions/chromium/manifest.json manifest.json
git mv extensions/chromium/background.js background.js
git mv extensions/chromium/content.js content.js
```

その後、Chrome / Edge の各プロファイルで ChromeKontrol 拡張を一度削除し、旧パス（`<プロジェクトルート>/`）で「パッケージ化されていない拡張機能を読み込む」から再読み込みしてください。

**注意: ロールバックしても旧 `profileId` は復元されません。**

拡張IDは読み込み元のパスから導出されるため旧パスに戻せば旧IDに戻りますが、拡張の削除時に `chrome.storage.local` のデータは消去されています。Chrome の公式ドキュメントは「データはローカルに保存され、拡張機能が削除されると消去される」と明記しており、この挙動は回避できません。

したがってロールバック後も `profileId` は新規生成され、`config.json` のエイリアスは再度更新が必要です。**移行しても、ロールバックしても、`profileId` の作り直しは1回ずつ発生します。**
