# Security Report

updated: 2026-07-25

> 本レポートが参照する診断レポート本体（`ISSUES.md`）は、特定環境の構成情報を含むためリポジトリに含めていません。`P0-x` / `P1-x` / `P2-x` は内部の課題管理番号です。

## 最新レビュー結果

- 日付: 2026-07-25
- 対象: Phase F7（フォーカス最新のクライアントを自動選択 / P1-5 の解消）
- 判定: **条件付き通し → M-1 対応により PASS**
- 内部スコア: 8.5/10
- ASVS L1 準拠率: 93% (14/15) — 適用章: V1, V2, V12, V14
- **Critical 0 / High 0 / Medium 1 / Low 0** → M-1 は対応済み
- テスト: **463 passed, 2 xfailed**（着手前 423 + 新規40）/ カバレッジ 99.16% / JS **46 passed**（着手前 31 + 新規15）
- 実機検証: WebSocket プローブで `identify` → `focus` を送り、`Auto-selected most recently focused client` のログ出力と正しい選択を確認
- 両ディレクトリの `background.js` はバイト同一（39933 bytes）

**これで診断時に定義した「到達目標」の4要素すべてが実装完了。**

### フェーズ別の履歴

| Phase | 対象 | 判定 | スコア | ASVS L1 |
|---|---|---|---|---|
| 0 | テスト基盤（`pyproject.toml`, `uv.lock`, `tests/`, `.gitignore`） | PASS | 8/10 | 93% (14/15) |
| 1 | トークン経路修復 + systemd 化 | PASS（M-3/M-4 対応後） | 8/10 | 94% (15/16) |
| 2a | クライアント識別基盤 + P1-3 修正 | PASS | 8.5/10 | 94% (16/17) |
| 2b | エイリアス解決 + L-3/L-4/M-1 対応 | PASS（M-5 対応後） | 8.5/10 | 94% (16/17) |
| 2c | プロファイル自動起動 | PASS（M-6/M-7 対応後） | 8.5/10 | 94% (16/17) |
| 3a | 拡張側のプロファイル識別 + P1-2 修正 | PASS | 8.5/10 | 94% (15/16) |
| F1 | 拡張ファイルの `extensions/chromium/` 分離 | レビュー対象外（ファイル移動・ドキュメント・ビルド設定のみ） | — | — |
| F2 | サーバー側の Firefox クライアント受理 | PASS（M-1/M-2 対応後） | 8/10 | 95% (19/20) |
| F3a | 拡張の名前空間抽象化と Promise 統一 | PASS（M-1 は F3b で対応） | 8.5/10 | 94% (15/16) |
| F3b | `extensions/firefox/` の作成、同一性テスト、M-1 対応 | **PASS（修正指示なし）** | **9/10** | **100% (12/12)** |
| F5 | MCPブリッジの `target` 対応（P0-4 解消） | PASS（M-1 対応後） | 8.5/10 | 92% (11/12) |
| F6 | 候補列挙メッセージから email を排除（P0-5 解消） | **PASS（指摘0件）** | **9/10** | **100%**（スコープ内） |
| F7 | フォーカス最新のクライアントを自動選択（P1-5 解消） | PASS（M-1 対応後） | 8.5/10 | 93% (14/15) |

---

## 指摘履歴

### 2026-07-25 レビュー（Phase F7）

`target` / `browser` 省略時に、最終フォーカス時刻が最新のクライアントを自動選択する機能。拡張が `api.windows.onFocusChanged` を購読してサーバーへ通知する。

#### Critical / High

**なし**

#### Medium（対応済み）

**M-1: `_is_valid_positive_timestamp()` が `float('inf')` を通過し、後続の `int()` で `OverflowError` が未ハンドルで伝搬する**

**これは設計仕様の穴。** 仕様書に「数値（`int`/`float`、`bool` は除外）かつ 0 より大きいこと」と書いたが、**JSON の `1e999` が `float('inf')` としてパースされる経路**を考慮していなかった。

実測で確認した挙動:

```
json.loads('{"ts":1e999}') → inf (float)
  isinstance(float) : True
  inf > 0           : True    → 検証を通過してしまう
  int(inf)          : OverflowError

-inf → 既存の > 0 で拒否
nan  → 既存の > 0 が False で拒否
True → bool チェックで拒否
```

**`+inf` だけが穴だった。**

- 攻撃経路: ローカルプロセスが `{"type":"focus","ts":1e999}` を送ると `int(inf)` で `OverflowError` → `_handle_message()` → メッセージループ → `handle_connection()` の try から例外が伝搬し**送信元の接続ハンドラがクラッシュ**する。`finally` で cleanup は走るためサーバー自体は継続し、他のクライアントにも影響しない。`_receive_identify()` の `focusTs` 経路でも同様
- 実害: localhost 限定サービスであり、影響は自分自身の接続のクラッシュのみ。正規の拡張が `Date.now()` を送る限り到達しない。ただし防御的コーディングとして塞ぐべき
- 対応: `math.isfinite(value)` を条件に追加（1行 + `import math`）。docstring に「JSON の `1e999` は `inf` としてパースされ `inf > 0` は True になるため明示的に弾く必要がある」旨を記載（後から「冗長」として削られないため）。テストのパラメトリクスに `inf` / `-inf` / `nan` を追加（後2つは既に弾かれるが、条件変更時の退行検出のため）
- 参照: CWE-704（Incorrect Type Conversion or Cast）

#### 申し送り6点の評価（すべて実装者の判断が正しいと確認された）

**1. DoS 耐性 — 問題なし**

`_focus_ts` のエントリ数は `_clients` と1:1対応。新規キーの書き込みは `_handle_focus_notification()` と identify 転記のみで、いずれも `_clients` に登録済みのキーにしか書かない。

CPU 消費について: `_handle_focus_notification()` は完全に同期（`await` なし）で、処理は `isinstance` 3回 + `dict.__setitem__` 1回。メッセージループの `async for` が各フレーム読み取りで `await` するため、フレーム間でイベントループが他のコルーチンへ制御を渡す。**大量の `focus` 通知でも他クライアントの応答処理や HTTP 受理はブロックされない。**

**2. `focus` 通知が `_pending_response` に触れない設計 — 誤認経路なし**

コードパスを追跡した結果、`focus` 分岐から `return` するまでに `_pending_response` / `_response_event` へ到達する経路が構造的に存在しない。WebSocket のメッセージ順序保証により、通知とコマンド応答が入れ替わることもない。Phase 2a で「focus 通知を応答と誤認するより安全な故障モード」と評価した設計の正しい実装。

**3. `TypeGuard` の否定分岐 — 意図通り**

`TypeGuard[int | float]` は片方向保証だが、否定分岐は `return` で抜けるため `ts` を後続で使わない。True 分岐では `int | float` に絞り込まれるため `int()` が型安全。

**4. `focus_ts` の転記タイミング — 正しい**

`self._clients[key] = client_info` から `self._focus_ts[key] = client_info.focus_ts` までの間に **`await` が一切ない**ことを確認。asyncio のシングルスレッド保証により他コルーチンの割り込みがない。`_receive_identify()` は後着拒否の判定（`key` 確定）より前に呼ばれるため、そこで直接書かない設計は**拒否された接続の時刻が残ることを防いでいる**。

**5. 切断時のクリーンアップ位置 — 正しい**

`if self._clients.get(key) is client_info:` の内側に `pop` を置く分析が正確。拡張リロードで同じ `key` の新しい接続に置換された場合、`is` 比較が False になるため新しい接続の記録を消さない。

**6. フォーカス時刻の偽装 — 脅威モデル外として妥当**

偽装で得られるのは「`target` 省略時のコマンドが自分に届く」（情報窃取と応答偽装）。しかし前提条件が「同一マシン上のローカルプロセスが WebSocket に接続して identify を送れる」時点で、そのプロセスは既にローカルファイルアクセス等の遥かに強力な攻撃ベクトルを持つ。上限検証を入れても攻撃者は正当な範囲内で大きな値を送るだけであり、時刻ソースの検証はコスト対効果が見合わない。

#### Phase F3a のテストハーネス欠陥の影響評価

**結論: F3a / F3b の PASS 判定は撤回不要。ただし検出装置の強度評価は更新される。**

`extensions/tests/helpers/load-background.mjs` の sandbox に `URL` グローバルが不在だったため、`isAllowedOrigin()` 内の `new URL(url)` が `ReferenceError` → catch で `false` → **`connect()` が WebSocket 生成前に早期 return していた**。F3a 以降のテストでは `ws` が常に `null` だった。

しかし F3a のテスト群は以下のいずれも **`connect()` の成否に依存しない機能**のテストだった。

| テスト対象 | WebSocket が必要か |
|---|---|
| `detectBrowser()` | 不要（`navigator.userAgent` の解析） |
| `getOrCreateProfileId()` | 不要（`api.storage.local` の操作） |
| `getEmail()` | 不要（`api.identity` の呼び出し） |
| `storageGet()` / `storageSet()` | 不要（純粋な storage 操作） |

したがってテスト結果自体は正しい。また F3a で指摘した「`namespace: 'browser'` の検出装置は完全ではない」は `detectBrowser()` の名前空間検出の話であり、`connect()` 内部の挙動とは別の論点。

**F7 で `URL` を追加した結果、`connect()` が実際に `DummyWebSocket` を生成するようになり、`connect()` → `sendIdentify()` → API コールまでテスト可能になった。検出装置の観測能力が上がった。**

### 2026-07-25 レビュー（Phase F6）

`_format_ambiguous_clients_message()` と `_format_not_connected_message()` が `display_name` をエラーメッセージへ埋め込んでいたため、`label` 未設定のクライアントで email が平文露出していた問題への対処。

#### Critical / High / Medium / Low

**すべてなし。**

#### このレビューの位置づけ: 過去の判定が経路の変化で無効になった

**Phase 2a のレビューでは、この箇所を「問題なし」と判定していた。**

> 候補列挙エラーの `email`/`label` 露出は、`_resolve_client()` が `send_command()` 経由でしか呼ばれず認証後にしか到達しないため問題なし

当時は HTTP API のみの経路であり妥当な判定だった。Phase F5 で MCP ツールを整備した結果、同じ文字列が言語モデルの入力として取り込まれ、トランスクリプト・会話履歴・セッション共有先へ永続化される経路が生まれた。**認証の有無ではなく、認証後のデータがどこへ流れるかが変わった。**

Phase F5 の M-1（`list_clients` の email 露出）と合わせて、**同じ構造の問題が同日に2件**発生している。教訓として記録する。

> 「HTTP API では問題ない」という判定は、MCP 経路が追加された時点で前提が崩れる。認証境界の内側であっても、LLM のコンテキストへ入る経路は別の脅威として評価する必要がある。

#### 横断調査: 同種の判定残りはなかった

レビュアーが `server.py` 内の `.email` 実行時参照を AST 解析で洗い出した結果。

| 箇所 | 用途 | MCP 経由での到達 | 状態 |
|---|---|---|---|
| `display_name` property 内部 | label 未設定時のフォールバック | `list_clients` のみ | F5 で MCP 側が置換・除去 |
| `_match_by_identifier_order()` | 識別子の照合 | 照合のみで出力しない | 問題なし |
| `list_clients` のレスポンス | `email` フィールド | MCP 側が `delete` | F5 で対処済み |

`.display_name` の実行時アクセスは `list_clients` のレスポンス組み立て1箇所のみ。他は docstring / コメント内の言及。**同種の判定残りは見つからなかった。**

#### 重点検証の結果

**`label` を括弧表示する判断は安全**

`display_name` が email に自動フォールバックするのは**ユーザーが意図していない露出**。対して `label` はユーザーが `ck_label` に自分で書いた値であり、そこに email を入れるのは本人の明示的な選択。この線引きが妥当と評価された。

入力検証も通っている（`_validate_identity_value()` による印字可能文字制限・最大長・空白のみの拒否）。`label` にログインジェクション用の文字列を仕込んでも、下流でこのメッセージをパースする処理がないため表示が紛らわしくなるだけで実害はない。

**`_aliases_for_client()` を呼ばない設計（無限再帰の回避）は正しい**

レビュアーが実コードで呼び出しチェーンを確認。

```
_resolve_resolved_string() → _finalise_candidates() → _format_ambiguous_clients_message()
```

ここで `_aliases_for_client()` を呼ぶと、その内部で `_resolve_resolved_string()` が再び呼ばれる。**エイリアス値に裸のブラウザ名（例: `"chrome"`）が設定されていて複数プロファイルが接続していれば、即座に無限再帰が成立する**と具体的な条件まで示された。

設定ファイルからの直接逆引きを却下した理由（「一意解決されるエイリアスのみ」という正確性が失われ、曖昧なエイリアスを候補として示すと誤誘導になる）も妥当と評価。

**新設テストは退行検出装置として機能する**

`"@" not in result["message"]` という形のアサーションが3件（ユニット1・統合2）。将来メッセージ形式を変えても email が再流入すれば落ちる。Phase 0 の `xfail(strict=True)` と同じ思想。

`_format_ambiguous_clients_message()` の呼び出し元3箇所のうち、統合テストは2箇所を間接的にカバーしている。残る1箇所（同一ブラウザの複数プロファイル）は直接テストされていないが、同じヘルパーを経由するためユニットテストが退行を検出する。実害のある抜けではないと評価。

**仕様書の矛盾に対する実装者の解釈が妥当**

私の仕様書のテスト観点に「エイリアス名が含まれること」と書いてあり、本文の「エイリアスは一切含めない」と矛盾していた（方針変更時の更新漏れ）。実装者は「エイリアス」を「label」の言い間違いと解釈して実装した。レビュアーはこの解釈を妥当と判定した。

#### レビュー後に対応した申し送り

**`display_name` の docstring が実態と乖離していた**（レビュアーは「次フェーズでよい」としたが、`server.py` が未コミットの時点で対応した）

「list_clientsコマンドのレスポンスと、_resolve_clientが複数候補をエラーメッセージに列挙する際の両方で使う」という記述が、F6 の変更で嘘になっていた。用途が `list_clients` のみであること、エラーメッセージでは使わない理由（PII の永続化）、`list_clients` で使い続ける理由（HTTP API の後方互換と MCP 側の対処）を明記する形に更新した。

W2（コメントと実装の乖離）は本プロジェクトで繰り返し発生しており、放置すると後から読む人が誤った前提でコードを書く。

### 2026-07-25 レビュー（Phase F5）

MCP ツールから `target`（エイリアス）で対象プロファイルを指定できるようにした。`registerTool()` への移行と、起動時に接続中クライアントのエイリアスを description へ反映する機構を含む。

#### Critical / High

**なし**

#### Medium（対応済み）

**M-1: `list_clients` の MCP レスポンスに `email`（PII）が平文で含まれていた**

- 場所: `mcp_bridge.mjs` の `list_clients` ツールハンドラ
- 内容: サーバーのレスポンスをそのまま `JSON.stringify` して返していたため、`email` が MCP クライアントへ渡っていた

**この指摘は設計仕様の誤りに起因する。** 仕様書に「`email` は既に HTTP API が返している情報でありローカルツールの脅威モデル内なのでマスクしない」と書いたが、**経路の変化を考慮していなかった**。

| 経路 | 性質 |
|---|---|
| HTTP API（`curl`） | 人間がターミナルで見て終わり。揮発性が高い |
| **MCP 経由** | **LLM の入力として取り込まれ永続化される**（Claude Code のトランスクリプト、JSONL 生ログ、会話履歴、セッション共有時の第三者露出） |

Security-Guidelines.md S4（ログにPIIを含めない・マスクする）と S15（プロンプト・コンテキストにPIIを含めない）に反する。`list_clients` の用途は「`target` に渡せる名前を知ること」であり `email` は不要。

- 対応: MCP のレスポンスからのみ `email` を除去。`server.py` の HTTP API は後方互換のため無変更。**あわせて `displayName` も処理した**（`ClientInfo.display_name` は `label > email > profileId > browser` の優先順で決まるため、`label` 未設定のクライアントでは `displayName` が email そのものになる。`email` だけ消しても `displayName` から漏れる）。実装者が `displayName === email` を検出して `profileId` 先頭8文字へ置換する形にした
- 検証: MCP クライアントとして実接続し、`email` フィールドの不在と `@` を含む値が0件であることを確認。`key` / `aliases` は引き続き返る（`target` 選択に必要）

#### 重点検証の結果

**`@hono/node-server` の moderate は3重に到達不可能**

レビュアーが実行パスを追跡した結果。

| チェックポイント | 結果 |
|---|---|
| `mcp_bridge.mjs` のインポート | `StdioServerTransport` のみ |
| `McpServer` が `streamableHttp.js` をインポートするか | しない（grep 0件） |
| `streamableHttp.js` が `serve-static` を使うか | 使わない（`getRequestListener` のみ） |
| ランタイム実測 | `StdioServerTransport` のインポート時に `streamableHttp.js` はロードされない |
| OS | Linux（脆弱性は Windows 限定） |

**`RegisteredTool` ハンドル保持に問題なし**

ハンドルはモジュールスコープの `const` で、モジュールは `export` を持たない。ESM のモジュールバインディングは読み取り専用であり、外部から `update()` / `enable()` / `disable()` を呼ぶ経路がない。`list_clients` のハンドルは保持していない（description 更新の対象外）点も正しい設計と評価された。

**`target` の検証をサーバーに委ねた判断は妥当**

MCP 側の `max(256)` と、サーバー側の `_validate_command`（型・長さ・`browser` との排他）で二重に検証される。MCP 側で排他制約を表現しないことによる攻撃面の拡大はない（サーバーが到達前に拒否する）。

**description への動的反映で漏れる情報の範囲**

エイリアス名のみを追記する設計（`email` / `profileId` は含めない）。エイリアスはユーザー自身が設定した名前で PII ではないが、業務用途を示す語が含まれうる。レビュアーの評価は「Low / 情報提供程度」。README に挙動を記載済み。

#### このレビューを契機に発見された新規項目

**P0-5: 曖昧解決エラーが email を漏らす（ISSUES.md に登録）**

M-1 の修正作業中に実装者が発見した。`_format_ambiguous_clients_message()` が `display_name` をエラーメッセージに埋め込んでおり、`label` 未設定のクライアントでは email が平文で出る。`target` 省略時に複数クライアントが接続していると、**5ツールすべてのエラーパス**から漏れる。

MCP クライアントとして実接続して再現を確認済み。

**Phase 2a のレビューではこの箇所を「問題なし」と判定していた**（「認証後にしか到達しないため問題なし」）。当時は HTTP API のみの経路だったため妥当だったが、Phase F5 で MCP 経路が整備されたことで前提が崩れた。**M-1 と完全に同じ構造**であり、過去の判定が経路の変化によって無効になる例として記録する。

#### 継続中の指摘

| 指摘 | 状態 |
|---|---|
| L-2（`mcp_bridge.mjs` の `res.json()` が SyntaxError を未キャッチ） | **未修正**。Phase 1 で「次フェーズ」としたが F5 でもスコープ外だった。次に `mcp_bridge.mjs` を触る際に対応する |
| `@hono/node-server` の moderate | 到達不可能を再確認。SDK の依存更新待ち |

### 2026-07-25 レビュー（Phase F3b）

Firefox 向けの拡張ディレクトリを新設し、`background.js` / `content.js` を Chrome / Edge 版とバイト単位で同一に保つ構成にした。差分は `manifest.json` のみ。

#### Critical / High / Medium / Low

**すべてなし。** 全フェーズを通じて初めて指摘0件。

#### 検証された事項

**Firefox 版 manifest の CSP は既定より厳しい**

```
指定値 : script-src 'self'; object-src 'self'; connect-src 'self' ws://127.0.0.1:9765
既定値 : script-src 'self' 'wasm-unsafe-eval'
```

Firefox MV3 の既定 CSP は `wasm-unsafe-eval` を含むが、今回の指定は含まない。`object-src 'self'` も明示的な制限であり、**既定を上書きして弱めてはいない**。

`connect-src` を既定ポート 9765 に固定した判断も安全側と評価された。ワイルドカード（`ws://127.0.0.1:*`）は MDN に MV3 の `connect-src` での挙動が明記されていないため、確実に動く形を選ぶのが正解。カスタムポート運用時の制約は README / SPEC.md / `docs/firefox-setup.md` の3箇所にドキュメント化されている。

**`identity` 権限を除いた際の `getEmail()` の経路が正しい**

Firefox では `browser.identity` 自体は存在するが `getProfileUserInfo` メソッドが存在しない。`getEmail()` の存在チェック `typeof api.identity.getProfileUserInfo !== 'function'` が true になり、即座に `null` を返す。権限の有無だけで挙動が決まる設計が意図どおり機能する。

**`gecko.id` を公開リポジトリに固定値で書くことは問題ない**

拡張IDは秘匿情報ではない。AMO で公開される全拡張のIDは公開されるし、self-hosted でも manifest に含まれる。`uuidgen` で新規生成して旧 FirefoxKontrol と衝突させない判断も妥当。

**M-1 の `prototype` キーは弾く必要がない（実装者の判断が正しかった）**

実装者は `__proto__` と `constructor` をガードし、`prototype` は対象外とした上で「`Object.entries()` 経由だと `prototype` が列挙されるケースは基本ない」と申し送りしていた。レビュアーの分析:

`mergeDeep` の `result` は `{ ...base }` で作られたプレーンオブジェクト。`result['prototype']` は own property として存在しないため `isPlainObject(undefined)` が false になり、else 分岐で `result.prototype = {...}` が設定される。これは **own property の設定であり `[[Prototype]]` スロットは変わらない**。プロトタイプ汚染が成立するのは `__proto__` 経由（ブロック済み）か `constructor.prototype` 経由（`constructor` もブロック済み）のみ。

**同一性テストの検出装置に穴がない**

攻撃者視点での検証:
- `manifest.json` に悪意のある js ファイルを追加 → ファイル構成テストが「firefox にのみ存在するファイル」として検出
- `manifest.json` の `background.scripts` を書き換えて既存ファイルを別用途で読み込む → コード自体はバイト同一なので実行内容は同じ。検出不要の範疇
- `SHARED_FILES` の手動管理リストが未更新でも、ファイル構成テストが片方だけの追加を拾う。両方に追加してリスト未更新のケースのみ見逃すが、それは「意図的に両方へ追加した＝同一であるべき新ファイル」であり、テスト更新が自然な流れになる

**スコープ外修正の監査結果: 適切**

実装者が仕様書外の判断で `README.md` の `"browser"` フィールド説明2箇所に `firefox` を追加した件。Phase F2 で `ALLOWED_BROWSERS` に追加済みなのにドキュメントが追従していなかった分の修正であり、技術的に正確。

**MCP連携セクションを `chrome` / `edge` のまま残した判断も適切**と評価された。`mcp_bridge.mjs` の Zod スキーマが `z.enum(["chrome", "edge"])` で5箇所固定されており firefox を受け付けないため、ドキュメントだけ先に firefox を書くと嘘になる。ISSUES.md P0-4 として登録済みの既知問題。

#### 将来の改善候補（今回は対応不要）

**ESLint の `no-restricted-globals` に `chrome` を追加する**

Firefox では `chrome` グローバルも存在するため、将来 `chrome.` の直接参照が混入した場合、ReferenceError にならずコールバック形式の API が Promise として扱われて**静かに壊れる**失敗モードになる。

現状の対策: テストハーネスの `namespace: 'browser'` モードが `chrome` を未定義にするため、混入すれば ReferenceError で即死する。加えてコード上の `chrome.` 実行時参照は0件（コメント内4件のみ）。

静的に防ぐなら ESLint の `no-restricted-globals` が最もコストが低い（設定1行）。実行時検出（`Object.freeze(chrome)` 等）は副作用が大きく価値がないとの評価。

**`@hono/node-server` の moderate 2件**（唯一の減点要因）

Windows 固有のパストラバーサルで Linux では到達不可能。Phase F3b で依存を追加していないため既存問題。`@modelcontextprotocol/sdk` の更新で解決する可能性があるが breaking change の確認が必要。

### 2026-07-25 レビュー（Phase F3a）

`background.js` を Chrome / Edge / Firefox で完全同一のファイルにするための書き換え。名前空間を `api` に一本化し、コールバック形式の呼び出し4箇所を Promise ベースへ統一した。**動作している Chrome / Edge 向けコードの書き換え**であり、`background.js` にはこれまでテストが1件も存在しなかったため、挙動の同等性が最大の焦点。

#### Critical / High

**なし**

#### Medium（Phase F3b で対応）

**M-1: テストハーネスの `mergeDeep()` に `__proto__` / `constructor` のガードがない**

- 場所: `extensions/tests/helpers/load-background.mjs` のモック部分上書き関数
- 内容: `Object.entries(overrides)` でキーを走査する際に危険なキーをフィルタしていない。`JSON.parse('{"__proto__":{...}}')` の結果を渡すと `__proto__` の setter が発火してオブジェクトのプロトタイプが差し替わる
- 到達性: **現状は到達不可能**。`apiOverrides` はテストコード内のリテラルオブジェクトからしか渡されないため、`JSON.parse` 経由の own-property としての `__proto__` は発生しない。グローバルな `Object.prototype` 汚染にもならず、per-object のプロトタイプ差し替えに留まる。ただしテストが外部フィクスチャを `JSON.parse` して渡す形に拡張された瞬間に発火する
- 影響範囲: **テストハーネス限定**。プロダクションコード（`background.js`）には該当なし。`executeCommand` 内の `Object.create(null)` による prototype pollution 対策は維持されている
- 対応: ループ冒頭で `if (key === '__proto__' || key === 'constructor') continue;` を追加（1行）。`extensions/tests/` を触る Phase F3b でまとめて対応する

#### 重点検証の結果

**`api` の解決判定に穴がない**

`const api = typeof browser !== 'undefined' && browser.runtime ? browser : chrome;`

- `background.js` は Service Worker（Chrome MV3）または event page（Firefox）で動作する。**ページ側のスクリプトが Service Worker のグローバルスコープに `browser` を注入する手段は存在しない**。他の拡張からもアクセスできない
- `typeof` チェックと `browser.runtime` の二段構成により、`browser` が `null` / `0` / `false` / `runtime` を持たないオブジェクトのいずれの場合も `chrome` に落ちる
- Firefox 実機では `chrome` と `browser` が両方存在し、`browser.runtime` が truthy なので `api = browser` に解決される（正しい挙動）

**`getPort()` の `try/catch` はエラーを飲み込みすぎていない**

既存のコールバック版は `chrome.runtime.lastError` を**一切検査していなかった**。値が取れなければエラーの有無を問わず `DEFAULT_WS_PORT` で resolve していた。書き換え後の `catch` は同じ挙動を Promise の世界で再現しているだけで、error path の意味は変わっていない。

ポートの用途は WebSocket の接続先で `isAllowedOrigin()` の localhost 検証を通る。最悪のケースは「接続先ポートが違って接続失敗 → 再接続ループ」であり既存と同一。storage の恒久的な破損はここで検知しても対処アクションがない。

**`detectBrowser()` の `userAgent` 判定に偽装経路がない**

- Service Worker の `navigator.userAgent` はブラウザが設定する値であり、ページのスクリプトや他の拡張から書き換えられない
- `includes('Firefox/')` は十分に specific。Chromium 系（Chrome / Edge / Brave / Opera）の UA 文字列に `Firefox/` は含まれない
- 戻り値はサーバー側の `ALLOWED_BROWSERS` で二重に検証される。`'unknown'` は許可リストに含まれないため拒否される

**テストハーネスの設計**

- `EXPORT_EPILOGUE` の副作用は**ゼロ**。`globalThis.__ck_test_exports__` への代入のみで、`api` と `DEFAULT_WS_PORT` は `const` 宣言のため不変。エピローグは `background.js` の全トップレベルコード（`connect()` / `registerKeepaliveAlarm()` を含む）の実行後に走る
- **`namespace: 'browser'` の検出装置は機能するが完全ではない**というレビュアーの指摘: `vm.runInContext` 時に走る `connect()` の内部は全て `try/catch` で囲まれているため、仮に `chrome.xxx` の書き換え漏れがあっても ReferenceError が握りつぶされてテストが通る可能性がある。`storageGet` / `storageSet` は `namespace: 'browser'` で直接呼ばれるため検出できるが、`getPort` / `getEmail` / `detectBrowser` はこの構成で直接テストされていない。ただし実コード中の `chrome.` が0件であることを grep で確認済みのため実害はない（網の目の話）

**実装者が「変更しない」と判断した2箇所はいずれも妥当**

- ISSUES.md P1-2 の歴史的引用として残した `chrome.tabs.query({ currentWindow: true })` — Chrome API の仕様上の問題点を説明するコメントであり、`api.tabs.query` に書き換えると何の API の話か不明確になる
- JSDoc の `@param {chrome.alarms.Alarm}` — `chrome.alarms.Alarm` は型定義に実在する型名。`api` はランタイムのローカル変数で型名前空間に存在しないため、書き換えると型チェックツールが壊れる

#### 次フェーズ以降への申し送り（優先度は低い）

- `getPort()` の `catch` ブロックにログ出力がない。storage の永続エラーをデバッグする際に手がかりが残らない。**F3a 以前からの既存債務**であり今回の回帰ではない
- `getEmail()` のエラーログ文言 `via api.identity:` は、`api` が内部の変数名のため利用者には意味が伝わりにくい。`via identity API:` 等の方が親切（cosmetic）

### 2026-07-25 レビュー（Phase F2）

サーバーが `firefox` を名乗るクライアントを受理できるようにした変更。`ALLOWED_BROWSERS` への追加、`_is_allowed_origin()` での `moz-extension://` 許可、自動起動の許可リストに Firefox を**登録しない**判断、およびそれに伴う `KeyError` 修正が対象。

#### Critical / High

**なし**

#### Medium（いずれも対応済み）

**M-1: ユーザー向けメッセージの "Chrome or Edge" ハードコードが不正確になった**

- 場所: `server.py` のタイムアウト案内および常駐起動時のログ
- 内容: `ALLOWED_BROWSERS` に `firefox` を追加したにもかかわらず、利用者に示すメッセージが `Chrome or Edge` のままだった。Firefox の拡張が接続を試みている状況で誤誘導になる。攻撃シナリオは存在せず情報露出でもないが、誤ったエラーメッセージは障害対応を遅延させる（ASVS V12）
- 対応: 文字列にブラウザ名を直接書くのをやめ、`ALLOWED_BROWSERS` から列挙文を組み立てるヘルパー `_format_browser_list()` を新設した。`sorted()` で順序を安定させ、出力が実行ごとに変わらないことをテストで固定している。将来ブラウザを追加した際に修正漏れが起きない構造になった

**M-2: docstring 内の chrome / edge 限定の例示**

- 場所: モジュール docstring、`ChromeKontrolServer` クラス docstring、`send_command` の引数 docstring ほか
- 内容: `"chrome"` / `"edge"` を決め打ちで列挙していた箇所が、`firefox` 追加後は不正確になった。開発者が docstring を信じて「firefox は来ない」前提のコードを書くリスクがある
- 対応: `firefox` を含める、または `ALLOWED_BROWSERS` を参照する記述に更新した。指摘された4箇所に加え、実装者が同種の2箇所（`ClientInfo.browser` と `_resolve_browser_executable()`）も自主的に更新している

#### 重点検証の結果

**`moz-extension://` のプレフィックス一致は追加の攻撃面を生まない**

レビュアーの結論は3点。(1) サーバーは 127.0.0.1 にのみバインドしており到達範囲がローカルプロセスに限定される、(2) Origin ヘッダーなしの接続が CLI ツール用に既に許可されているため `moz-extension://` を偽装しても得られるアクセスに差がない、(3) `moz-extension://` スキームはブラウザが強制するため Web ページからの偽装は不可能。

なお Firefox の Origin は `moz-extension://<internal-uuid>` の形式で、UUID はインストールごとにランダム生成され事前に判明しない。完全一致による検証は原理的に不可能であり、プレフィックス一致が唯一の手段である。

**`firefox` 追加で新たに到達可能になるコードパスの網羅確認**

`BROWSER_EXECUTABLE_CANDIDATES[browser]` の `KeyError` と同種の「chrome / edge の両方がキーを持つ前提」に依存した箇所が他にないか、以下の経路をすべて追跡した。

| 経路 | 結果 |
|---|---|
| `_validate_command()` | `ALLOWED_BROWSERS` 内なので通過。後続は既存の解決経路で安全 |
| `_receive_identify()` | 入力検証（サイズ・JSON・印字可能文字・許可リスト・profileId・email・label）がブラウザ名に依存しない設計。Firefox でも同一の検証強度 |
| `_match_candidates_for_resolved()` | 既知ブラウザとして空リストを返す。エラー dict にはならない |
| `_auto_launch_response()` | `.get()` で空タプル → 事前 return。`subprocess.Popen` への到達は不可能 |
| `_wait_for_client()` | ブラウザ名の単純比較のみ |
| クライアント解決のメッセージ生成 | `capitalize()` が `Firefox` を正しく生成 |

`BROWSER_EXECUTABLE_CANDIDATES` への直接キーアクセスが他に残っていないことも確認済み。

**エラーメッセージからの情報露出（実装者の申し送り）**

自動起動の非対応を伝えるメッセージから「どのブラウザが自動起動に対応しているか」が読み取れる点について、問題なしと判定。(1) 呼び出し元がブラウザ名を指定している以上その情報は既知、(2) `SPEC.md` に自動起動の対応ブラウザが明記されている、(3) 127.0.0.1 限定かつトークン認証済みの呼び出し元にしか返らない。

**テストの `"firefox"` → `"safari"` 差し替え**

「未知のブラウザの拒否」を検証する既存テストが `"firefox"` を例に使っていたため、`firefox` の許可後は検証の意味が失われる。`"safari"` への差し替えはテストの意図を保っており、各テストに差し替えの理由が docstring として記録されている点も適切と評価された。

#### レビュー対象外だが記録すべき事項

**KeyError 修正の有効性を独立に検証した**

修正を一時的に旧コードへ戻して全テストを実行したところ、意図した3件が `KeyError: 'firefox'` で失敗した。テストが実際に欠陥を検出できることを確認したうえで復元している（テストが通ることと、テストが欠陥を捕まえられることは別の性質であるため）。

### 2026-07-25 レビュー（Phase 3a）

#### Critical / High / Medium

**なし**（全フェーズを通じて初めて Medium もゼロ）

#### Low

**L-11: `identity` 権限が最小権限の原則に照らして過剰な可能性** → 実機確認待ち

- 場所: `manifest.json` の `permissions`
- 内容: `getProfileUserInfo()` は公式ドキュメントで `identity.email` を要求すると明記されているが、`identity` 単独の必要性は不明確。`identity` 権限は `chrome.identity.launchWebAuthFlow()`（任意の OAuth 認証フローの起動）へのアクセスも開く
- 実害の文脈: この拡張は既に `<all_urls>` + `scripting` を持ち、`chrome.scripting.executeScript` で任意のページにスクリプトを注入できる。`identity` の追加による実質的なリスク増加はほぼゼロ。拡張のソースを書き換えられる前提は脅威モデル外（同一ユーザー権限 = ホームディレクトリに書き込み可能 = 拡張なしでも任意実行可能）
- 対応: **実機確認が必要なため `docs/phase3a-verification.md` の手順 2-1 に検証手順を追記した**。`identity` を削除して3プロファイルで再読み込みし、`email` が引き続き取得できるかを確認する。取得できれば削除を採用、できなければ現状維持。`getEmail()` の失敗時は `email` フィールドを省略して接続を維持する設計のため、この検証で拡張が壊れることはない

**L-12 / P2-14: `ensureIdentifyPayload()` にタイムアウトがない** → Phase 3c 送り

- 内容: `chrome.identity.getProfileUserInfo()` がハングした場合、`connect()` 全体がブロックされ再接続タイマーもキープアライブアラームも機能しなくなる
- 実害の文脈: 可用性の問題でセキュリティ上の実害はない。`chrome.identity` は Chrome 内部 API のため外部からハングを誘発する手段がない。理論上は Chrome の不具合やプロファイル破損時のみ
- 対応方針: 各非同期取得に `Promise.race()` で個別タイムアウト（2秒目安）を設ける。サーバー側の identify タイムアウト3秒に余裕を持たせるため

**L-13 / P2-15: identify キャッシュが `ck_label` の変更を反映しない** → Phase 3c 送り

- 内容: `cachedIdentifyPayload` は Service Worker の生存期間中に1回だけ構築される。`chrome.storage.local.set({ ck_label: ... })` で変更しても、再接続では古い値が送られる
- 実害の文脈: Phase 3a の時点では `label` の設定手段が DevTools からの暫定操作のみで利用頻度が低い
- 対応方針: Phase 3c で `chrome.storage.onChanged` を購読し、`ck_label` の変更時にキャッシュを破棄する

---

## Phase 3a の個別検証結果

### 情報の取り扱い

| 検証項目 | 結論 |
|---|---|
| `email` を `chrome.storage.local` に保存していないか | **保存していない**。`storageSet()` の呼び出しは `profileId` の1箇所のみ。`email` は毎回取得し、キャッシュはモジュールスコープ変数（Service Worker 停止で消滅） |
| `profileId` / `email` / `label` の平文送信 | 接続先は `ws://127.0.0.1` に限定。Security-Guidelines.md の「localhost 開発を超える場合は `wss://` 必須」に適合。`email` は実 PII だが到達先はローカルサーバーのみで、サーバーログには値ではなくキー形式（`browser:profileId`）が記録される |
| ログ出力に値が含まれないか | **含まれない**。`email` / `label` は有無（yes/no）のみ出力。`browser` は `detectBrowser()` の固定値（`chrome`/`edge`/`unknown`）で PII ではない |
| `profileId` のクロスプロファイル関連付け | **構造的に不可能**。v4 UUID は 122 ビットのランダム。プロファイルごとに独立した `chrome.storage.local`（プロファイルスコープ）に保存される |

### P1-2 修正の検証

| 検証項目 | 結論 |
|---|---|
| `getLastFocused()` が意図どおり動作するか | `populate: true` でタブ配列を取得し `win.tabs.find(t => t.active)` でアクティブタブを特定。`windowTypes: ['normal']` で DevTools / ポップアップ / パネルを除外 |
| `windowTypes: ['normal']` による除外の影響 | PWA ウィンドウ（`type: 'app'`）がフォーカス中の場合は該当タブが見つからずフォールバックに進む。機能上の制約であり新たなセキュリティ上の穴ではない |
| フォールバック経路 | `getLastFocused()` が例外を投げた場合も従来の `currentWindow: true` クエリにフォールバックし、`console.warn` でログを残す。正しい |
| タブ URL 検証の継続 | `if (!tab.url || !tab.url.startsWith('http'))` は両経路で通る |

### 実装者の申し送りへの回答

| 申し送り | 回答 |
|---|---|
| ログ出力を email / label の有無（yes/no）にした判断 | **十分**。デバッグ時に値が必要なら `list_clients`（認証済み API）で確認すればよい |
| `getOrCreateProfileId()` の排他制御なしの判断 | **正しい**。Chrome の Service Worker は1プロファイルに1インスタンスしか同時起動しない（拡張アーキテクチャの基本設計）。競合は物理的に発生しない。仮に発生しても UUID が1回変わるだけで、サーバー側は新しいキーで再登録する。Web Locks API のコストに見合わない |
| `accountStatus: 'ANY'` の判断 | **問題なし**。(1) `'SYNC'` だと同期無効なプロファイルで常に空文字列が返り P0-1 の目的を達成できない、(2) 送信先は `ws://127.0.0.1` のみでネットワーク越しに外部へ送られる経路がない、(3) サーバーログに email 値は記録されない、(4) `list_clients` の応答に含まれるが認証済みリクエストでのみ到達可能、(5) この email は Chrome プロファイル上でユーザーが既に紐付けた情報であり、同一マシン上のローカルサーバーへの伝達は新規の情報露出にあたらない |

### 手順書の検証（公開リポジトリに含まれる）

| 検証項目 | 結論 |
|---|---|
| 環境詳細の過度な露出 | なし。実メールアドレスは使わず `example.com` を使用。プロファイル名は一般的な `Default` / `Profile 1`。トークンファイルのパスは SPEC.md で公開済みの情報 |
| ロールバック手順の正しさ | 正しい。`git checkout HEAD~1 -- background.js manifest.json` は対象ファイルのみを戻す。サーバー側のロールバックが不要である旨も正確 |
| DevTools からの `ck_label` 設定を案内するリスク | なし。Service Worker の DevTools はローカルの拡張管理画面からのみアクセス可能で、リモートからの操作経路がない。設定値はサーバー側 `_is_valid_identify_field()` で検証される |

---

### 2026-07-25 レビュー（Phase 2c）

#### Critical / High

なし

#### Medium

**M-6: systemd `KillMode` の既定値がブラウザの巻き添え終了とゾンビ蓄積を招く** → **対応済み（2026-07-25）**

- 場所: `systemd/chromekontrol.service` の `[Service]` セクション（`KillMode` 未指定 = 既定 `control-group`）、および `server.py` の `subprocess.Popen`（戻り値を破棄し `wait()` を呼ばない）
- 内容: 2つの問題が同根。`Popen` の戻り値を破棄して `wait()` しないため、(a) ブラウザプロセスが先に終了するとゾンビが残る、(b) ブラウザがサーバーの子プロセスとして cgroup 内に入るため `systemctl --user restart` でブラウザごと終了する
- 実害の文脈: モード別。Chrome が既に起動している通常運用では、`Popen` した子プロセスが既存インスタンスに通知して数秒で exit するため**実際にゾンビが発生する**。ブラウザの巻き添え終了は Chrome が一切起動していない状態から自動起動した場合にのみ顕在化するが、発生すると「ブラウザのタブ全損」という被害になる
- 対応:
  - systemd 側: 両方の unit ファイル（リポジトリ内テンプレートと実配置）に `KillMode=process` を追加。`systemd-analyze --user verify` が exit 0、`systemctl --user show -p KillMode` で実効値が `process` であることを確認。再起動後も `active` で疎通も確認済み
  - Python 側: `_ignore_sigchld_for_auto_launched_children()` を新設し、`run_serve_mode()` の冒頭で `signal.signal(signal.SIGCHLD, signal.SIG_IGN)` を1回だけ設定。Linux ではこれにより子プロセスが自動で reap される。`hasattr(signal, 'SIGCHLD')` でガード
  - 検証: 稼働中サーバーの `/proc/<pid>/status` の `SigIgn` ビットマスクを直接読み、SIGCHLD（シグナル番号 17）が無視設定になっていることを確認
- 設計判断: `Popen` オブジェクトを保持して `poll()` する方式は採らない。ブラウザの寿命管理は ChromeKontrol の責務ではないため
- 前提条件の確認: このプロジェクトは `asyncio.create_subprocess_exec()` / `create_subprocess_shell()` を使用していない（`grep` で確認済み）。asyncio の child watcher はこれらの API が初回呼び出しされた時点で遅延初期化されるため、`SIGCHLD` を `SIG_IGN` にしても衝突しない

**M-7: systemd unit のコメントが実装と食い違っていた** → **対応済み（2026-07-25）**

- 場所: `systemd/chromekontrol.service` の `NoNewPrivileges` に付随するコメント
- 内容: Phase 1 の M-3 対応時に書いた「`server.py` は現時点でサブプロセスを起動しないが、将来コマンドを拡張した際の防御の深層として設定する」が、Phase 2c で実際にブラウザプロセスを起動するようになったため事実に反する状態になった
- 実害の文脈: 絶対的。セキュリティ設定のコメントが実装と食い違っているのは、次にこのファイルを触る者を誤らせる
- 対応: 実態（`subprocess.Popen()` でブラウザプロセスを起動しており、`NoNewPrivileges` が起動されたブラウザの特権昇格経路を実際に塞いでいる）に合わせて書き換えた

#### Low

**L-7: `_launching` セットが現状デッドステート** → 残す方針（コメント強化で対応）

- 内容: `_command_lock` による直列化のため `_launching` は常に空か要素1つで、実質的に機能していない
- レビュアー判定: 「セキュリティリスクはなし。削除してもいいが、残しても害はない（`set` の add/discard 2回のコスト）。残すなら冗長性をコメントで明示すべき」
- 対応: **残す**。Phase 4 で `reqId` 相関を導入する際にロック設計を見直す予定があり、その時点で必要になる想定。コメントに冗長性が意図的であることを明示する NOTE を追加した
- **この指摘の原因は設計仕様にある**。`_command_lock` により競合が構造的に起きないことを認識しながら「記録と解除の順序は仕様に従って」と指示したため、意味を持たない状態変数が生まれた

**L-8 / P2-13: `_launch_attempts` にクリーンアップ機構がない** → Phase 4 送り

- 内容: エントリが追加されるのみで削除されない
- 実害の文脈: なし。キーは `profiles` のキーと同一形式で `MAX_ALIAS_COUNT`（1000件）に実質制限され、メモリ影響は数十 KB
- 対応方針: Phase 4 でロック設計と併せて見直す

**L-9: 条件チェック順序による設定状態の推測** → 対応不要

- 内容: 「自動起動が無効」「`profiles` に未登録」「クールダウン中」等のエラーメッセージが区別できる
- 検証結果: **脅威モデル外**。このメソッドへの到達経路は `_handle_http_request()` → `secrets.compare_digest()` によるトークン認証 → `send_command()` → `_resolve_client()` → `_auto_launch_response()` のみ。トークンを持つ者は同一ユーザー権限を持つことと同義で、`config.json`（0600）を直接読める。エラーメッセージ経由の推測に意味がない

**L-10: 環境変数の丸ごと継承** → 対応不要

- 内容: `Popen` に `env=` を指定していないため、`CHROME_KONTROL_TOKEN` を含む親プロセスの環境変数がブラウザに継承される
- 検証結果: **脅威モデル外**。ブラウザは同一ユーザー権限で動作し、そもそも `~/.config/chromekontrol/token`（0600）を直接読める。このトークンはローカル CSRF 対策であり外部サービスの認証情報ではない。`env=` で明示指定すると `PATH` 等も自前で構成する必要があり、コストが見合わない

#### 設計者の判断の誤りが訂正された項目

**先頭ハイフンチェックは「現状不要」ではなく実際に機能している**

Phase 2c の仕様書で「先頭が `-` でないこと ── 配列渡しなので現状は引数として解釈されず不要だが、防御の深層として設ける」と指示し、その旨をコード内コメントに書かせた。しかしレビュアーの分析により、これが誤りと判明した。

```
"-rf" → 文字種チェック: 通過する（'-' は _PROFILE_DIR_ALLOWED_EXTRA_CHARS に含まれる）
      → 先頭ハイフンチェック: ここで初めて弾かれる
```

`-` は許可文字に含まれるため文字種チェックを通過し、先頭ハイフンチェックが**実際に到達して防御として機能している**。

一方 `.` / `..` については当初の判断が正しく、`.` は `isalnum()` が False かつ許可文字に含まれないため文字種チェックの段階で弾かれ、到達しない。

M-7 と同種の「コメントが実装と食い違う」問題として、コメントを実態に合わせて訂正した。

---

## Phase 2c の個別検証結果

レビュアーが検証した項目と結論。

### プロセス起動の安全性

| 観点 | 結論 |
|---|---|
| 許可リスト方式で任意コマンド実行が防げているか | **防げている**。`BROWSER_EXECUTABLE_CANDIDATES` はモジュールレベル定数で設定ファイルからの上書き経路がない。`_resolve_browser_executable()` はこの定数のみを参照し外部入力を受け取らない |
| `shutil.which()` が PATH を参照するリスク | **脅威モデル外**。PATH 先頭に悪意あるディレクトリを追加するには同一ユーザー権限が必要。実環境では `/usr/bin/google-chrome` → symlink → `/opt/google/chrome/google-chrome` で正常に解決される |
| 引数インジェクション | **構造的に不可能**。`shell=False` + 配列渡しでシェルメタ文字は解釈されない。値は ASCII 英数字 + スペース + ハイフン + アンダースコアに制限済み。Chrome が `--profile-directory` の値をシェルに渡す既知の経路は存在しない |
| ゾンビプロセスの蓄積 | M-6 で指摘・対応済み |
| systemd `KillMode` | M-6 で指摘・対応済み |
| 環境変数の継承 | L-10。脅威モデル外 |

### 検証の十分性

| 観点 | 結論 |
|---|---|
| プロファイルディレクトリ名の検証に見落ちがないか | **ない**。`ch.isascii()` は U+0000〜U+007F のみ True を返し非 ASCII を確実に弾く。`ch.isalnum()` は ASCII 範囲では `[a-zA-Z0-9]` のみ True。追加許可文字は `frozenset` で明示列挙。非 ASCII 文字列が弾かれることをテストで直接確認済み |

### DoS / リソース

| 観点 | 結論 |
|---|---|
| ロック保持のまま最大30秒待つ設計 | 設計として認識済み・Phase 4 送り。(1) 自動起動は既定無効、(2) 有効化にはユーザーの明示的操作が必要、(3) 認証済みリクエストでのみ発動、(4) クールダウンで同一プロファイルは60秒制限。悪意ある DoS より「有効化したら全体が遅くなった」という運用上の驚きの方が現実的 |
| クールダウンの十分性 | **十分**。異なるプロファイルを次々に指定するには `profiles` への登録が必要（上限1000件）。1000プロファイル分の `config.json` と認証トークンの両方を持つ攻撃者は脅威モデル外 |

### リファクタの安全性

| 観点 | 結論 |
|---|---|
| `_read_config_object()` への委譲 | **挙動変更なし**。以前は `is_file()` → `read_text()` → `json.loads()` → `isinstance(data, dict)` を自前で実施していた処理を共通化したもの。失敗時に空 dict を返すため `data.get('aliases')` は `None` を返し、結果として空 dict になる。ログ文言のみ差異。既存315テストの全 pass が証左 |
| `_match_candidates_for_resolved()` の抽出 | **コード移動のみ**。`_resolve_resolved_string()` の前半がそのまま抽出され、後半（`_finalise_candidates()`）は元の位置に残っている。既存39テストの全 pass と、抽出後のメソッドを直接検証する新規11テストで確認 |

### 脅威モデル

| 観点 | 結論 |
|---|---|
| SPEC.md の追記が実装と一致しているか | **一致している**。許可リスト・検証ルール・`shell=False`・DEVNULL・クールダウン秒数のすべてが実装と合致 |
| 「同一ユーザー権限の攻撃者は脅威モデル外」が外部プロセス起動後も妥当か | **妥当**。自動起動で攻撃者が得る能力は「登録済みのブラウザプロファイルのウィンドウを開く」だけ。同一ユーザー権限を持つ攻撃者はそもそも `google-chrome` を直接実行でき、`server.py` を書き換えることもできる。ChromeKontrol を経由することで能力が拡大する要素はない |

### bandit の Low 2件

| Finding | 結論 |
|---|---|
| B404（`import subprocess`） | informational。subprocess を使うなら import は必然でセキュリティ影響なし |
| B603（`subprocess_without_shell_equals_true`） | **正しく `shell=False`**。bandit の警告は「ユーザー入力が引数に含まれていないか確認せよ」の意であり、実装は許可リストから解決済みのパスと検証済みのディレクトリ名のみを使う。`# noqa: S603` の抑制理由がコメントに明記されている |

---

### 2026-07-25 レビュー（Phase 2b）

#### Critical / High

なし

#### Medium

**M-5: エイリアスエントリ数の上限がない** → **対応済み（2026-07-25）**

- 場所: `server.py` の `_load_aliases()` のループ
- 内容: キー・値は 1〜256 文字に制限されているが、エントリ数が無制限。巨大な `config.json` を読み込むとメモリを消費し、`_aliases_for_client()` が `list_clients` のたびに全エントリをイテレートするため `O(aliases × clients)` になる。N100 / 8GB 環境では 100万件 × 512バイト ≈ 500MB で OOM の可能性
- 実害の文脈: 環境固有。悪意あるローカルプロセスが `config.json` を書き換えられる前提は脅威モデル外（同一ユーザー権限の攻撃者にはより直接的な手段がある）。実運用では数十件が上限
- 対応: `MAX_ALIAS_COUNT = 1000` を定数として追加し、`_load_aliases()` のループ先頭で上限到達時に警告を出して `break` する。警告は `break` により自然に1回のみ。境界値（ちょうど 1000 件 / 1001 件 / 1050 件）のテストを4件追加

#### Low

**L-5 / P2-10: `_sanitise_for_log()` が Zs カテゴリを素通し** → Phase 5 送り

- 内容: U+00A0 NO-BREAK SPACE、U+3000 IDEOGRAPHIC SPACE、U+2000〜U+200A の各種スペースがフィルタを通過する。ログの視覚的整列が崩れ、`"chrome "` と `"chrome"` が目視で区別できなくなる
- 実害の文脈: モード別。これらは行区切りを生成しないためログエントリの偽造（CWE-117）には至らず、可読性の問題に留まる。`browser` フィールドは ASCII 制限 + 許可リストで到達しない
- 対応方針: 通常のスペース（U+0020）も `Zs` に属するため、単純に追加するとスペースが消える。`ch == ' '` を例外扱いする必要がある

**L-6 / P2-11: 設定ファイル読み込みがシンボリックリンクをフォローする** → Phase 5 送り

- 内容: `Path.is_file()` はシンボリックリンクを透過的にフォローする
- 検証結果: **ファイル内容の漏洩はない**。`/etc/shadow` 等を指した場合 `json.loads()` が失敗するが、`str(JSONDecodeError)` は位置情報のみを出力し `doc` 属性を含まないことを実測で確認済み。読み取り権限がなければ `OSError` で即座に失敗し、エラーメッセージもパス名のみ
- 実害の文脈: 脅威モデル外。優先度は最低

#### 実装者からの申し送り（スコープ内と判定）

`MAX_ALIAS_COUNT` は「有効なエントリ数」でカウントするため、無効エントリが多数混在する `config.json` では 1000 件の有効エントリに達するまで `raw_aliases` 全体をイテレートし続ける。メモリは制限できているが CPU 時間は制限していない。

ただし `json.loads()` の段階で既に全体がメモリに載っており、パースもイテレーションもともに `O(n)` であるため、オーダー上の追加リスクはない。真に制限するにはパース前のファイルサイズ上限が必要で、これは Phase 5 の検討事項とする。

---

## Phase 2a 指摘への対応確認（Phase 2b で完了）

| 指摘 | 対応状況 |
|---|---|
| **L-3**（空白のみの値が通過） | 完了。`not isinstance(value, str) or not value or not value.strip()` で拒否。`value.strip()` は検証のみに使い保存値は書き換えないため、意図的な前後空白は保持される |
| **L-4**（Unicode 正規化未統一） | 完了。**label 受理時 / target 受理時 / alias キー・値の読み込み時のすべて**に `unicodedata.normalize('NFC', ...)` を適用。漏れなし |
| **M-1 / P2-7**（U+2028 / U+2029 素通し） | **完了**。`'Zl'` / `'Zp'` をフィルタに追加し、デッドコードだった `ch == '\t'` 分岐を削除。docstring も実装に合わせて修正。テストは修正後の挙動（除去される）を検証する形に更新。`Mn`（結合文字）は行区切りを生成せずログインジェクションのベクトルにならないため対象外と判定 |

---

## 解決アルゴリズムの安全性検証（Phase 2b）

| 検証項目 | 結論 |
|---|---|
| `"A": "A"` 自己参照 | **ループしない**。値を返した後は再解決せず、ブラウザ名でもクライアントにも一致しないため未接続エラーになる |
| `"A": "B"` / `"B": "A"` 相互参照 | **ループしない**。エイリアス解決が1回のみの設計が機能している |
| `chrome:a:b` のコロン分割 | 安全。`partition(':')` で `("chrome", "a:b")` に分割され、識別子部に `:` が残っても各段は完全一致 / 前方一致で比較するため特殊扱いされない |
| `casefold()` と NFC 正規化の順序 | 安全。target は NFC → casefold の順。トルコ語 `İ.casefold()` は `i̇`（i + 結合ドット）となり通常の `i` とは一致しない |
| 1文字の profileId 前方一致 | 安全。複数該当時は曖昧性エラーを正しく返す |
| `*abc` / `a*c` のワイルドカード | 安全。`identifier_part == '*'` の完全一致でのみ発動し、部分的に `*` を含む値は4段階照合に回される |
| email 形式検証の緩さ | 問題なし。`split('@', 1)` でローカルパートを取り出すだけで DNS 解決もメール送信もしない。`@@` を含む値は二重チェックで拒否される |

### 設定ファイルの耐性

| 攻撃パターン | 挙動 |
|---|---|
| `/etc/shadow` 等を指す環境変数 | `json.loads()` 失敗 → 警告ログ（パス名のみ、**内容漏洩なし**） → エイリアスなしで動作 |
| 不正な JSON | `JSONDecodeError` → 警告 → エイリアスなし |
| 非 dict のトップレベル / `aliases` 値 | 型チェックで拒否 → 警告 → エイリアスなし |
| 非文字列・空・長すぎるエントリ | 個別スキップ（他のエントリは読み込み継続） |
| 深いネスト | `data.get('aliases')` はトップレベルのみ参照するため無視される |
| 巨大なエントリ数 | `MAX_ALIAS_COUNT = 1000` で打ち切り（M-5 対応済み） |
| シンボリックリンク | フォローするが内容漏洩なし（L-6 / P2-11） |

---

### 2026-07-25 レビュー（Phase 2a）

#### Critical / High

なし

#### Medium

**M-1 / P2-7（既知・据え置き）: `_sanitise_for_log` が U+2028 / U+2029 を素通し**

Phase 0 で出された指摘と同一。ただし Phase 2a で新設した `_is_valid_identify_field()` の `isprintable()` が U+2028 / U+2029 を正しく弾くため、**identify 経路からの到達性は遮断された**。残るのは `_sanitise_for_log` を直接通る他の経路（HTTP エラー文字列、例外文字列など）で、到達性は Phase 1 から変化していない。次に `server.py` を触るフェーズで対応する。

#### Low

**L-3: 空白のみの `label` / `email` / `profileId` がバリデーションを通過する** → Phase 2b で対応

- 場所: `server.py` の `_is_valid_identify_field()` の `not value` チェック
- 内容: `"   "`（空白のみ）は `not value` が False（空文字列ではない）、`isprintable()` が True（スペースは印字可能）のため通過する。`display_name` が空白だけになり、`list_clients` のレスポンスや候補列挙のエラーメッセージで候補が視認できなくなる
- 実害の文脈: 環境固有。悪意あるローカルプロセスは脅威モデル外のため、実害は「ユーザーが自分で空白ラベルを設定した際の表示崩れ」に留まる
- 対応方針: 先頭チェックを `not value or not value.strip()` に変更する。`value.strip()` で値自体を書き換えると意図的な前後空白も消えるため、拒否のみに使う

**L-4: `label` の Unicode 正規化が未統一** → Phase 2b で対応

- 場所: `server.py` の label 受理部分
- 内容: NFC 形と NFD（結合文字分解）形は `isprintable()` をどちらも通過するがバイト列が異なるため、別の label として扱われる
- 実害の文脈: モード別。Phase 2a では label は表示専用のため実害なし。**Phase 2b のエイリアス解決**（解決順序ステップ2の label 完全一致）で、見た目が同じラベルが一致しない事態になりうる
- 対応方針: Phase 2b で label をエイリアス解決に使う段階で、受理時に `unicodedata.normalize('NFC', label)` を適用する

#### 改善提案（対応済み）

**mypy が dev 依存に含まれていない** → **対応済み（2026-07-25）**

- 内容: Phase 0 / 1 では `~/.local/bin/mypy`（システム側 1.19.1）に `--python-executable .venv/bin/python3` を渡して型チェックしていたが、`pyproject.toml` の dev 依存に `mypy` がないため venv 単体では実行できず、他環境での再現性がない
- 対応: `uv add --dev "mypy>=1.19.1"` を実行。venv に **mypy 2.3.0** が入り、`.venv/bin/python -m mypy --strict server.py tests/` が 19 ファイルで `Success: no issues found`。システム側より新しいメジャーバージョンでも型エラーなし

---

## 個別検証の結果（Phase 2a）

レビュアーが攻撃者視点で検証した項目と結論。

| 検証項目 | 結論 |
|---|---|
| `profileId` の `':'` 拒否だけでキーの一意性が保証されるか | **保証される**。`browser` は許可リスト（`chrome`/`edge`）でコロンを含まず、`profileId` からコロンを排除すればキー中のコロンは区切りの1個のみ。legacy キー（コロンなし）と新キー（コロン1個）は構造的に衝突しない |
| `email` の形式検証なしのリスク | **許容範囲**。email は表示用と情報フィールドのみで、認証・認可・キー構成に使われない。Phase 2b でエイリアス解決に使う段階で `@` 必須チェックを追加すべき |
| `label` の非 ASCII 許容と正規化攻撃 | `isprintable()` が RTL Override (U+202E)、Zero-Width Space (U+200B)、BOM (U+FEFF)、Line/Paragraph Separator (U+2028/U+2029) を**すべて拒否**することを実測で確認。結合文字（カテゴリ Mn）は通過するが Phase 2a では表示専用のため実害なし（L-4 参照） |
| 候補列挙エラーへの `email` / `label` 露出 | `_resolve_client()` は `send_command()` 経由でのみ呼ばれ、HTTP API（トークン認証済み）か stdin からしか到達しない。**認証前にエラーメッセージが見える経路はない** |
| `list_clients` の情報露出範囲 | トークン認証を通過したリクエストにのみ返る。トークンは 0600 ファイルで保護、localhost 限定。脅威モデルに照らして問題なし |
| P1-3 修正（`.closed` → `.open`）の安全性 | **新たな穴なし**。OPEN な既存接続は従来どおり後着を拒否、CLOSING / CLOSED は後着を許可（拡張リロードの正当な再接続が通る）。既存接続を OPEN → CLOSING に遷移させるには WebSocket のクローズハンドシェイクが必要で、外部プロセスが任意に `.open` を False にすることはできない |
| `run_ping_loop()` の修正 | 適切。ロジックは同一で、スナップショットパターン（`list(self._clients.items())`）も維持されており、イテレーション中の辞書変更への耐性がある |
| `list_clients` が `_command_lock` を経由しない設計 | **設計通り**。`_list_clients_response()` は同期メソッド（`await` なし）で `_clients` を読み取るのみ。asyncio のシングルスレッド特性により実行中にプリエンプションされない |

### 段階移行の安全性（Phase 2a → Phase 3）

| 観点 | 評価 |
|---|---|
| 旧仕様の後方互換 | `profileId` 省略時はキーが `browser` のみになり Phase 2a 以前と同一挙動。テストで証明済み |
| 新旧混在 | 旧キー `"chrome"` と新キー `"chrome:uuid"` は `:` の有無で必ず異なり衝突しない。混在時に `_resolve_client("chrome")` は両方にマッチして曖昧性エラーを返す。**黙って片方を選ばない** |
| ロールバック | 拡張を旧版に戻すだけで完結。サーバー側のロールバックは不要 |
| `type` 分岐 | 未知の `type` は黙って無視するため、Phase 3 の `focus` 通知を受け取っても応答を汚染しない |

**故障モードの評価**: 拡張が意図せず `type` 付きの応答を送った場合、その応答は捨てられコマンドがタイムアウトする。これは「focus 通知を応答と誤認する」より安全な故障モードである。タイムアウトは検知・デバッグ可能だが、応答の誤認は沈黙的なデータ破損になるため。

---

### 2026-07-25 レビュー（Phase 1）

#### Critical

なし

#### High

なし

#### Medium

**M-3: systemd unit にセキュリティハードニングが不足** → **対応済み（2026-07-25）**

- 場所: `systemd/chromekontrol.service` および `~/.config/systemd/user/chromekontrol.service` の `[Service]` セクション
- 内容: `NoNewPrivileges=yes` が未設定。将来 `server.py` にサブプロセス実行を導入した場合、setuid バイナリ経由の特権昇格経路が残る
- 実害の文脈: モード別。現時点の `server.py` はサブプロセスを起動しないため即座の実害はない。防御の深層としてゼロコストで導入できる
- 対応: 両ファイルの `[Service]` に `NoNewPrivileges=yes` を追加し、理由をコメントで併記。`systemd-analyze --user verify` が exit 0、`systemctl show -p NoNewPrivileges` で `yes` が実効されていることを確認
- 補足: user unit では `ProtectSystem=strict` / `PrivateTmp=yes` 等は特権不足で機能しないため、`NoNewPrivileges` が実質唯一の有効なハードニング指定

**M-4: `fast-uri` に HIGH 脆弱性2件** → **対応済み（2026-07-25）**

- 場所: `node_modules/fast-uri`（`package-lock.json` で 3.1.2 に固定されていた）
- 内容: GHSA-v2hh-gcrm-f6hx（literal backslash authority delimiter による host confusion）、GHSA-4c8g-83qw-93j6 / CVE-2026-13676（IDN canonicalization 失敗による host confusion）
- 到達性: `mcp_bridge.mjs` の接続先は `http://127.0.0.1:9766/` の固定値で、外部入力の URL を `fast-uri` に渡す経路がない。SDK 内部でも JSON スキーマ検証（ajv 経由）に使われており URL 解決には使われていない。**到達不可能**
- 対応: `npm audit fix` を実行し `fast-uri` を **3.1.4** へ更新。`package.json` は無変更、`package-lock.json` のみ更新。実行後に `node --check mcp_bridge.mjs` と SDK の import を確認

#### Low

**L-2: `mcp_bridge.mjs` の `res.json()` が SyntaxError を未キャッチ** → 次フェーズで対応

- 場所: `mcp_bridge.mjs` の `sendCommand()` 内、リトライ後とその直後の2箇所
- 内容: `Response.json()` はボディが有効な JSON でない場合に `SyntaxError` を投げる。サーバーが異常状態（未キャッチ例外による HTML エラーページ、OOM による応答の途切れ）の場合に MCP ツールハンドラ内で未キャッチ例外になる
- 実害の文脈: 環境固有。N100 / 8GB 環境での OOM 時にレスポンスが破損する可能性はゼロではないが、実害は「エラーメッセージが不親切」に留まる
- 対応方針: `try { return await res.json(); } catch { ... }` で包み、HTTP ステータスを含むエラーメッセージに変換する。`try-catch` で捕捉するには明示的な `await` が必要

#### 対応不要と判定

**`@hono/node-server <2.0.5`（moderate, GHSA-frvp-7c67-39w9）**

- 内容: Windows 環境での `serve-static` path traversal（`%5C` エンコードされたバックスラッシュ経由）
- 到達性: `mcp_bridge.mjs` は stdio トランスポートのみを使用し、HTTP サーバーを立てず `serve-static` も使わない。かつ実行環境は Linux。**到達不可能**
- 修正手段が `@modelcontextprotocol/sdk` の 1.24.3 へのダウングレード（breaking change）であるため、費用対効果が見合わない
- 優先度: 最低。SDK 側で依存が更新された時点で自然に解消する

---

## トークンライフサイクルの分析結果（Phase 1）

レビュアーが攻撃者視点で精査した結果、**TOCTOU の隙なし**と判定された。

| 段階 | 実装 | 評価 |
|---|---|---|
| 一時ファイル作成 | `tempfile.mkstemp(dir=parent)` | `O_CREAT\|O_EXCL` で原子的。ファイル名は `os.urandom` ベースで推測困難なため、シンボリックリンクの事前設置が成立しない |
| 権限設定 | `os.chmod(tmp_path, 0o600)` | `mkstemp` のデフォルトモードが 0600 のため、umask=0o000 でも chmod 前に他者が読める窓がない。二重保証 |
| 書き込み | `os.fdopen(fd, 'w')` | fd を `mkstemp` から直接受け取るため TOCTOU の隙なし |
| 差し替え | `os.replace(tmp_path, token_file)` | 同一ファイルシステム上では `rename(2)`。読み取り側は古い完全な内容か新しい完全な内容のみを見る |
| 最終権限 | `os.chmod(token_file, 0o600)` | `replace` は inode を差し替えるため権限は引き継がれるが、念のための二重保証 |
| クリーンアップ | 内側 `except BaseException` → 外側 `except OSError` の二重 | 内側で `unlink` 成功なら外側は `missing_ok=True` で空振り。二重失敗も最内の `except OSError: pass` で吸収。全パスがテスト済み |

**トークンファイルを削除しない判断**: 妥当。次回起動で必ず上書きされる。サーバー停止中に古いトークンが残っても、接続先が存在しないため悪用できない。

**401 リトライを1回に制限した判断**: 妥当。2回目の 401 は「読み直しても同じ古いトークン」か「新しいトークンも不一致（2連続再起動などの異常事態）」のいずれかで、3回目に成功する見込みがない。`fetch` 失敗（接続拒否）と 401 で戦略を分けた設計も正しい。接続拒否はファイルの読み直しでは解決しない。

---

## Bitwarden vault を使わない判断の評価（Phase 1）

レビュアーにより**妥当**と評価された。根拠は以下の3点。

1. systemd user unit は lingering が有効ならログインセッション不在でも起動する。`rbw unlocked` に依存すると起動時の可用性を損なう
2. このトークンは外部サービスの認証情報ではなくローカル CSRF 対策トークン。盗まれても 127.0.0.1 にのみバインドされた HTTP エンドポイントへのアクセスにしか使えない
3. `SPEC.md` の脅威モデルで「同一マシン上の他プロセスからの攻撃」は out-of-scope。0600 のファイル権限は異なるユーザーからのアクセスを十分に防いでいる

vault に格納しても、起動時にファイルへ取り出す以上、ファイルにトークンが存在する瞬間は消えない。コスト対効果が見合わない。

---

### 2026-07-25 レビュー（Phase 0）

#### Critical

なし

#### High

なし

#### Medium

**M-1: `_sanitise_for_log` が U+2028 / U+2029 を素通しさせる** → 対応予定（Phase 1 以降）

- 場所: `server.py:138-141`
- 内容: U+2028 LINE SEPARATOR / U+2029 PARAGRAPH SEPARATOR の Unicode カテゴリは `Zl` / `Zp` で、現行のフィルタ対象 `Cf` / `Cc` / `Cs` に含まれない。ECMAScript および RFC 7159 では行末として扱われるため、ログを JSON 形式で外部転送する構成では偽のログエントリを注入できる（CWE-117）
- 実害の文脈: モード別。現環境（Python logging + stderr 出力）では改行として扱われないため実害なし。SIEM 連携や JSON ログ転送を導入した時点で顕在化する
- 到達性: identify の `browser` フィールドは 64文字 / ASCII printable 制限があるため到達しない。`_sanitise_for_log` を直接通る他の経路では理論上可能
- 対応方針: `ISSUES.md` の P2-7 に統合。同じ関数の別の不具合（タブ保持のデッドコード）と合わせて Phase 1 以降で修正する。テスト側のアサーション更新もセットで行う

**M-2: `pytest-asyncio>=1.4.0` のバージョンフロアが古すぎる** → **却下（事実誤認）**

- 指摘内容: 「`>=1.4.0` は 2022年1月頃のリリースでフロアが低すぎる。`>=0.23.0` に引き上げるべき」
- 却下理由: PyPI で検証した結果、事実誤認と判明した

  | バージョン | リリース日 |
  |---|---|
  | 1.4.0 | 2024-09-17 |
  | 0.24.0 | 2024-08-22 |
  | 0.23.0 | 2023-12-03 |

  `1.4.0` は `0.23.0` より新しい。推奨された変更は数値上フロアの引き下げであり、より古いバージョンのインストールを許容する改悪になる。現状の `>=1.4.0` は最新版をフロアに指定しており、これ以上厳しくできない
- 対応: `pyproject.toml` は変更しない

#### Low

**L-1: `FakeWebSocket.__aiter__` が本物の `ConnectionClosedOK` ハンドリングと異なる** → 対応不要

- 場所: `tests/conftest.py:87-93`
- 内容: 本物の websockets 10.x は `ConnectionClosedOK` を内部で `StopAsyncIteration` に変換して `async for` を終了させる。`FakeWebSocket` はフィードされた例外をそのまま raise するため、`handle_connection` の `except` 節まで伝播する
- 対応不要の理由: cleanup は `finally` ブロックで実行されるため、どちらの終了パスでも到達する。差異はログメッセージのみ。`tests/test_handle_connection.py` が `StopAsyncIteration` パスも別テストでカバーしており、`conftest.py` の docstring にも簡略化の事実が明記されている。レビュアー自身も「修正は不要と判断」としている

---

## 適用済みセキュリティ対策

### Always Apply（全項目確認済み）

| 項目 | 状態 |
|---|---|
| 全境界での入力検証 | コマンド / セレクタ長 / tabId型 / browser許可リスト / identify形式 / HTTPヘッダー / ボディサイズをテストで固定 |
| ハードコードされた秘密情報なし | テスト内の `AUTH_TOKEN` はダミーと明記。ruff S105/S106 は `tests/` のみ免除 |
| 具体的な例外処理 | 各例外パスを個別のテストで検証 |
| リソース管理のコンテキストマネージャ | `writer.close()` / `writer.wait_closed()` のテストあり |
| 型ヒントと静的型チェック | mypy strict 設定。`as_ws_protocol()` で型キャストを明示 |
| セキュリティスキャン | ruff の S ルール有効 |
| 機密情報を含まないエラーメッセージ | 401 レスポンスの同一性テストあり（トークンの欠落と不一致を区別しない） |
| セキュリティイベントの監査ログ | ログ呼び出しをコード確認 |

### Conditional Application

| 項目 | 判定 | 内容 |
|---|---|---|
| WebSocket Origin 検証 | 適用 | 11テストで網羅（null / 空 / 拡張オリジン / localhost / 部分文字列攻撃 / ワイルドカード） |
| レート制限・DoS 対策 | 適用 | ヘッダー 8KiB 制限 / ボディ 5MiB 制限 / タイムアウト。全てテスト済み |
| CSRF 対策 | 適用 | カスタムヘッダー `X-ChromeKontrol-Token` + `secrets.compare_digest` + Content-Type 必須 |
| セッション管理 | 適用 | 単一スロット `_pending_response` + `_command_lock` による直列化 |
| AI/MCP/Agent 適合性 | 非適用 | Phase 0 の対象はテストコードのみ。`mcp_bridge.mjs` を触る Phase 1 以降で再評価 |

### サプライチェーン

- 全 dev 依存に OSV API で CVE チェック実施: pytest 9.1.1（0件）/ pytest-asyncio 1.4.0（0件）/ pytest-cov 7.1.0（0件）/ black 26.5.1（0件）
- `websockets==10.4` の CVE 0件を確認。`PRODUCT_INVENTORY.md` の「依存バージョン判断記録」と一致
- `uv.lock` にハッシュ付き（128件）
- CVE フロア指定の妥当性を確認: pytest `>=9.0.3`（CVE-2025-71176 回避）、black `>=26.3.1`（CVE-2026-32274 回避）
- dev 依存は `[dependency-groups] dev` で本番実行経路から分離

### 稼働環境への非干渉

- テストは実ソケットをバインドしない（`FakeWebSocket` + `MagicMock` writer + プリロード済み `StreamReader`）
- ポート 9765 / 9766 への接続・バインドなし
- 外部ネットワークアクセスなし
- 稼働中サーバー（pid 262990）への影響なしを実測で確認

### xfail テストと ISSUES.md の整合性

| 項目 | 確認結果 |
|---|---|
| P1-1（レスポンス混線） | xfail テストの再現手順が ISSUES.md の「reqId がない」記述と一致 |
| P1-3（CLOSING 判定） | `closed=False` で CLOSING 状態を模擬。「`.closed` は CLOSED のときのみ True」の記述と一致 |
| P1-4（対象クライアント切断） | chrome 切断 + edge 残留を再現。「`if not self._clients` が全クライアント消失のみ」の記述と一致 |

---

## AI / MCP / Agent 適合性（Phase 1 で再評価）

`mcp_bridge.mjs` を改修したため MCP Top 10 の観点で再評価した。

| 項目 | 判定 | 内容 |
|---|---|---|
| MCP01（Token Mismanagement） | 対策済み | トークンは 0600 ファイルに保管。ログに値を出力しない。毎リクエスト読み直してキャッシュしない |
| MCP04（Supply Chain） | 対応済み | `fast-uri` を 3.1.4 へ更新（M-4）。`@hono/node-server` は到達不可能と判定 |
| MCP07（Insufficient Auth） | 対策済み | `secrets.compare_digest` によるタイミングセーフ比較。トークンの欠落と不一致を区別しない |
| MCP08（Audit / Telemetry） | 対策済み | 401 拒否・接続エラーをサーバー側でログ出力 |
| MCP10（Context Over-Sharing） | 問題なし | エラーメッセージにトークン値を含まない。ファイルパスは公知情報 |

---

## 次フェーズへの申し送り

- **L-2**（`mcp_bridge.mjs` の `res.json()` 未キャッチ）を次フェーズで対応する
- **M-1 / P2-7**（`_sanitise_for_log` の U+2028 / U+2029 素通し）を `server.py` を触るタイミングで修正する。Phase 1 の変更で到達性は変化していないことをレビュアーが確認済み
- Phase 2（P0-1: プロファイル識別・エイリアス解決・未接続時の自動起動）はレビュー対象。特に **2c のプロファイル自動起動**は、サーバーが外部プロセスを起動する経路を新設するため重点的な検証が必要
- `@hono/node-server` の moderate 2件は SDK 側で依存が更新された時点で再評価する
