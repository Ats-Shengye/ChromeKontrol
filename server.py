"""
Location   : ChromeKontrol/server.py
Purpose    : CLIツール（Bash, curl等）とChromeKontrol Chrome拡張機能を橋渡しする
             WebSocketサーバー。2つのモードに対応:
               - ワンショットモード（デフォルト）: stdinからコマンドを1つ読み取り、
                 拡張機能に送信し、レスポンスを出力して終了。
               - サーブモード（--serve）: 無期限に稼働し、ポート9766のHTTPリスナーが
                 POSTリクエストを受け付け、WebSocket経由で拡張機能に転送する。
Why        : CDP (Chrome DevTools Protocol) は特別なChromeフラグが必要で、
             デバッグ環境以外には不向き。軽量なローカルWebSocketサーバーにすることで
             それらの制約を回避しつつ、localhost限定を維持できる。
             常駐サーブモードにより、コマンドが連続実行される場合（自動化スクリプト等）
             の起動ごとのレイテンシを解消する。
Related    : extensions/chromium/background.js (client), extensions/chromium/manifest.json

マルチブラウザ対応:
  - 複数のブラウザ拡張機能（Chrome, Edge, Firefox等。許可リストはALLOWED_BROWSERS
    参照）が同時に接続可能。
  - 各拡張機能は接続直後にidentifyメッセージを送信する:
      {"type": "identify", "browser": "chrome"}  または  {"browser": "edge"}  または  {"browser": "firefox"}
  - コマンドは "browser" フィールドで特定のブラウザを指定可能。
  - "browser" が省略され、クライアントが1つだけ接続している場合は自動的にそれを使用。
    複数接続時はエラーで拒否する。

マルチプロファイル対応（ISSUES.md P0-1, Phase 2a）:
  - identifyメッセージは "browser" に加え、任意フィールド "profileId" / "email" /
    "label" を受理する（いずれも省略可）。
      {"type": "identify", "browser": "chrome", "profileId": "a3f2c1d8",
       "email": "user@example.com", "label": "メイン"}
  - profileIdが指定された場合、クライアントは "browser:profileId" 形式のキーで
    管理され、同一ブラウザの異なるプロファイルが同時接続できる。
  - profileIdが省略された場合（Phase 3で拡張側が更新されるまでの後方互換）は
    従来どおりbrowser名のみでキーを構成し、挙動は変わらない。
  - "browser" フィールドによる解決は該当ブラウザの全プロファイルを対象にする。
    1つだけ一致すれば自動選択、複数一致すれば候補一覧を含むエラーを返す。
  - 接続中クライアントの一覧は "list_clients" コマンドで取得できる
    （拡張機能への転送は行わず、サーバー側の保持情報のみで応答する）。

エイリアス解決（ISSUES.md P0-1, Phase 2b）:
  - コマンドは "browser" の代わりに "target" フィールドで対象クライアントを
    指定できる（両方同時の指定はエラー）。
  - "target" はまず設定ファイル（デフォルト ~/.config/chromekontrol/config.json、
    CHROME_KONTROL_CONFIG_FILE環境変数で上書き可）の "aliases" と大文字小文字を
    無視して完全一致するか確認し、一致すればその値を解決対象文字列とする
    （エイリアスの再帰解決は1回のみ）。
  - 解決対象文字列は最初の":"でbrowser部/identifier部に分割し、browser部が
    あればそのブラウザのクライアントに絞り込む。identifier部が"*"ならその
    絞り込み内で単純に0/1/複数を判定し、それ以外ならlabel完全一致→email完全
    一致→emailローカルパート完全一致→profileId前方一致の順に照合する
    （最初に1件以上ヒットした段で確定、複数なら曖昧性エラー）。
  - 設定ファイルは起動時に1回だけ読み込まれる。変更の反映にはサーバー再起動
    が必要。

プロファイル自動起動（ISSUES.md P0-1, Phase 2c）:
  - "target" が解決対象文字列に解決できたが該当クライアントが未接続（曖昧では
    なく候補0件）の場合、設定ファイルの "autoLaunch" がtrueであれば、サーバーが
    ブラウザプロセスを自動起動して接続を待つことができる。既定は無効（false）。
  - 実行するコマンドは設定ファイルから一切指定させない。ブラウザ実行ファイルは
    BROWSER_EXECUTABLE_CANDIDATESの許可リストでサーバー側に固定し、
    shutil.which()でPATHから解決する。設定ファイルの "profiles" マッピングには
    プロファイルディレクトリ名（例: "Profile 1"）のみを書かせる。これにより
    config.jsonを書き換えられる攻撃者であっても任意コマンド実行はできない。
  - subprocess.Popen()は必ずshell=False（引数は配列）で呼び出す。シェル
    メタ文字は構造的に解釈されず、プロファイルディレクトリ名に半角スペースが
    含まれても1つの引数として正しく扱われる。
  - 起動の発動条件（すべて満たす場合のみ）: (1) autoLaunchがtrue、
    (2) targetフィールドが指定されている、(3) 解決対象文字列が"profiles"に
    登録されている、(4) 該当するクライアントが接続していない、(5) そのプロ
    ファイルがクールダウン中でない、(6) ブラウザ実行ファイルが見つかる。
  - 同一プロファイルへの起動は60秒のクールダウンを設ける（成功・失敗を問わず、
    起動を試みた時刻から）。ブラウザの終了監視は行わず、クールダウンは時間
    ベースのみで解除される。
  - 起動後の接続待機は_command_lockを保持したまま最大30秒行う（通常のコマンド
    タイムアウト15秒より長い。ブラウザ起動とService Worker初期化に時間が
    かかるため）。この間は他の全コマンドがブロックされる——単一ユーザーの
    ローカルツールでは許容範囲と判断し、Phase 4のreqId対応時に見直す。

フォーカス最新クライアントの自動選択（ISSUES.md P1-5, Phase F7）:
  - 拡張機能は自分のウィンドウがフォーカスを得るたびに
    {"type": "focus", "ts": <クライアント側Date.now()のミリ秒epoch>} を
    サーバーへ通知する。identifyメッセージにも任意フィールド"focusTs"として
    直近の記録を含められる（拡張のロード直後、接続確立前にフォーカスを
    得ていた場合の初期値）。
  - サーバーは各クライアントの最終フォーカス時刻を_focus_ts（キー: ClientInfo.key）
    に保持する。target/browserが省略され複数クライアントが接続中の場合、
    _focus_tsに記録があるクライアントの中で最も新しいものを自動選択する
    （_resolve_client参照）。全クライアントが未記録の場合のみ、従来どおり
    曖昧性エラーにフォールバックする。
  - tsの検証は数値（int/float、boolを除く）かつ0より大きいことのみ。
    不正な値は記録を更新せず、エラーレスポンスも返さない（一方的な通知の
    ため）。上限（未来すぎる時刻）は検証しない——同一マシン上の自分の
    拡張機能からしか届かない値であり脅威モデル外（詳細はサーバー実装の
    _is_valid_positive_timestamp参照）。

セキュリティ上の考慮事項:
  - WebSocketとHTTPの両リスナーは127.0.0.1にのみバインドする。
  - WebSocket接続時にlocalhostのOriginヘッダーを検証する。
  - 受信メッセージサイズを制限しメモリ枯渇を防止する（GHSA-6g87-ff9q-v847）。
  - 受信HTTPおよびWebSocketコマンドの構造バリデーションを実施する。
  - 同時HTTPリクエストはasyncio.Lockで直列化し、同時呼び出し間の
    レスポンスの混在を防止する。
  - HTTPリクエストにはX-ChromeKontrol-Tokenヘッダーによるトークン認証を必須とする。
    CSRFのsimple request（preflight不要）攻撃を防止する。
  - HTTPリクエストにはContent-Type: application/jsonを必須とし、CORS preflightを強制する。
  - 機密データはログに記録しない。トークン値はログ・エラーレスポンスに含めない。
  - プロファイル自動起動（Phase 2c）が有効な場合、サーバーは外部プロセス
    （ブラウザ）を起動する。実行ファイルはサーバー側の許可リストで固定され、
    設定ファイルからは指定できない。subprocess.Popen()はshell=Falseで
    呼び出すため、config.jsonを書き換えられる攻撃者に対しても引数
    インジェクション以上の攻撃面は生まれない（詳細は上記「プロファイル
    自動起動」節）。

トークンの受け渡し（ISSUES.md P0-2）:
  - 認証トークンは起動ごとに決定（環境変数CHROME_KONTROL_TOKEN優先、なければ
    secrets.token_urlsafe(32)で新規生成）され、権限0600のファイルに書き出される。
    デフォルトの配置場所は ~/.config/chromekontrol/token（CHROME_KONTROL_TOKEN_FILE
    環境変数で上書き可能、主にテスト用途）。
  - トークン値はstderr/ログに一切出力しない。案内するのはファイルパスのみ。
  - ファイルへの書き込みに失敗しても警告ログのみでサーバー起動は継続する
    （環境変数経由でのトークン共有が引き続き機能するため）。
  - トークンファイルの削除・クリーンアップは行わない。次回起動時に上書きされる
    ため不整合は自然に解消し、異常終了時の削除漏れを気にする必要がない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeGuard

import websockets
import websockets.server

# ---------------------------------------------------------------------------
# ログ設定
# ---------------------------------------------------------------------------

# 設計判断: ロガーをモジュールレベルで設定することで、外部の呼び出し元
# （テストランナー等）がこのファイルを変更せずにハンドラーを上書きできるようにしている。
logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """CLI使用時のルートロガーを設定する。

    Coding.mdのLogging Layer Design原則に従い、ハンドラーの設定は
    エントリーポイント関数に限定している。
    """
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

DEFAULT_PORT: int = 9765
DEFAULT_HTTP_PORT: int = 9766
BIND_HOST: str = '127.0.0.1'

# 単一の受信WebSocketメッセージの最大バイト長。
# GHSA-6g87-ff9q-v847（大きなメッセージによるメモリ枯渇）を緩和する。
# 5 MBは一般的なページのDOM HTMLに対して十分な余裕がありつつ、上限を設ける。
MAX_MESSAGE_BYTES: int = 5 * 1024 * 1024  # 5 MiB

# このサーバーが受け付けるコマンド（background.jsでも同じセットをバリデーション）。
# 'list_clients' はISSUES.md P0-1（Phase 2a）で追加。他のコマンドと異なり
# background.jsへは転送されず、サーバーが保持するクライアント情報のみで完結する
# （ChromeKontrolServer.send_command()内の分岐を参照）。
ALLOWED_COMMANDS: frozenset[str] = frozenset({'get_dom', 'click', 'get_elements', 'list_tabs', 'list_clients'})

# セレクター長の上限（background.jsのバリデーションと同一）。
MAX_SELECTOR_LENGTH: int = 512

# identifyメッセージの任意フィールド（ISSUES.md P0-1, Phase 2a）の長さ上限。
# profileIdは拡張側でcrypto.randomUUID()生成を想定するためUUID長(36)に余裕を
# 持たせた64、emailはRFC上の実用上限として広く使われる254、labelはオプション
# ページでの手入力を想定した64。
MAX_PROFILE_ID_LENGTH: int = 64
MAX_EMAIL_LENGTH: int = 254
MAX_LABEL_LENGTH: int = 64

# このサーバーが受け付けるブラウザ名。identifyメッセージの "browser" フィールドが
# このセットに含まれない場合は拒否し、予期しないクライアントが任意の名前で
# 登録するのを防ぐ。
ALLOWED_BROWSERS: frozenset[str] = frozenset({'chrome', 'edge', 'firefox'})

# コマンドの "target" フィールド（ISSUES.md P0-1, Phase 2b）の長さ上限。
# エイリアス名・"browser:profileId"形式のいずれも収まる余裕を持たせつつ、
# 過大な値によるログ肥大・処理コストを抑える。
MAX_TARGET_LENGTH: int = 256

# 設定ファイルの "aliases" エントリ（キー・値ともに）の長さ上限。
# targetと同じ256文字を採用する（エイリアス値はtargetにそのまま代入される
# ため、targetより緩い上限を設ける理由がない）。
MAX_ALIAS_ENTRY_LENGTH: int = 256

# 設定ファイルの "aliases" マッピングに読み込むエントリ数の上限
# (Security-Audit.md M-5)。実運用でのエイリアス数は数十件程度が現実的な
# 上限であり、1000件は防御的上限として十分な余裕を持つ。悪意あるローカル
# プロセスによるconfig.json改ざんは脅威モデル外だが、巨大なconfig.json
# （例: 100万エントリ × 512バイト ≈ 500MB）を読み込むとN100/8GB環境では
# メモリ枯渇（OOM）の可能性があり、また_aliases_for_client()がlist_clients
# のたびに全エントリをO(aliases)でイテレーションするため応答が遅くなる。
# 上限を設けることでこれらのコストをほぼゼロのコストで防げる。
MAX_ALIAS_COUNT: int = 1000

# 設定ファイルのデフォルト配置場所。CHROME_KONTROL_CONFIG_FILE環境変数で
# 上書き可能（主にテスト用途。トークンファイルと同じパターン）。
DEFAULT_CONFIG_FILE: Path = Path.home() / '.config' / 'chromekontrol' / 'config.json'

CONFIG_FILE_ENV_VAR: str = 'CHROME_KONTROL_CONFIG_FILE'

# ---------------------------------------------------------------------------
# プロファイル自動起動（ISSUES.md P0-1, Phase 2c）
# ---------------------------------------------------------------------------

# "profiles" エントリの値（プロファイルディレクトリ名）の長さ上限。
# 実際のディレクトリ名は"Default"/"Profile 1"程度で、64文字は十分な余裕を
# 持った防御的上限（MAX_PROFILE_ID_LENGTHと同じ考え方）。
MAX_PROFILE_DIR_LENGTH: int = 64

# プロファイルディレクトリ名として許可する、英数字以外の追加文字。
# Chromeのプロファイルディレクトリ名で実際に使われる文字種（半角スペース・
# ハイフン・アンダースコア）のみを許可し、それ以外を許す理由がない。
_PROFILE_DIR_ALLOWED_EXTRA_CHARS: frozenset[str] = frozenset({' ', '-', '_'})

# 自動起動後、プロファイルのクライアントが接続するまで待つ最大秒数。
# 通常のコマンドタイムアウト（15秒、send_commandのデフォルト）より長くする。
# ブラウザプロセスの起動とMV3 Service Workerの初期化に時間がかかるため。
AUTO_LAUNCH_WAIT_TIMEOUT: float = 30.0

# 同一プロファイルへの自動起動のクールダウン秒数。起動を試みた時刻から起算し、
# 成功・失敗を問わず適用する。ブラウザの終了監視は行わないため、ユーザーが
# 手動でブラウザを閉じてもクールダウンは時間経過のみで解除される。
AUTO_LAUNCH_COOLDOWN_SECONDS: float = 60.0

# 自動起動時に探索するブラウザ実行ファイル名の許可リスト（この順で
# shutil.which()により探索し、最初に見つかったものを使う）。設定ファイルから
# 実行ファイルを指定させないためのセキュリティ設計の中核: config.jsonを
# 書き換えられる攻撃者がいても、ここに列挙されていない任意のコマンドは
# 実行できない。
BROWSER_EXECUTABLE_CANDIDATES: dict[str, tuple[str, ...]] = {
    'chrome': ('google-chrome', 'google-chrome-stable', 'chromium'),
    'edge': ('microsoft-edge', 'microsoft-edge-stable'),
}
# 'firefox' はALLOWED_BROWSERSに含まれる（Phase F2）が、意図的にここへは
# 追加しない。プロファイル自動起動（_auto_launch_response()）は現時点で
# Firefoxを対象外とする:
#   1. 検証環境のFirefoxはflatpak版で、起動が
#      `flatpak run org.mozilla.firefox` というサブコマンド形式になる。
#      この辞書はshutil.which()で解決できる単一の実行ファイル名を前提と
#      しているため構造が合わない。
#   2. プロファイル指定の形式が異なる。Chrome/Edgeは
#      `--profile-directory=<名前>` だが、Firefoxは `-P <名前>` または
#      `--profile <パス>`。
#   3. 実環境のFirefoxプロファイル名に非ASCII文字と半角スペースが含まれ、
#      _is_valid_profile_directory_name()の検証を通らないものがある。
# BROWSER_EXECUTABLE_CANDIDATES.get(browser, ())は未登録ブラウザに対して
# 空タプルを返すため、_resolve_browser_executable('firefox')は常にNoneを
# 返す。_auto_launch_response()側はこれを「自動起動に対応していない
# ブラウザ」として扱い、KeyErrorにはしない（同関数内のコメント参照）。

# ローカル以外の接続を防ぐための許可済みlocalhostオリジン値。
ALLOWED_ORIGINS: frozenset[str] = frozenset({
    f'ws://{BIND_HOST}',
    'ws://localhost',
    'ws://127.0.0.1',
})
# 注: 'null' オリジン（file:// ページ由来）は _is_allowed_origin() で明示的に拒否する。

# CSRF対策: HTTPリクエスト認証に使用するカスタムヘッダー名。
# ブラウザのsimple request判定（preflightなし）を回避するため、
# カスタムヘッダーを必須とする。これによりCORS preflightが強制される。
# セキュリティ注記: ヘッダー名はログに記録してよいが、ヘッダー値（トークン）は絶対に記録しない。
HTTP_AUTH_HEADER_NAME: str = 'X-ChromeKontrol-Token'

# CSRF対策: Content-Type検証。application/json以外を拒否することで
# CORS preflightを強制し、simple requestによる攻撃を防止する。
REQUIRED_CONTENT_TYPE: str = 'application/json'


# ---------------------------------------------------------------------------
# 入力バリデーション
# ---------------------------------------------------------------------------

def _sanitise_for_log(value: Any) -> str:
    """ログ出力前に値からASCIIおよびUnicode制御文字・行区切り文字を除去する。

    悪意のあるペイロードが改行、エスケープシーケンス、Unicode制御文字
    （例: U+202E RIGHT-TO-LEFT OVERRIDE, U+FEFF BOM）や行区切り文字
    （U+2028 LINE SEPARATOR, U+2029 PARAGRAPH SEPARATOR）を埋め込んで
    ログエントリを偽造したりターミナル表示を操作するログインジェクション
    攻撃（CWE-117）を防止する。

    Unicodeカテゴリによるフィルタリング:
      - 'Cf' (Format): 不可視の書式制御文字（U+202E, U+FEFF等）
      - 'Cc' (Control): C0/C1制御コード（改行、エスケープ、タブ等）
      - 'Cs' (Surrogate): エンコードエラーを引き起こす孤立サロゲート
      - 'Zl' (Line Separator): U+2028。ECMAScript/RFC 7159では行末として
        扱われるため、JSON形式でログを外部転送する構成では偽の
        ログエントリを注入できる（ISSUES.md P2-7 / M-1）
      - 'Zp' (Paragraph Separator): U+2029。Zlと同様の理由で除去する

    タブ（U+0009）はカテゴリ'Cc'に属するため、この関数は制御文字として
    無条件に除去する。ログインジェクション対策としてはタブを残さない方が
    安全なため、タブのみを例外的に保持する分岐は設けない。

    Args:
        value: サニタイズ対象の任意の値。

    Returns:
        全ての制御文字・書式文字・行区切り文字を除去したサニタイズ済み文字列表現。
    """
    raw = str(value)
    return ''.join(ch for ch in raw if ch >= ' ' and unicodedata.category(ch) not in ('Cf', 'Cc', 'Cs', 'Zl', 'Zp'))


def _is_valid_identify_field(
    value: Any,
    *,
    max_length: int,
    require_ascii: bool,
    require_email_format: bool = False,
) -> bool:
    """identifyメッセージの任意フィールド（profileId/email/label）の形式を検証する。

    3つのフィールドはいずれも「文字列型・非空白・印字可能文字のみ・長さ上限内」
    という共通の骨格を持ち、ASCII限定かどうかだけが異なる
    （profileId/emailはASCII限定、labelは日本語ラベルを許容するため非ASCII可）。
    重複を避けるためこの共通部分を1箇所に集約する。

    印字可能文字チェック（str.isprintable()）は制御文字だけでなく
    Unicodeの「Separator」カテゴリ（U+2028 LINE SEPARATOR / U+2029 PARAGRAPH
    SEPARATOR を含む）も非印字可能として除外するため、ログインジェクション
    （ISSUES.md P2-7が指摘する_sanitise_for_log自体の穴とは別経路）や
    JSON行区切りへの偽装をこの入力境界で未然に防ぐ。

    Security-Audit.md L-3: 先頭チェックを`not value`だけでなく
    `not value.strip()`も見るように変更した。空白のみの値（例: "   "）は
    `not value`がFalse・`isprintable()`もTrue（スペースは印字可能）のため
    従来は通過していたが、display_nameが空白だけになりlist_clientsや
    曖昧性エラーの候補列挙で視認できなくなる問題があった。検証にのみ
    `value.strip()`の結果を使い、戻り値・保存される値は元の文字列のまま
    とする（意図的な前後空白を消さないため。値自体をstrip()して書き換え
    ることはしない）。

    Args:
        value: 検証対象の値（dict.get()の戻り値なのでAny型）。
        max_length: 許容する最大文字数（Pythonの文字列長、コードポイント単位）。
        require_ascii: Trueならisascii()も要求する（profileId/email用）。
                       Falseなら非ASCII文字を許可する（label用）。
        require_email_format: Trueならemail形式（"@"をちょうど1個含み、
                       ローカルパート・ドメイン部がともに1文字以上）も要求する
                       （identify受理時のemailフィールド用。エイリアス値に
                       含まれる文字列に対しては適用しない——照合に失敗する
                       だけで実害がないため）。

    Returns:
        文字列型・非空白・印字可能・長さ制限内（・ASCII制限・email形式を
        満たす場合）True。
    """
    if not isinstance(value, str) or not value or not value.strip():
        return False
    if not value.isprintable():
        return False
    if require_ascii and not value.isascii():
        return False
    if require_email_format:
        local_part, sep, domain_part = value.partition('@')
        # 「ちょうど1個の"@"」は、partition()が最初の"@"で分割した後の
        # domain_partに"@"が残っていないかで判定する（2個目以降の"@"が
        # あればdomain_part側に現れる）。
        if not sep or not local_part or not domain_part or '@' in domain_part:
            return False
    return len(value) <= max_length


def _is_valid_positive_timestamp(value: Any) -> TypeGuard[int | float]:
    """タイムスタンプ値（クライアント側Date.now()のミリ秒epoch想定）が
    数値かつ正であるかを検証する。

    ISSUES.md P1-5（Phase F7）: identifyメッセージの"focusTs"フィールドと、
    ハンドシェイク後に届く{"type": "focus", "ts": ...}通知の"ts"フィールドは
    どちらも同じ意味（最終フォーカス時刻）を持つため、検証ロジックを
    ここに集約する。

    Pythonではboolがintのサブクラスであるため、`isinstance(value, int)`
    だけではTrue/Falseがそれぞれ1/0として通過してしまう。
    `isinstance(value, bool)`を先に弾いて除外する必要がある。

    Security-Audit.md M-1（Phase F7 セキュリティレビュー）: JSON数値リテラルの
    `1e999`（`float`の表現可能域を超える値）は`json.loads()`によって
    `float('inf')`としてパースされる。`inf`は`isinstance(value, (int, float))`
    をTrueで通過し、かつ`inf > 0`もTrueであるため、`math.isfinite()`による
    チェックがなければこの関数を通過してしまう。通過した`inf`を呼び出し元
    （`_receive_identify`のfocusTs検証、`_handle_focus_notification`）が
    `int(value)`に渡すと、Pythonの`int`には`inf`に対応する値がないため
    `OverflowError`が送出される。未捕捉のまま`_handle_message()`から
    `handle_connection()`の呼び出し元まで伝搬すると、その接続のメッセージ
    ループ（送信元の接続ハンドラ）がクラッシュする。
    なお`float('-inf')`は`value > 0`が`False`になるため、また`float('nan')`
    も`nan > 0`が常に`False`であるため、どちらも本チェックを追加する前から
    既に弾かれていた——`math.isfinite()`の追加は`+inf`の穴を塞ぐために
    必要であり、一見冗長に見えても削ってはならない。

    上限（未来すぎる時刻）の検証は行わない。呼び出し元のいずれの経路でも、
    未来の時刻を送ってきたクライアントが選ばれやすくなるだけで、同一マシン上の
    自分の拡張機能からしか届かない値であるため脅威モデル外（仕様書参照）。

    戻り値型をTypeGuardにしている理由: 呼び出し元（_receive_identifyの
    focusTs検証、_handle_focus_notification）はこの関数がTrueを返した後に
    `int(value)`を呼ぶ。単なるboolを返す設計だとmypy --strictがその
    呼び出しをAny | Noneのままとみなし型エラーになるため、TypeGuardで
    呼び出し元スコープの`value`をint | floatへ絞り込む。

    Args:
        value: 検証対象の値（dict.get()の戻り値なのでAny型）。

    Returns:
        boolを除くint/float型で、有限（inf/nanでない）かつ0より大きければ
        True。
    """
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and value > 0
    )


def _validate_command(msg: Any) -> tuple[bool, str]:
    """CLI呼び出し元からのパース済みコマンドdictをバリデーションする。

    Args:
        msg: パース済みJSON値（dictであることを期待）。

    Returns:
        (is_valid, error_message) のタプル。
        is_validがTrueの場合、error_messageは空文字列。
    """
    if not isinstance(msg, dict):
        return False, 'Command must be a JSON object.'

    cmd = msg.get('cmd')
    if not isinstance(cmd, str) or cmd not in ALLOWED_COMMANDS:
        return False, f'Unknown or missing command: {_sanitise_for_log(cmd)}'

    selector = msg.get('selector')
    if cmd in ('click', 'get_elements'):
        if not isinstance(selector, str):
            return False, 'Missing or invalid selector field.'
        if len(selector) > MAX_SELECTOR_LENGTH:
            return False, f'Selector exceeds maximum length ({MAX_SELECTOR_LENGTH}).'

    # オプションのtabIdフィールド: 存在する場合はint型の非負整数であること。
    tab_id = msg.get('tabId')
    if tab_id is not None:
        if not isinstance(tab_id, int) or isinstance(tab_id, bool):
            return False, 'tabId field must be an integer.'
        if tab_id < 0:
            return False, 'tabId must be a non-negative integer.'

    # オプションのbrowserフィールド: 存在する場合は許可リスト内の文字列であること。
    browser = msg.get('browser')
    if browser is not None:
        if not isinstance(browser, str):
            return False, 'browser field must be a string.'
        if browser not in ALLOWED_BROWSERS:
            return False, f'Unknown browser: {_sanitise_for_log(browser)}'

    # オプションのtargetフィールド（ISSUES.md P0-1, Phase 2b）: 存在する場合は
    # 1〜MAX_TARGET_LENGTH文字の文字列であること。中身（エイリアス名か
    # "browser:identifier"形式か等）の解釈はChromeKontrolServer側で行うため、
    # ここでは形式のみを検証する。
    target = msg.get('target')
    if target is not None:
        if not isinstance(target, str):
            return False, 'target field must be a string.'
        if not (1 <= len(target) <= MAX_TARGET_LENGTH):
            return False, f'target field must be between 1 and {MAX_TARGET_LENGTH} characters.'

    # "target" と "browser" は同じ目的（対象クライアントの指定）を持つため、
    # 両方指定された場合は矛盾を黙って解決せずエラーとする。
    if target is not None and browser is not None:
        return False, 'Specify either "target" or "browser", not both.'

    return True, ''


# ---------------------------------------------------------------------------
# Originバリデーション
# ---------------------------------------------------------------------------

def _is_allowed_origin(headers: Any) -> bool:
    """WebSocketハンドシェイクのOriginがlocalhostであることを確認する。

    websockets 10.x はヘッダーを .get() メソッドを持つHeadersオブジェクトとして公開する。

    設計判断: Originヘッダーの欠如をチェックするのではなく、ホワイトリストと
    大文字小文字を区別せず比較する。これにより、常にOriginを送信するブラウザが
    非localhostの値で偽装されることを防ぐ。

    Args:
        headers: WebSocketハンドシェイクのリクエストヘッダー。

    Returns:
        Originが許容されるlocalhostバリアントの場合True。
    """
    origin: str = (headers.get('Origin') or '').lower()
    # "null" オリジン（file://等からのシリアライズ済みオリジン）を拒否する。
    if origin == 'null':
        return False
    # Originヘッダーなしの接続を許可する（wscat等のCLIツール用）。
    if not origin:
        return True
    # Chrome/Edge拡張機能のオリジンを許可する。サーバーは127.0.0.1にのみ
    # バインドしているため、ローカルの拡張機能だけがこのポートに到達できる。
    if origin.startswith('chrome-extension://'):
        return True
    # Firefox拡張機能のオリジンを許可する（Phase F2）。Firefoxの拡張は接続時に
    # "moz-extension://<internal-uuid>" をOriginとして送るが、このUUIDは
    # インストールごとにランダム生成される。manifest.jsonの
    # browser_specific_settings.gecko.idで拡張ID自体を固定しても、この
    # UUIDは事前に判明しないため完全一致による検証はできず、
    # chrome-extension://と同様のプレフィックス一致が唯一の方法になる。
    # セキュリティ上の位置づけもchrome-extension://と同じ: プレフィックス
    # 一致では任意のFirefox拡張が接続しうるが、サーバーは127.0.0.1にのみ
    # バインドしており到達できるのはローカルの拡張だけ（SPEC.mdの脅威モデルは
    # 同一マシン上の他プロセスからの攻撃を対象外としている）。
    if origin.startswith('moz-extension://'):
        return True
    allowed_lower = {o.lower() for o in ALLOWED_ORIGINS if o != 'null'}
    return origin in allowed_lower


# ---------------------------------------------------------------------------
# クライアント識別情報（ISSUES.md P0-1, Phase 2a）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientInfo:
    """接続中クライアント1つ分の識別情報とWebSocket接続を保持する。

    identifyメッセージのバリデーションを通過した後、_receive_identify()に
    よって構築される。profile_id/email/labelは全て任意で、Phase 3で拡張側
    (background.js)が更新されるまでは、profile_idがNoneの旧仕様クライアント
    のみが接続してくる。その場合keyはbrowserと同一になり、Phase 2a以前の
    挙動（ブラウザ名のみでの識別）と完全に一致する。

    frozen=True: 一度登録されたクライアントの識別情報は再接続まで不変であり、
    _clients辞書に格納された後に書き換える正当な理由がないため、意図しない
    変更を型レベルで防止する。

    Attributes:
        browser: ブラウザ名（ALLOWED_BROWSERSのいずれか）。
        websocket: このクライアントのWebSocket接続。
        profile_id: 拡張側がプロファイルごとに生成・永続化した一意なID。任意。
        email: chrome.identity.getProfileUserInfo()から取得したメールアドレス。任意。
        label: 拡張のオプションページで手動設定された表示名。任意。
        focus_ts: identifyメッセージに含まれていた最終フォーカス時刻
            （ISSUES.md P1-5, Phase F7）。任意。ChromeKontrolServer._focus_ts
            辞書へ直接書き込まず、この不変フィールド経由で運ぶ設計にしている
            理由はhandle_connection()のコメント参照
            （後着拒否で拒否された接続の時刻がサーバー状態に残るのを防ぐため）。
    """

    browser: str
    websocket: websockets.server.WebSocketServerProtocol
    profile_id: str | None = None
    email: str | None = None
    label: str | None = None
    focus_ts: int | None = None

    @property
    def key(self) -> str:
        """`ChromeKontrolServer._clients` 辞書のキー。

        profile_idがあれば "browser:profile_id"、なければ従来どおりbrowserのみ。
        後者はPhase 2/3移行期間中、旧仕様の拡張機能（profileIdを送らない）が
        接続してきても既存の1ブラウザ1クライアントの挙動を保つための設計。
        """
        if self.profile_id is not None:
            return f'{self.browser}:{self.profile_id}'
        return self.browser

    @property
    def display_name(self) -> str:
        """人間向けの表示名。

        決定順序: label > email > profile_idの先頭8文字 > browser。

        用途はlist_clientsコマンドのレスポンス（"displayName"フィールド）のみ。
        ISSUES.md P0-5（Phase F6）以降、エラーメッセージの候補列挙には使わない
        ——labelが未設定のクライアントではこのプロパティがemailを返すため、
        MCP経由でエラーメッセージが言語モデルのコンテキストへ取り込まれると
        PIIが永続化される。候補列挙は_format_client_candidates()が
        key（+label）だけで組み立てる。

        list_clientsのレスポンスでこのプロパティを使い続けているのはHTTP APIの
        後方互換のため。MCPブリッジ側（mcp_bridge.mjs）が、値がemailと一致する
        場合にprofileIdの先頭へ置き換え、emailフィールド自体も除去している
        （Phase F5のM-1対応）。
        """
        if self.label:
            return self.label
        if self.email:
            return self.email
        if self.profile_id:
            return self.profile_id[:8]
        return self.browser


def _format_client_candidates(clients: Iterable[ClientInfo]) -> str:
    """クライアント候補一覧を"key (label)"形式でカンマ区切り連結する。

    ISSUES.md P0-5（Phase F6）: `_format_ambiguous_clients_message()` と
    `_format_not_connected_message()` はどちらも従来 `display_name` を使って
    候補を列挙していたが、`display_name` は label > email > profile_id[:8] >
    browser の優先順で決まるため、label未設定でemailがあるクライアントでは
    メールアドレスがそのままメッセージに出てしまう。Phase F5のM-1
    （list_clientsのemail露出）と同じ構造の問題で、MCP経由だとこの文字列が
    言語モデルのコンテキストに永続化される（Security-Guidelines.md S4/S15）。

    このヘルパーは `display_name` を一切参照せず、`key`（targetにそのまま
    渡せる値）と、設定されていれば`label`（ユーザー自身が付けた任意の名前で
    PIIではない）のみを使う。emailやprofile_idはメッセージに含めない。

    エイリアス名は意図的に含めない。`_aliases_for_client()` を経由すると
    `_resolve_resolved_string()` → `_format_ambiguous_clients_message()` →
    `_aliases_for_client()` → `_resolve_resolved_string()` という無限再帰に
    陥るため（`_aliases_for_client()`のdocstring参照）。エイリアス一覧が
    必要な場合は`list_clients`コマンドを使う。

    Args:
        clients: 列挙対象のClientInfo（未ソートで渡してよい）。

    Returns:
        "key1 (label1), key2, key3 (label3)" 形式の文字列。keyの昇順で
        列挙する。clientsが空なら空文字列を返す（"(none)"のような代替
        表示への置換は呼び出し元の責務）。
    """
    return ', '.join(
        f'{client.key} ({client.label})' if client.label else client.key
        for client in sorted(clients, key=lambda c: c.key)
    )


def _format_ambiguous_clients_message(clients: list[ClientInfo]) -> str:
    """複数クライアントが解決候補になった際のエラーメッセージを組み立てる。

    候補はkeyの昇順で列挙し、labelが設定されていれば括弧で併記する
    （例: "chrome:a3f2c1d8 (メイン)"）。`display_name`は使わない——PII
    （email）が混入しうるため（`_format_client_candidates()`のdocstring、
    ISSUES.md P0-5参照）。呼び出し側（_resolve_client）が「browser指定で
    複数プロファイルに一致」「browser未指定で複数クライアント接続中」の
    どちらの場合でも同一フォーマットを使う。

    Args:
        clients: 候補となったClientInfoのリスト（未ソートで渡してよい）。

    Returns:
        "Multiple clients matched (...); specify \"browser\" or \"target\" to
        select one." 形式のエラーメッセージ本文。
    """
    candidates = _format_client_candidates(clients)
    return f'Multiple clients matched ({candidates}); specify "browser" or "target" to select one.'


def _format_browser_list(names: Iterable[str], *, conjunction: str = 'or') -> str:
    """複数の項目を英語の自然な列挙形式（Oxford comma付き）で連結する。

    Phase F2レビューM-1: ALLOWED_BROWSERSにブラウザが追加された際、ユーザー
    向けメッセージ（接続待ちタイムアウト時の案内、起動時ログのcurlサンプル）
    を個別に手直しする必要がないよう、ALLOWED_BROWSERSの内容から一覧文字列
    を組み立てる共通ヘルパーとして用意した。要素数に応じて次の形式を返す:
      - 0件: 空文字列
      - 1件: そのまま返す
      - 2件: "A {conjunction} B"
      - 3件以上: "A, B, {conjunction} C"

    Args:
        names: 列挙する文字列のイテラブル。frozensetの反復順序は保証されない
               ため、呼び出し側で`sorted(ALLOWED_BROWSERS)`等により順序を
               安定させたものを渡すこと（順序が実行ごとに変わると、ログや
               エラーメッセージも実行ごとに変わってしまい、文字列一致の
               テストが書けなくなる）。
        conjunction: 最後の要素をつなぐ接続詞。「いずれか1つを指定する」
                     文脈のメッセージで使うことを想定し、デフォルトは'or'。

    Returns:
        英語の列挙形式に整形された文字列。
    """
    items = list(names)
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f'{items[0]} {conjunction} {items[1]}'
    return ', '.join(items[:-1]) + f', {conjunction} {items[-1]}'


# ---------------------------------------------------------------------------
# 設定ファイル・エイリアス解決（ISSUES.md P0-1, Phase 2b）
# ---------------------------------------------------------------------------


def _resolve_config_file_path() -> Path:
    """設定ファイルの読み込み先パスを解決する。

    _resolve_token_file_path()と同じ「環境変数優先、なければデフォルト」の
    パターンを踏襲する。

    Returns:
        CHROME_KONTROL_CONFIG_FILE環境変数が空でない値で設定されていれば
        そのパス、なければDEFAULT_CONFIG_FILE。
    """
    override = os.environ.get(CONFIG_FILE_ENV_VAR, '').strip()
    return Path(override) if override else DEFAULT_CONFIG_FILE


def _is_valid_alias_entry(value: Any) -> bool:
    """設定ファイルのaliasエントリ（キーまたは値）の形式を検証する。

    Args:
        value: 検証対象の値（dict.items()由来なのでAny型）。

    Returns:
        文字列型・1〜MAX_ALIAS_ENTRY_LENGTH文字であればTrue。
    """
    return isinstance(value, str) and 1 <= len(value) <= MAX_ALIAS_ENTRY_LENGTH


def _read_config_object(config_file: Path) -> dict[str, Any]:
    """設定ファイルを読み込み、トップレベルJSONオブジェクトとして返す共通ヘルパー。

    _load_aliases() / _load_auto_launch() / _load_profiles()（いずれも
    ISSUES.md P0-1）が共有する読み込みロジック。以下のいずれの場合も例外を
    送出せず空dictを返す: ファイル不在、読み取り失敗、不正なJSON、トップ
    レベルがオブジェクトでない。ファイル不在は正常系（設定ファイル自体が
    任意のため）としてログを出さないが、それ以外の失敗はlogger.warning()
    で報告する。

    設計判断: この関数はserver.py起動時に（aliases / autoLaunch / profiles
    それぞれの読み込み元から）最大3回呼ばれ、同じファイルを都度読み直す。
    設定ファイルの読み込みは起動時に1回のみ発生する処理であり（サーブ
    モードでは変更の反映にサーバー再起動が必要）、3つのトップレベルキーの
    読み込みロジックをそれぞれ独立して読みやすく保つことの方が、I/O呼び出し
    回数を最小化することより優先度が高いと判断した。

    Args:
        config_file: 読み込み対象の設定ファイルパス
                     （通常は_resolve_config_file_path()の戻り値）。

    Returns:
        パース済みのトップレベルJSONオブジェクト。ファイル不在・読み取り
        失敗・不正なJSON・非オブジェクトのいずれかに該当すれば空dict。
    """
    if not config_file.is_file():
        return {}

    try:
        raw = config_file.read_text(encoding='utf-8')
    except OSError as exc:
        logger.warning(
            'Failed to read config file %s: %s. Ignoring config file.',
            _sanitise_for_log(config_file),
            _sanitise_for_log(exc),
        )
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            'Config file %s contains invalid JSON: %s. Ignoring config file.',
            _sanitise_for_log(config_file),
            _sanitise_for_log(exc),
        )
        return {}

    if not isinstance(data, dict):
        logger.warning(
            'Config file %s top-level value must be a JSON object; ignoring config file.',
            _sanitise_for_log(config_file),
        )
        return {}

    return data


def _load_aliases(config_file: Path) -> dict[str, str]:
    """設定ファイルから "aliases" マッピングを読み込み検証する。

    ファイル読み込み・JSONパース・トップレベル検証は_read_config_object()に
    委譲する（そちらが失敗した場合も含め、以下のいずれの場合も例外を送出
    せず空のdict（エイリアスなしで動作）を返す）: ファイル不在、読み取り
    失敗、不正なJSON、トップレベルがオブジェクトでない、"aliases"がオブジェ
    クトでない。個々のエントリが不正な場合（キーまたは値が文字列でない・空・
    長さ上限超過）はそのエントリのみをlogger.warning()で報告して無視し、
    残りのエントリは読み込みを継続する。

    有効なエントリ数がMAX_ALIAS_COUNTに達した時点で読み込みを打ち切り、
    残りのエントリは無視する（Security-Audit.md M-5）。巨大な"aliases"
    マッピングによるメモリ消費、および_aliases_for_client()の
    O(aliases × clients)コスト増大を防ぐための防御的上限。打ち切り時は
    logger.warning()で1回だけ報告する。

    未知のトップレベルキー（"autoLaunch" / "profiles"、Phase 2cで追加）は
    無視する。data.get('aliases')以外のキーには一切触れないため、前方互換性
    は自然に保たれる。

    キー・値はいずれもNFC正規化してから格納する（仕様3-4: 設定ファイル由来の
    alias キー・値もNFC正規化対象）。これにより、later のエイリアス照合
    （casefold比較）でNFC/NFD形の差異を気にする必要がなくなる。

    Args:
        config_file: 読み込み対象の設定ファイルパス
                     （通常は_resolve_config_file_path()の戻り値）。

    Returns:
        NFC正規化済みのキー・値からなるdict。読み込むエイリアスが1つも
        なければ空dict。
    """
    data = _read_config_object(config_file)

    raw_aliases = data.get('aliases')
    if raw_aliases is None:
        return {}
    if not isinstance(raw_aliases, dict):
        logger.warning(
            'Config file %s "aliases" must be a JSON object; continuing without aliases.',
            _sanitise_for_log(config_file),
        )
        return {}

    aliases: dict[str, str] = {}
    for key, value in raw_aliases.items():
        if len(aliases) >= MAX_ALIAS_COUNT:
            logger.warning(
                'Config file %s has more than %d aliases; ignoring the rest.',
                _sanitise_for_log(config_file),
                MAX_ALIAS_COUNT,
            )
            break
        if not _is_valid_alias_entry(key) or not _is_valid_alias_entry(value):
            logger.warning(
                'Ignoring malformed alias entry (key=%s): both key and value must be strings of 1-%d characters.',
                _sanitise_for_log(key),
                MAX_ALIAS_ENTRY_LENGTH,
            )
            continue
        aliases[unicodedata.normalize('NFC', key)] = unicodedata.normalize('NFC', value)

    return aliases


def _load_auto_launch(config_file: Path) -> bool:
    """設定ファイルから "autoLaunch" フラグを読み込む（ISSUES.md P0-1, Phase 2c）。

    仕様2節: 既定false。キー非存在・false・boolean以外のいずれも「無効」
    として扱う。boolean以外の値の場合のみlogger.warning()を出す（キー非存在
    は正常系のためログなし）。

    isinstance(value, bool)による判定: PythonのboolはintのサブタイプだがJSON
    のtrue/falseはPython側で必ずbool型のTrue/Falseにデコードされるため、
    この判定でJSON上の数値（例: 1/0）や文字列（例: "true"）を確実に
    「非boolean」として除外できる。

    Args:
        config_file: 読み込み対象の設定ファイルパス。

    Returns:
        "autoLaunch"がtrueであればTrue、それ以外（キー非存在・false・
        非boolean値）はFalse。
    """
    data = _read_config_object(config_file)
    value = data.get('autoLaunch')
    if value is None:
        return False
    if not isinstance(value, bool):
        logger.warning(
            'Config file %s "autoLaunch" must be a boolean; treating as disabled.',
            _sanitise_for_log(config_file),
        )
        return False
    return value


def _is_valid_profile_directory_name(value: Any) -> bool:
    """設定ファイルの"profiles"エントリの値（プロファイルディレクトリ名）を
    検証する（ISSUES.md P0-1, Phase 2c）。

    検証項目（該当違反時はFalse）:
      1. 文字列型・1〜MAX_PROFILE_DIR_LENGTH文字。
      2. ASCII英数字・半角スペース・ハイフン・アンダースコアのみ。
      3. 先頭が'-'でないこと —— '-'は上記2の許可文字集合
         （_PROFILE_DIR_ALLOWED_EXTRA_CHARS）に含まれるため、文字種チェック
         だけではこのチェックまで到達し、実際に値を弾く（Security-Audit.md
         セキュリティレビュー指摘、Phase 2c）。現在のコード経路では
         `--profile-directory=<value>` という単一の引数文字列として
         Popen()に渡るため、値がハイフンで始まっていてもそれ単体が別の
         オプション引数として誤解釈されるわけではない。しかし値そのものが
         '-'から始まる理由はなく、将来コードが変更されて値が独立した引数
         要素として渡るようになった場合や、Chrome自身が値を特殊に解釈する
         場合に備え、到達可能な検証として維持する。
      4. '/'と'\\'を含まないこと（パストラバーサルの防止）。
      5. '.'または'..'そのものでないこと —— 上記4でパス区切り文字を既に
         禁止しているため現状はこの分岐に到達しないが、Chromeが
         "--profile-directory=.."をどう解釈するか不明なため防御的に
         明示しておく。

    Args:
        value: 検証対象の値（dict.items()由来なのでAny型）。

    Returns:
        全ての検証を通過すればTrue。
    """
    if not isinstance(value, str):
        return False
    if not (1 <= len(value) <= MAX_PROFILE_DIR_LENGTH):
        return False
    if not all(ch.isascii() and (ch.isalnum() or ch in _PROFILE_DIR_ALLOWED_EXTRA_CHARS) for ch in value):
        return False
    if value.startswith('-'):
        return False
    if '/' in value or '\\' in value:
        return False
    if value in ('.', '..'):
        return False
    return True


def _load_profiles(config_file: Path) -> dict[str, str]:
    """設定ファイルから "profiles" マッピングを読み込み検証する
    （ISSUES.md P0-1, Phase 2c）。

    "profiles"が存在しなければ空dict。dictでなければ警告を出し空dictで
    動作を継続する。エントリのキー（解決対象文字列。"aliases"の値と同じ
    "browser:identifier"形式であることが前提）は1〜MAX_TARGET_LENGTH文字の
    文字列であること —— 仕様に明記はないが、キーはtargetのエイリアス解決後
    の文字列と同じ形式であるため、一貫性のためtargetと同じ上限を採用する。
    値は_is_valid_profile_directory_name()の検証を通過すること。いずれかを
    満たさないエントリは個別にlogger.warning()で報告して無視し、残りの
    エントリは読み込みを継続する。

    有効なエントリ数がMAX_ALIAS_COUNTに達した時点で読み込みを打ち切る
    （仕様: 「エントリ数上限はaliasesと同じ1000件」）。_load_aliases()の
    エントリ数上限処理と同じ理由・同じ定数を用いる。

    キーはNFC正規化してから格納する（_load_aliases()と同様、
    _lookup_profile_directory()での照合がNFC正規化済みのresolved文字列を
    前提とするため）。値（プロファイルディレクトリ名）はASCII文字のみを
    許可しているためNFC正規化は事実上no-opであり、正規化は行わない。

    Args:
        config_file: 読み込み対象の設定ファイルパス。

    Returns:
        検証済みのdict（キーはNFC正規化済み）。該当なしなら空dict。
    """
    data = _read_config_object(config_file)

    raw_profiles = data.get('profiles')
    if raw_profiles is None:
        return {}
    if not isinstance(raw_profiles, dict):
        logger.warning(
            'Config file %s "profiles" must be a JSON object; continuing without auto-launch profiles.',
            _sanitise_for_log(config_file),
        )
        return {}

    profiles: dict[str, str] = {}
    for key, value in raw_profiles.items():
        if len(profiles) >= MAX_ALIAS_COUNT:
            logger.warning(
                'Config file %s has more than %d profiles; ignoring the rest.',
                _sanitise_for_log(config_file),
                MAX_ALIAS_COUNT,
            )
            break
        if not isinstance(key, str) or not (1 <= len(key) <= MAX_TARGET_LENGTH):
            logger.warning(
                'Ignoring malformed profiles entry (key=%s): key must be a string of 1-%d characters.',
                _sanitise_for_log(key),
                MAX_TARGET_LENGTH,
            )
            continue
        if not _is_valid_profile_directory_name(value):
            logger.warning(
                'Ignoring profiles entry for %s: invalid profile directory name (%s).',
                _sanitise_for_log(key),
                _sanitise_for_log(value),
            )
            continue
        profiles[unicodedata.normalize('NFC', key)] = value

    return profiles


def _resolve_browser_executable(browser: str) -> str | None:
    """許可リスト（BROWSER_EXECUTABLE_CANDIDATES）に基づき、指定ブラウザの
    実行ファイルパスをPATHから解決する（ISSUES.md P0-1, Phase 2c）。

    設定ファイルから実行ファイルを指定させないためのセキュリティ設計の
    中核: 候補名はこの関数の外から一切渡らず、server.py内に固定された
    リストのみを探索する。

    Args:
        browser: ALLOWED_BROWSERSのいずれか。ただしBROWSER_EXECUTABLE_CANDIDATES
                 にキーを持つのは現時点で'chrome'と'edge'のみ。それ以外
                 （例: 'firefox'）は.get(browser, ())が空タプルを返すため、
                 常にNoneが返る（BROWSER_EXECUTABLE_CANDIDATES定義箇所の
                 コメント参照）。

    Returns:
        shutil.which()で最初に見つかった実行ファイルの絶対パス。
        いずれの候補も見つからなければNone。
    """
    for candidate in BROWSER_EXECUTABLE_CANDIDATES.get(browser, ()):
        found = shutil.which(candidate)
        if found is not None:
            return found
    return None


def _split_resolved_target(resolved: str) -> tuple[str | None, str]:
    """解決対象文字列を最初の":"でbrowser部/identifier部に分割する（仕様3-2）。

    Args:
        resolved: エイリアス解決後（または最初からエイリアスでなかった）の
                  解決対象文字列。

    Returns:
        (browser_part, identifier_part)のタプル。resolvedに":"が含まれなければ
        browser_partはNoneでidentifier_partはresolved全体。
    """
    if ':' in resolved:
        browser_part, _, identifier_part = resolved.partition(':')
        return browser_part, identifier_part
    return None, resolved


def _match_by_identifier_order(candidates: list[ClientInfo], identifier: str) -> list[ClientInfo]:
    """候補集合を識別子の照合順序（仕様3-3）で絞り込む。

    label完全一致 → email完全一致 → emailローカルパート完全一致 →
    profileId前方一致 の順に試し、1件以上ヒットした最初の段の結果を返す
    （そこで確定・複数なら曖昧性エラーの判定は呼び出し側の責務。この関数は
    絞り込みのみを行う）。全段で0件ならその段の結果（空リスト）ではなく
    空リストを返す。

    全ての比較はcasefold()による大文字小文字無視で行う（仕様3-4）。

    Args:
        candidates: 絞り込み対象のClientInfoリスト。
        identifier: 照合対象の識別子文字列（NFC正規化済みを期待）。

    Returns:
        いずれかの段で1件以上ヒットした場合はその段の候補リスト、
        全段0件なら空リスト。
    """
    folded = identifier.casefold()

    step1 = [c for c in candidates if c.label is not None and c.label.casefold() == folded]
    if step1:
        return step1

    step2 = [c for c in candidates if c.email is not None and c.email.casefold() == folded]
    if step2:
        return step2

    step3 = [c for c in candidates if c.email is not None and c.email.split('@', 1)[0].casefold() == folded]
    if step3:
        return step3

    step4 = [c for c in candidates if c.profile_id is not None and c.profile_id.casefold().startswith(folded)]
    if step4:
        return step4

    return []


# ---------------------------------------------------------------------------
# サーバーハンドラー
# ---------------------------------------------------------------------------

class ChromeKontrolServer:
    """マルチクライアントWebSocketサーバー。

    キー（例: "chrome", "edge", "firefox", "chrome:a3f2c1d8"）ごとに1つの接続済み拡張機能
    クライアント（ClientInfo）を管理する。キーはidentifyメッセージの内容から
    ClientInfo.keyとして決まる: profileIdが送られていれば "browser:profileId"、
    送られていなければ従来どおりbrowserのみ（ISSUES.md P0-1, Phase 2a）。
    これにより同一ブラウザの複数プロファイルが同時接続できる。

    CLI呼び出し元はコマンドを送信し、リクエストごとに1つのレスポンスを受け取る。
    コマンド内のオプション "browser" フィールドで対象クライアントを選択する
    （該当ブラウザの全プロファイルが対象になり、1つだけ一致すれば自動選択、
    複数一致すればエラー）。省略時にクライアントが1つだけ接続されていれば
    それを自動使用する。"list_clients" コマンドは拡張機能に転送せず、
    接続中クライアントの一覧をサーバーが保持する情報のみで返す。

    接続ライフサイクル:
      1. 拡張機能がWebSocket経由で接続する。
      2. 拡張機能が即座に
         {"type": "identify", "browser": "<name>", "profileId": "...",
          "email": "...", "label": "..."}
         を送信する（profileId/email/labelは任意）。
      3. サーバーがClientInfo.keyでクライアントを登録する。
      4. 以降のそのソケットからのメッセージはコマンドレスポンスとして扱われる。

    スレッドセーフティに関する注意: asyncioはイベントループ内でシングルスレッドのため、
    _clientsと_pending_responseへのアクセスにロックは不要。
    _command_lockは同時HTTPリクエストを直列化し、WebSocketラウンドトリップが
    一度に1つだけ実行されるようにし、レスポンスの混在を防止する
    （"list_clients" はWebSocketラウンドトリップを一切発生させないため、
    このロックの外で処理される）。
    """

    # 新規接続後にidentifyメッセージを待つ最大秒数。
    # 3秒は正規のローカル拡張機能には十分であり、それ以上は遅延または
    # 悪意のあるクライアントのために接続スロットを不必要に開けておくことになる。
    _IDENTIFY_TIMEOUT: float = 3.0

    def __init__(
        self,
        aliases: dict[str, str] | None = None,
        *,
        auto_launch: bool = False,
        profiles: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            aliases: 設定ファイルから読み込み済みのエイリアスマッピング
                     （_load_aliases()の戻り値を想定。キー・値ともにNFC
                     正規化済みであること）。Noneの場合は空dictとして扱う
                     （エイリアスなしで動作）。設計判断: __init__内で
                     _load_aliases()を直接呼ばないのは、既存のテストが
                     ChromeKontrolServer()を引数なしで多数呼び出しており、
                     __init__がファイルI/Oを行うとテスト実行環境の実際の
                     ~/.config/chromekontrol/config.jsonに依存してしまう
                     ため。ファイル読み込みはrun_server()/run_serve_mode()
                     （プロセスエントリーポイント、カバレッジ対象外）側の
                     責務とする。
            auto_launch: 設定ファイルの"autoLaunch"の読み込み結果
                     （_load_auto_launch()の戻り値を想定）。既定False
                     （ISSUES.md P0-1, Phase 2c）。aliasesと同じ理由で
                     __init__は自身でファイルを読まない。
            profiles: 設定ファイルの"profiles"の読み込み結果
                     （_load_profiles()の戻り値を想定）。Noneの場合は空dict
                     として扱う（ISSUES.md P0-1, Phase 2c）。
        """
        # クライアントキー（ClientInfo.key: "browser" または "browser:profileId"）
        # -> ClientInfo のマッピング。
        self._clients: dict[str, ClientInfo] = {}
        # クライアントキー -> 最終フォーカス時刻（ミリ秒epoch、int）のマッピング
        # （ISSUES.md P1-5, Phase F7）。target/browser省略時の自動選択で、
        # 最も新しくフォーカスされたクライアントを選ぶために使う。エントリが
        # 存在しないキーは「フォーカス通知を一度も受け取っていない」ことを
        # 意味する（0や負の値でNoneを表現するのではなく、キーの有無で表現する）。
        self._focus_ts: dict[str, int] = {}
        self._response_event: asyncio.Event = asyncio.Event()
        self._pending_response: dict[str, Any] | None = None
        # 設計判断: 全ての同時呼び出し元が同じイベントループ内で動作するため、
        # threading.LockではなくasyncioのLockを使用する。このロックにより
        # 一度に1つのHTTPリクエストだけがWebSocketラウンドトリップを占有し、
        # 最初の呼び出し元がレスポンスを読む前に2番目の呼び出し元が
        # _response_event / _pending_responseを上書きすることを防ぐ。
        self._command_lock: asyncio.Lock = asyncio.Lock()
        # ISSUES.md P0-1（Phase 2b）: エイリアス名 -> 解決対象文字列 のマッピング。
        self._aliases: dict[str, str] = aliases if aliases is not None else {}
        # ISSUES.md P0-1（Phase 2c）: 自動起動の設定。
        self._auto_launch: bool = auto_launch
        self._profiles: dict[str, str] = profiles if profiles is not None else {}
        # 自動起動が現在進行中（Popen呼び出し後、接続確立 or タイムアウトまで）の
        # 解決対象文字列の集合。_command_lockが同時実行を構造的に禁止するため、
        # 現状はこの集合の要素数が2以上になることは実質起こり得ない
        # （単一のsend_command呼び出し内でしか変化しない）。それでもPhase 4で
        # ロックの扱いを見直す際の安全網として、記録・解除の規律をここで
        # 確立しておく（仕様7節）。
        # NOTE: 現在は_command_lockによる直列化のため冗長。この冗長性は
        # 意図的なもので、Phase 4のreqId対応でロック設計を見直す際に
        # 必要になる想定で保持している（セキュリティリスクなし。分析は
        # Security-Audit.mdのL-7参照）。
        self._launching: set[str] = set()
        # 解決対象文字列 -> 最後に起動を試みた時刻（asyncio.get_running_loop().
        # time()の単調増加値）。60秒のクールダウン判定に使う。成功・失敗を
        # 問わず記録する（仕様7節）。
        self._launch_attempts: dict[str, float] = {}

    async def handle_connection(
        self,
        websocket: websockets.server.WebSocketServerProtocol,
    ) -> None:
        """ブラウザ拡張機能からの受信WebSocket接続を処理する。

        identifyメッセージを待ち、報告されたブラウザ名でクライアントを登録し、
        ソケットが閉じるまで後続のコマンドレスポンスメッセージを処理する。

        Args:
            websocket: 接続済みクライアントのプロトコルオブジェクト。
        """
        # 接続境界でのOriginバリデーション。
        if not _is_allowed_origin(websocket.request_headers):
            logger.warning(
                'Rejected connection from non-localhost origin: %s',
                _sanitise_for_log(websocket.request_headers.get('Origin', '<none>')),
            )
            await websocket.close(code=1008, reason='Forbidden origin')
            return

        # 最初のメッセージとしてidentifyメッセージを期待する。
        client_info = await self._receive_identify(websocket)
        if client_info is None:
            # _receive_identifyが既に適切な理由でソケットを閉じている。
            return

        key = client_info.key

        # H-3: identify重複時の後着拒否
        #
        # 既存接続が生きている場合は後着を拒否する。悪意あるローカルプロセスが
        # 同じkeyでidentifyを送って正規接続を奪うことを防ぐためのセキュリティ強化。
        #
        # ただし「拡張機能の再読み込み」のような正規ケースを壊さないよう、
        # 既存接続が既に閉じている（stale）場合は置換を許可する。
        #
        # ISSUES.md P1-3の修正: 生存判定を `.closed` から `.open` に変更した。
        # websockets 10.xの `.closed` は State.CLOSED のときのみ True を返すため、
        # closeハンドシェイク進行中（CLOSING）を誤って「生きている」と判定してしまう。
        # `.open` は State.OPEN かつ転送タスクが完了していない場合のみ True を返すため、
        # CLOSING/CLOSEDのいずれでも False になり、拡張リロード直後の再接続が
        # 1サイクル分（最大数秒）遅延・拒否され続ける問題を解消する。
        old_client = self._clients.get(key)
        if old_client is not None:
            if old_client.websocket.open:
                # 既存接続が生きている → 後着を拒否する。
                logger.warning(
                    'Rejecting duplicate %s connection; existing connection is still alive.',
                    _sanitise_for_log(key),
                )
                await websocket.close(code=1008, reason='Duplicate connection rejected')
                return
            # 既存接続が閉じている、または閉じつつある（stale）→ 正当な再接続として置換を許可する。
            logger.info(
                'Replacing stale %s connection.',
                _sanitise_for_log(key),
            )

        self._clients[key] = client_info
        # ISSUES.md P1-5（Phase F7）: identifyにfocusTsが含まれていれば
        # _focus_tsへ転記する。_receive_identify()の中では直接書き込まない
        # ——同関数はkey確定（後着拒否の判定）より前に呼ばれるため、そこで
        # 書き込むと拒否された接続の時刻がサーバー状態に残ってしまう。
        # self._clients[key] = client_info が成功した直後のこの位置で転記
        # することで、拒否された接続の影響を受けないようにしている。
        if client_info.focus_ts is not None:
            self._focus_ts[key] = client_info.focus_ts
        logger.info(
            'Extension connected: browser=%s key=%s',
            _sanitise_for_log(client_info.browser),
            _sanitise_for_log(key),
        )

        try:
            async for raw_message in websocket:
                await self._handle_message(raw_message, key)
        except websockets.exceptions.ConnectionClosedOK:
            logger.info('Extension disconnected normally: key=%s', _sanitise_for_log(key))
        except websockets.exceptions.ConnectionClosedError as exc:
            logger.warning(
                'Extension disconnected with error: key=%s error=%s',
                _sanitise_for_log(key),
                _sanitise_for_log(exc),
            )
        finally:
            # この接続(client_info)のみ削除する。代替が既に登録されている可能性がある。
            if self._clients.get(key) is client_info:
                del self._clients[key]
                # ISSUES.md P1-5（Phase F7）: _clientsと同じ条件の内側で削除する。
                # 拡張のリロード等で同じkeyの新しい接続に置換された場合、この
                # finallyは古い接続のものとして走る。条件の外側で削除すると、
                # 新しい接続がidentifyで記録した時刻を消してしまう
                # （既存の_clients削除と同じ理由）。
                self._focus_ts.pop(key, None)
            # コマンド待機中のタスクをアンブロックし、切断を検知できるようにする。
            self._response_event.set()
            logger.info('Extension connection cleaned up: key=%s', _sanitise_for_log(key))

    async def _receive_identify(
        self,
        websocket: websockets.server.WebSocketServerProtocol,
    ) -> ClientInfo | None:
        """新規接続からの初期identifyメッセージを待機しバリデーションする。

        ISSUES.md P0-1（Phase 2a）: browserに加え、任意フィールドの
        profileId / email / label を受理する。いずれも省略可能で、省略時は
        従来どおりbrowserのみでクライアントを識別する（Phase 3で拡張側
        (background.js)がprofileIdを送るようになるまでの後方互換）。
        いずれかのフィールドが存在するが不正な形式の場合は、browser自体が
        正しくても接続を拒否する（部分的に不正な識別情報を許容しない）。

        Args:
            websocket: 新たに受け入れたWebSocket接続。

        Returns:
            成功時はClientInfo（このwebsocketを含む）、
            接続が拒否された場合はNone（その場合ソケットは既に閉じられている）。
        """
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=self._IDENTIFY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning('Identify timeout; closing connection.')
            await websocket.close(code=1008, reason='Identify timeout')
            return None
        except websockets.exceptions.ConnectionClosed:
            logger.warning('Connection closed before identify message received.')
            return None

        # サイズガード（_handle_messageと同様）。
        raw_bytes = raw if isinstance(raw, bytes) else raw.encode('utf-8')
        if len(raw_bytes) > MAX_MESSAGE_BYTES:
            logger.warning('Identify message too large; closing connection.')
            await websocket.close(code=1008, reason='Message too large')
            return None

        try:
            data = json.loads(raw_bytes)
        except json.JSONDecodeError:
            logger.warning('Non-JSON identify message; closing connection.')
            await websocket.close(code=1008, reason='Invalid JSON in identify')
            return None

        # 前方互換性のために {"type": "identify", "browser": "..."} と
        # 素の {"browser": "..."} の両方を受け入れる。
        if not isinstance(data, dict):
            logger.warning('Identify message is not a JSON object; closing connection.')
            await websocket.close(code=1008, reason='Invalid identify format')
            return None

        browser = data.get('browser')
        if not isinstance(browser, str) or not browser:
            logger.warning('Identify message missing browser field; closing connection.')
            await websocket.close(code=1008, reason='Missing browser field in identify')
            return None

        # ブラウザ名を安全な表示可能ASCIIに制限し、ログインジェクションや
        # dictキーの想定外の挙動を防ぐ。
        if not browser.isascii() or not browser.isprintable() or len(browser) > 64:
            logger.warning('Identify browser field contains invalid characters; closing connection.')
            await websocket.close(code=1008, reason='Invalid browser field value')
            return None

        # 許可リスト内のブラウザ名のみ受け入れ、予期しないクライアントが
        # 任意の名前で登録するのを防ぐ。
        if browser not in ALLOWED_BROWSERS:
            logger.warning(
                'Identify browser field not in allowlist: %s; closing connection.',
                _sanitise_for_log(browser),
            )
            await websocket.close(code=1008, reason='Unknown browser')
            return None

        # ISSUES.md P0-1（Phase 2a）: profileId / email / label は全て任意フィールド。
        # 存在しない場合はNoneのまま扱い、ClientInfo.keyが従来どおりbrowserのみに
        # なるようにする（拡張がまだPhase 3のフィールドを送らない段階の後方互換）。
        profile_id = data.get('profileId')
        if profile_id is not None:
            # ":" はClientInfo.keyの区切り文字として使うため、profileId自体に
            # 含まれているとキー構成が破綻する。単独でチェックする必要があるため
            # _is_valid_identify_fieldの共通チェックとは別にorで連結する
            # （左辺がFalseの時点で短絡し、非文字列型に対する`':' in profile_id`の
            # TypeErrorを避けられる）。
            if (
                not _is_valid_identify_field(profile_id, max_length=MAX_PROFILE_ID_LENGTH, require_ascii=True)
                or ':' in profile_id
            ):
                logger.warning('Identify profileId field is invalid; closing connection.')
                await websocket.close(code=1008, reason='Invalid profileId field value')
                return None

        email = data.get('email')
        if email is not None:
            # ISSUES.md P0-1（Phase 2b）: エイリアス解決でemailのローカルパート
            # 一致を使うため、"@"をちょうど1個含み、ローカルパート・ドメイン部
            # がともに1文字以上であることを要求する（RFC厳密準拠は不要）。
            if not _is_valid_identify_field(
                email, max_length=MAX_EMAIL_LENGTH, require_ascii=True, require_email_format=True
            ):
                logger.warning('Identify email field is invalid; closing connection.')
                await websocket.close(code=1008, reason='Invalid email field value')
                return None

        label = data.get('label')
        if label is not None:
            # labelのみ非ASCII（日本語ラベル等）を許可する。
            if not _is_valid_identify_field(label, max_length=MAX_LABEL_LENGTH, require_ascii=False):
                logger.warning('Identify label field is invalid; closing connection.')
                await websocket.close(code=1008, reason='Invalid label field value')
                return None
            # Security-Audit.md L-4: NFC正規化する。NFC形とNFD（結合文字分解）形は
            # isprintable()をどちらも通過するがバイト列が異なるため、正規化なしでは
            # 見た目が同じラベルがPhase 2bのエイリアス解決（label完全一致）で
            # 一致しない事態になりうる。
            label = unicodedata.normalize('NFC', label)

        # ISSUES.md P1-5（Phase F7）: focusTsは他の3フィールドと異なり、
        # 不正でも接続を拒否しない——「取得できたフィールドのみで続行する」
        # という既存の任意フィールド全体の方針に合わせ、無視してNoneのまま
        # 続行する（identify自体は成功させる）。
        focus_ts_raw = data.get('focusTs')
        focus_ts: int | None = None
        if _is_valid_positive_timestamp(focus_ts_raw):
            focus_ts = int(focus_ts_raw)

        return ClientInfo(
            browser=browser,
            websocket=websocket,
            profile_id=profile_id,
            email=email,
            label=label,
            focus_ts=focus_ts,
        )

    async def _handle_message(self, raw: str | bytes, key: str) -> None:
        """拡張機能からのコマンドレスポンスメッセージをパースし保存する。

        identifyメッセージはここでは期待されない（メインメッセージループ開始前に
        _receive_identifyで消費される）。

        Args:
            raw: 生のWebSocketメッセージ（バイトまたは文字列）。
            key: 送信元クライアントのClientInfo.key（ISSUES.md P1-5, Phase F7）。
                 このメッセージ自体にはどのクライアントからの通知かを示す情報が
                 含まれていないため、呼び出し元（handle_connection）が保持する
                 keyを渡してもらう必要がある。{"type": "focus", ...}通知の
                 記録先（_focus_ts[key]）を決めるために使う。
        """
        if isinstance(raw, bytes):
            if len(raw) > MAX_MESSAGE_BYTES:
                logger.error('Incoming message exceeds size limit; discarding.')
                self._pending_response = {'result': 'error', 'message': 'Response too large.'}
                self._response_event.set()
                return
            raw = raw.decode('utf-8', errors='replace')
        elif len(raw.encode('utf-8')) > MAX_MESSAGE_BYTES:
            logger.error('Incoming message exceeds size limit; discarding.')
            self._pending_response = {'result': 'error', 'message': 'Response too large.'}
            self._response_event.set()
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning('Received non-JSON response from extension; discarding.')
            self._pending_response = {'result': 'error', 'message': 'Non-JSON response from extension.'}
            self._response_event.set()
            return

        # "type" フィールドで分岐する（ISSUES.md P0-1, Phase 2a）。
        # "type" は旧互換のため省略可能: 拡張からの現行コマンドレスポンスは
        # "type" を持たないため、そのケースは従来どおりコマンドレスポンスとして扱う。
        if isinstance(data, dict):
            msg_type = data.get('type')
            if msg_type == 'identify':
                # identifyメッセージはハンドシェイク後に出現すべきではないが、
                # コマンドレスポンスとして扱うのを避けるため警告して無視する。
                logger.warning('Unexpected identify message received after handshake; ignoring.')
                return
            if msg_type == 'focus':
                # ISSUES.md P1-5（Phase F7）: 最終フォーカス時刻の通知。
                # これはコマンドへの応答ではなく拡張機能からの一方的な通知
                # なので、_pending_response / _response_eventには一切触れない
                # （触れると、待機中のsend_command呼び出しがこの通知を
                # 自分へのレスポンスと誤認してしまう）。
                self._handle_focus_notification(key, data.get('ts'))
                return
            if msg_type is not None:
                # "identify" / "focus" 以外の既知でない "type" は黙って無視する。
                # 将来追加される未知の通知種別を、サーバーがまだ対応していない
                # 段階で受け取っても、直前のコマンドのレスポンスとして
                # 誤認しないようにするための前方互換措置。
                return

        self._pending_response = data
        self._response_event.set()

    def _handle_focus_notification(self, key: str, ts: Any) -> None:
        """{"type": "focus", "ts": ...}通知を検証し、_focus_tsへ記録する。

        ISSUES.md P1-5（Phase F7）: 各クライアントが最後にフォーカスを得た
        時刻をサーバー側で保持し、target/browser省略時の自動選択（
        _resolve_client参照）に使う。

        tsが不正（負数・0・非数値・bool・欠落）な場合は記録を更新せず、
        エラーレスポンスも返さない——これは拡張機能からの一方的な通知であり
        サーバーへの要求（コマンド）ではないため、黙って無視する。

        Args:
            key: 通知元クライアントのClientInfo.key。
            ts: focus通知の"ts"フィールド値（未検証、dict.get()の戻り値の
                ためAny型）。
        """
        if not _is_valid_positive_timestamp(ts):
            return
        self._focus_ts[key] = int(ts)

    async def send_command(
        self,
        command: dict[str, Any],
        browser: str | None = None,
        target: str | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """バリデーション済みコマンドを接続中の拡張機能に送信し、レスポンスを待つ。

        WebSocketラウンドトリップ前に_command_lockを取得し、同時呼び出し元を
        直列化する（サーブモード）。ワンショットモードでは呼び出し元は最大1つのため、
        ロックは競合せずオーバーヘッドもない。

        ISSUES.md P0-1（Phase 2a）: "list_clients" はサーバーが保持する
        ClientInfo一覧のみで完結して応答できるため、拡張機能へのラウンドトリップ
        （と、それを直列化する_command_lock）を経由せず即座に返す。

        Args:
            command: バリデーション済みコマンドdict（例: {"cmd": "get_dom"}）。
            browser: 対象ブラウザ名（ALLOWED_BROWSERSのいずれか。例: "chrome",
                     "edge", "firefox"）。Noneの場合:
                     - クライアントが1つだけ接続中ならそれを使用。
                     - 複数接続中ならエラーを返す。
                     targetと同時に指定されることはない
                     （_validate_commandが既に拒否している）。
            target: 対象クライアントのエイリアス名または"browser:identifier"形式
                     の文字列（ISSUES.md P0-1, Phase 2b）。指定された場合は
                     browserより優先され、_resolve_client内でエイリアス解決
                     アルゴリズムに回される。browserと異なり、未接続の場合でも
                     接続を待たず即座にエラーを返す
                     （プロファイル自動起動はPhase 2cの対象）。
            timeout: 拡張機能の接続とレスポンスの両方を待つ最大秒数。
                     同じ時間枠で両方の待機をカバーする。

        Returns:
            拡張機能からのレスポンスdict、または"list_clients"の場合は
            サーバー内部で組み立てたレスポンスdict。
        """
        if command.get('cmd') == 'list_clients':
            return self._list_clients_response()
        async with self._command_lock:
            return await self._send_command_locked(command, browser, target, timeout)

    def _list_clients_response(self) -> dict[str, Any]:
        """ "list_clients"コマンドのレスポンスを組み立てる。

        拡張機能への転送は行わない。サーバーが保持するClientInfoの情報のみで
        完結して応答できるため（ISSUES.md P0-1, Phase 2a）。

        Returns:
            {'result': 'ok', 'data': [...]} 形式のレスポンス。
            dataは各クライアントの browser / profileId / email / label /
            displayName / key / aliases を含み、keyの昇順で安定ソートされる。
            表示名（displayName）が複数クライアント間で重複してもデータは
            間引かない（keyが一意なので呼び出し側は区別できる）。
            aliases（ISSUES.md P0-1, Phase 2b）はそのクライアントに一意に
            解決される設定ファイル由来のエイリアス名を辞書順で列挙したもの。
            該当がなければ空リスト。
        """
        data = [
            {
                'key': client.key,
                'browser': client.browser,
                'profileId': client.profile_id,
                'email': client.email,
                'label': client.label,
                'displayName': client.display_name,
                'aliases': self._aliases_for_client(client),
            }
            for client in sorted(self._clients.values(), key=lambda c: c.key)
        ]
        return {'result': 'ok', 'data': data}

    def _aliases_for_client(self, client: ClientInfo) -> list[str]:
        """`client` に一意に解決される設定ファイル由来のエイリアス名を列挙する。

        ISSUES.md P0-1（Phase 2b）list_clients拡張（仕様6節）: 「そのクライアント
        に解決されるエイリアスをすべて列挙する」の"解決される"は一意解決を指す
        と解釈する。あるエイリアスの値が現在の接続状況で複数クライアントに
        マッチする（曖昧）場合や、どのクライアントにもマッチしない場合は、
        そのエイリアスをどのクライアントの一覧にも含めない
        （_resolve_by_targetが実際にtargetとして使われた際の曖昧性エラー・
        未接続エラーと一貫した挙動）。

        _resolve_resolved_string()を再利用する。エイリアス値はロード時に
        既にNFC正規化済みで、かつここでは「別名の再解決」ではなく「このエイリアス
        自身の値」をそのまま解決対象文字列として使うため、_apply_alias()の
        二重解決は経由しない。

        Args:
            client: 判定対象のClientInfo。

        Returns:
            辞書順にソートされたエイリアス名のリスト。該当なしなら空リスト。
        """
        matching_keys = []
        for alias_key, alias_value in self._aliases.items():
            resolved = self._resolve_resolved_string(alias_value, alias_value, was_alias=False)
            if isinstance(resolved, ClientInfo) and resolved is client:
                matching_keys.append(alias_key)
        return sorted(matching_keys)

    async def _send_command_locked(
        self,
        command: dict[str, Any],
        browser: str | None,
        target: str | None,
        timeout: float,
    ) -> dict[str, Any]:
        """_command_lockを保持した状態でWebSocketラウンドトリップを実行する。

        ロックの不変条件を維持するため、send_commandからのみ呼び出すこと。

        Args:
            command: バリデーション済みコマンドdict。
            browser: 対象ブラウザ名、またはNoneで自動選択。
            target: 対象クライアントのエイリアス名/"browser:identifier"形式
                     文字列、またはNone（ISSUES.md P0-1, Phase 2b）。
            timeout: 接続待機とレスポンス待機を合わせた最大秒数。

        Returns:
            拡張機能からのレスポンスdict。
        """
        # 対象クライアントを解決する。必要に応じて待機する。
        client = await self._resolve_client(browser, timeout, target=target)
        if isinstance(client, dict):
            # _resolve_clientがエラーdictを返した。
            return client

        self._response_event.clear()
        self._pending_response = None

        try:
            await client.send(json.dumps(command))
        except websockets.exceptions.ConnectionClosed:
            return {'result': 'error', 'message': 'Extension disconnected before command was sent.'}

        try:
            await asyncio.wait_for(self._response_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {'result': 'error', 'message': f'Timed out waiting for extension response ({timeout}s).'}

        # 切断トリガーのイベントと実際のレスポンスを区別する。
        # handle_connectionはfinallyブロック内でWebSocket閉鎖時に
        # _response_event.set()を呼ぶ。クライアントが残っておらず
        # レスポンスも到着していなければ、待機中に拡張機能が切断されたことを意味する。
        if not self._clients and self._pending_response is None:
            return {'result': 'error', 'message': 'Extension disconnected while waiting for response.'}

        return self._pending_response or {'result': 'error', 'message': 'Empty response from extension.'}

    async def _resolve_client(
        self,
        browser: str | None,
        timeout: float,
        target: str | None = None,
    ) -> websockets.server.WebSocketServerProtocol | dict[str, Any]:
        """対象WebSocketクライアントを解決する。必要に応じて接続を待機する。

        ISSUES.md P0-1（Phase 2a）: `_clients` のキーがbrowser単位から
        browser:profileId単位に拡張されたため、"browser"フィールドは
        「該当ブラウザの全プロファイル」に一致しうる。1つだけ一致すれば
        自動選択し、複数一致する場合は候補一覧を含むエラーを返す
        （既存の"browser"フィールドは後方互換として維持）。

        ISSUES.md P0-1（Phase 2b）: `target` が指定された場合はエイリアス
        解決アルゴリズム（_resolve_by_target）に委譲し、`browser` の待機
        ロジックは経由しない。

        ISSUES.md P0-1（Phase 2c）: `target` の解決結果が「未接続」（曖昧
        ではなく、現在の一致候補が0件）の場合、以下の発動条件（仕様8節）を
        すべて満たせばサーバーがブラウザプロセスを自動起動し、接続を待つ
        （_auto_launch_response()参照。待機はAUTO_LAUNCH_WAIT_TIMEOUT秒固定
        で、この`timeout`引数とは独立している）。いずれか1つでも満たさない
        場合は、その理由を示すエラーメッセージを返す:
          1. config.jsonの"autoLaunch"がtrue
          2. targetフィールドが指定されている（このメソッドに到達した時点で
             自明に満たされる）
          3. 解決対象文字列が"profiles"に登録されている
          4. 該当するクライアントが接続していない（0件。曖昧＝2件以上では
             ない）
          5. そのプロファイルがクールダウン中でない
          6. ブラウザ実行ファイルが見つかる
        曖昧性エラー（候補2件以上）は自動起動を試みず、_resolve_by_targetが
        返した曖昧性エラーをそのまま返す——起動しても曖昧性は解消しないため。
        「未接続」か「曖昧」かは、_resolve_by_targetのエラーメッセージ文字列
        を解析するのではなく、resolved文字列に対する現在の一致候補数を
        _count_candidates_for_resolved()で独立に再計算して判定する。

        ISSUES.md P1-5（Phase F7）: `browser`・`target`ともに未指定で複数
        クライアントが接続中の場合、従来は無条件で曖昧性エラーを返していたが、
        `_focus_ts`（各クライアントの最終フォーカス時刻）に記録があれば
        その中で最も新しいものを自動選択する。全クライアントが未記録
        （旧仕様の拡張機能のみ接続、またはフォーカス通知がまだ一度も
        届いていない）の場合のみ、従来どおりの曖昧性エラーにフォールバック
        する。このパスは`target`指定時（上記）には適用されない——targetが
        指定された時点で解決先は一意に決まるべきであり、フォーカス時刻で
        代替選択する余地がないため。

        呼び出し元（_validate_command）が既にtarget/browser同時指定を
        拒否しているため、両方が非Noneになることはない。

        Args:
            browser: 希望するブラウザ名、またはNoneで自動選択。
            timeout: クライアント接続を待つ最大秒数（targetパスでは未使用。
                     自動起動の接続待機は独立した固定値を使う）。
            target: エイリアス名または"browser:identifier"形式の対象指定文字列。

        Returns:
            解決されたWebSocketServerProtocol、または解決に失敗した場合はエラーdict。
        """
        if target is not None:
            resolved_or_error = self._resolve_by_target(target)
            if isinstance(resolved_or_error, ClientInfo):
                return resolved_or_error.websocket

            # "未接続"（曖昧ではない）の場合のみ自動起動を検討する。エイリアス
            # 解決と候補数の再計算は_resolve_by_targetの内部と同じ手順を辿るが、
            # 意図的に独立して再実行する（レスポンスに内部状態を漏らさず、
            # かつ_resolve_by_target自体には一切手を加えないため）。
            normalised_target = unicodedata.normalize('NFC', target)
            resolved_string, _was_alias = self._apply_alias(normalised_target)
            if self._count_candidates_for_resolved(resolved_string) != 0:
                # 曖昧（1件以上でresolved_or_errorがClientInfoでなかったのは
                # 複数一致による曖昧性エラーのケースのみ）。自動起動しても
                # 解消しないため、元のエラーをそのまま返す。
                return resolved_or_error

            return await self._auto_launch_response(resolved_string, resolved_or_error['message'])

        if browser is not None:
            matches = [client for client in self._clients.values() if client.browser == browser]
            if not matches:
                # 指定されたブラウザの接続を待つ。
                logger.info(
                    'Waiting for %s extension to connect (up to %.0fs)...',
                    _sanitise_for_log(browser),
                    timeout,
                )
                try:
                    await asyncio.wait_for(
                        self._wait_for_client(browser=browser),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    return {
                        'result': 'error',
                        'message': (
                            f'Timed out waiting for {browser} extension. '
                            f'Is ChromeKontrol loaded in {browser.capitalize()}?'
                        ),
                    }
                matches = [client for client in self._clients.values() if client.browser == browser]
                if not matches:
                    # 待機完了とこの参照の間に切断された。
                    return {'result': 'error', 'message': f'{browser} extension disconnected unexpectedly.'}

            if len(matches) == 1:
                return matches[0].websocket

            # 同一ブラウザの複数プロファイルが接続中: どちらを使うか決められない。
            return {'result': 'error', 'message': _format_ambiguous_clients_message(matches)}

        # ブラウザ指定なし: クライアントが接続されていなければいずれかの接続を待つ。
        if not self._clients:
            logger.info('Waiting for any extension to connect (up to %.0fs)...', timeout)
            try:
                await asyncio.wait_for(self._wait_for_client(browser=None), timeout=timeout)
            except asyncio.TimeoutError:
                browser_list = _format_browser_list(name.capitalize() for name in sorted(ALLOWED_BROWSERS))
                return {
                    'result': 'error',
                    'message': f'Timed out waiting for extension. Is ChromeKontrol loaded in {browser_list}?',
                }

        # 自動選択: 接続クライアントが1つだけなら曖昧さがない。
        if len(self._clients) == 1:
            return next(iter(self._clients.values())).websocket

        # ISSUES.md P1-5（Phase F7）: 複数クライアント接続中でも、_focus_ts
        # に記録があるクライアントが1つ以上あれば、その中で最終フォーカス
        # 時刻が最も新しいものを自動選択する。tsが同値の場合はkeyの昇順で
        # 先頭を選び、決定論的にする（min()/max()の同値時の挙動に依存
        # しない）。_focus_tsに記録があるが_clientsに存在しないキー
        # （切断済みクライアント。cleanupで消えるはずだが防御的に）は
        # self._clients.values()を起点にしているため自然に候補から除外される。
        focused_candidates = [client for client in self._clients.values() if client.key in self._focus_ts]
        if focused_candidates:
            chosen = min(focused_candidates, key=lambda c: (-self._focus_ts[c.key], c.key))
            logger.info('Auto-selected most recently focused client: key=%s', _sanitise_for_log(chosen.key))
            return chosen.websocket

        # 複数クライアント接続中でブラウザ指定なし、かつ誰もフォーカス情報を
        # 持っていない: エラーを返す。
        return {
            'result': 'error',
            'message': _format_ambiguous_clients_message(list(self._clients.values())),
        }

    def _apply_alias(self, normalised_target: str) -> tuple[str, bool]:
        """エイリアスの再帰を1回だけ解決する（仕様3-1 ステップ1）。

        Args:
            normalised_target: NFC正規化済みのtarget文字列。

        Returns:
            (resolved_string, was_alias) のタプル。normalised_targetが
            self._aliasesのいずれかのキーとcasefold一致すれば、そのエイリアス
            の値をそのまま返す（値を再度aliasesのキーとして解決することは
            しない——再帰は1回のみ）。一致しなければnormalised_targetを
            そのまま返す。
        """
        folded = normalised_target.casefold()
        for alias_key, alias_value in self._aliases.items():
            if alias_key.casefold() == folded:
                return alias_value, True
        return normalised_target, False

    def _resolve_by_target(self, target: str) -> ClientInfo | dict[str, Any]:
        """`target` フィールド（ISSUES.md P0-1, Phase 2b）を解決する。

        NFC正規化 → エイリアス解決（1回のみ） → _resolve_resolved_string()
        による本体解決、の順で処理する。

        Args:
            target: コマンドの"target"フィールド値（バリデーション済みの
                     1〜MAX_TARGET_LENGTH文字の文字列）。

        Returns:
            解決されたClientInfo、または解決に失敗した場合はエラーdict
            （曖昧性エラーまたは未接続エラー）。
        """
        normalised_target = unicodedata.normalize('NFC', target)
        resolved, was_alias = self._apply_alias(normalised_target)
        return self._resolve_resolved_string(resolved, normalised_target, was_alias)

    def _match_candidates_for_resolved(self, resolved: str) -> list[ClientInfo] | dict[str, Any]:
        """resolved文字列に一致する現在接続中のクライアント候補を計算する
        （仕様3-2, 3-3の絞り込みロジック本体）。

        _resolve_resolved_string()（曖昧性/未接続の最終判定）と
        _count_candidates_for_resolved() / _wait_for_target()（ISSUES.md
        P0-1, Phase 2c: 自動起動の判定・起動後の接続待機）の両方から共有
        される。browser_partが不正な場合のみエラーdictを返し、それ以外は
        候補リスト（0件・1件・複数件のいずれも）を返す
        （最終判定は呼び出し元の責務）。

        Args:
            resolved: 解決対象文字列（":"で分割する対象）。

        Returns:
            一致するClientInfoのリスト、またはbrowser_partが許可リスト外の
            場合はエラーdict。
        """
        browser_part, identifier_part = _split_resolved_target(resolved)

        if browser_part is not None:
            if browser_part.casefold() not in ALLOWED_BROWSERS:
                return {
                    'result': 'error',
                    'message': f"Unknown browser '{_sanitise_for_log(browser_part)}' in target.",
                }
            candidates = [c for c in self._clients.values() if c.browser == browser_part.casefold()]
            if identifier_part == '*':
                return candidates
            return _match_by_identifier_order(candidates, identifier_part)

        # browser_partなし: まずidentifier_part全体をブラウザ名として試す。
        if identifier_part.casefold() in ALLOWED_BROWSERS:
            return [c for c in self._clients.values() if c.browser == identifier_part.casefold()]

        # ブラウザ名でもなければ、接続中の全クライアントを対象に識別子照合順序で絞り込む。
        return _match_by_identifier_order(list(self._clients.values()), identifier_part)

    def _resolve_resolved_string(
        self,
        resolved: str,
        original_target: str,
        was_alias: bool,
    ) -> ClientInfo | dict[str, Any]:
        """エイリアス解決"後"の文字列を実際のクライアントに解決する（仕様3-2, 3-3）。

        _resolve_by_target()から呼ばれる場合は`resolved`がエイリアス解決を
        経た（かもしれない）文字列、_aliases_for_client()から呼ばれる場合は
        エイリアス値そのもの（さらなるエイリアス解決は行わない）。

        Args:
            resolved: 解決対象文字列（":"で分割する対象）。
            original_target: エラーメッセージ用の元のtarget文字列
                     （_aliases_for_client()からの呼び出しではresolvedと同一の
                     ダミー値。was_alias=Falseの場合はエラーメッセージに
                     originalは使われないため実質無関係）。
            was_alias: resolvedがエイリアス解決によって得られた値かどうか。

        Returns:
            解決されたClientInfo、または解決に失敗した場合はエラーdict。
        """
        matched_or_error = self._match_candidates_for_resolved(resolved)
        if isinstance(matched_or_error, dict):
            return matched_or_error
        return self._finalise_candidates(matched_or_error, original_target, resolved, was_alias)

    def _count_candidates_for_resolved(self, resolved: str) -> int:
        """resolved文字列に現在一致する接続中クライアントの数を返す
        （ISSUES.md P0-1, Phase 2c: 自動起動の発動条件4「該当するクライアント
        が接続していない」の判定に使う）。

        _match_candidates_for_resolved()を再利用する。browser_partが不正な
        場合（エラーdict）は0件として扱う——そもそも自動起動が意味を持つ
        ケースではないため（_auto_launch_response()側でbrowser_partの妥当性
        を別途確認する）。

        Args:
            resolved: NFC正規化済みの解決対象文字列。

        Returns:
            一致する接続中クライアントの数。
        """
        matched_or_error = self._match_candidates_for_resolved(resolved)
        if isinstance(matched_or_error, dict):
            return 0
        return len(matched_or_error)

    def _finalise_candidates(
        self,
        matched: list[ClientInfo],
        original_target: str,
        resolved: str,
        was_alias: bool,
    ) -> ClientInfo | dict[str, Any]:
        """絞り込み結果を最終判定する: 一意解決・曖昧性エラー・未接続エラーのいずれか。

        ワイルドカード("*")パス・裸のブラウザ名パス・4段階識別子照合パスの
        いずれからも共通で呼ばれる。

        Args:
            matched: 絞り込み後の候補リスト（0件、1件、複数件のいずれもありうる）。
            original_target: 未接続エラーメッセージ用の元のtarget文字列。
            resolved: 未接続エラーメッセージ用の解決後文字列。
            was_alias: resolvedがエイリアス解決によって得られた値かどうか。

        Returns:
            matchedが1件ならそのClientInfo、複数なら曖昧性エラーdict、
            0件なら未接続エラーdict。
        """
        if len(matched) == 1:
            return matched[0]
        if len(matched) > 1:
            return {'result': 'error', 'message': _format_ambiguous_clients_message(matched)}
        return {
            'result': 'error',
            'message': self._format_not_connected_message(original_target, resolved, was_alias),
        }

    def _format_not_connected_message(self, original_target: str, resolved: str, was_alias: bool) -> str:
        """target解決は成功したが該当クライアントが未接続の場合のエラーメッセージを組み立てる
        （仕様4-2）。

        Args:
            original_target: ユーザーが指定した元のtarget文字列。
            resolved: エイリアス解決後（または最初からエイリアスでなかった）の
                     解決対象文字列。
            was_alias: original_targetがエイリアスとして解決されたかどうか。
                     Trueならoriginal_targetとresolvedの両方をメッセージに
                     含める。Falseならresolvedのみでよい（仕様4-2）。

        Returns:
            "Target '...' resolved to '...', but no matching client is
            connected. Connected: ...." 形式（またはエイリアスでない場合の
            簡略形）のエラーメッセージ本文。接続中クライアントが0件の場合は
            "Connected: (none)." と明示する。候補列挙は`display_name`を使わず
            `_format_client_candidates()`（key + label形式）に委ねる——
            ISSUES.md P0-5、`_format_client_candidates()`のdocstring参照。
            original_target/resolvedは呼び出し元が渡すtargetフィールド由来
            （形式チェックのみ済みで印字可能性は未検証）のため、他のエラー
            メッセージ組み立て箇所と同様に_sanitise_for_log()を通してから
            埋め込む。
        """
        connected = _format_client_candidates(self._clients.values())
        if not connected:
            connected = '(none)'
        safe_resolved = _sanitise_for_log(resolved)
        if was_alias:
            safe_original = _sanitise_for_log(original_target)
            return (
                f"Target '{safe_original}' resolved to '{safe_resolved}', but no matching client is connected. "
                f'Connected: {connected}.'
            )
        return f"Target '{safe_resolved}' is not connected. Connected: {connected}."

    def _lookup_profile_directory(self, resolved: str) -> str | None:
        """resolved文字列（エイリアス解決後の解決対象文字列）に対応する
        プロファイルディレクトリ名を"profiles"設定から取得する
        （ISSUES.md P0-1, Phase 2c、仕様8節条件3）。

        大文字小文字を無視して比較する（エイリアス解決・クライアント照合の
        他の箇所との一貫性のため）。self._profilesのキーは_load_profiles()
        でNFC正規化済み、resolvedも呼び出し元（_resolve_client）でNFC
        正規化済みであることを前提とする。

        Args:
            resolved: NFC正規化済みの解決対象文字列。

        Returns:
            一致するプロファイルディレクトリ名。該当なしならNone。
        """
        folded = resolved.casefold()
        for key, directory in self._profiles.items():
            if key.casefold() == folded:
                return directory
        return None

    async def _wait_for_target(self, resolved: str, timeout: float) -> ClientInfo | None:
        """自動起動後、resolvedが一意のClientInfoとして解決可能になるまで
        ポーリングする（ISSUES.md P0-1, Phase 2c、仕様6節）。

        _wait_for_client()と同じ0.1秒間隔のポーリング方式を使う。
        _match_candidates_for_resolved()の結果が複数件（曖昧）になった場合
        （ユーザーが自動起動と並行して別のプロファイルも手動で開いた等の
        レアケース）は、曖昧性エラーを返さずポーリングを継続する——
        いずれ1件に収束するかタイムアウトするまで待つ方が、起動直後の
        断片的な状態を誤ってエラー扱いするより安全なため。

        Args:
            resolved: エイリアス解決後の解決対象文字列。
            timeout: 最大待機秒数。

        Returns:
            一意に解決できればそのClientInfo、タイムアウトすればNone。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            matched_or_error = self._match_candidates_for_resolved(resolved)
            if isinstance(matched_or_error, list) and len(matched_or_error) == 1:
                return matched_or_error[0]
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(0.1)

    async def _auto_launch_response(
        self,
        resolved: str,
        not_connected_message: str,
    ) -> websockets.server.WebSocketServerProtocol | dict[str, Any]:
        """未接続確定済み（曖昧ではなく候補0件）のresolved文字列に対し、
        自動起動の発動条件（仕様8節）を順に確認する（ISSUES.md P0-1,
        Phase 2c）。

        条件を1つでも満たさない場合、その理由（仕様9節）をnot_connected_
        messageに追記した形で返す——Phase 2bのnot-connectedメッセージ
        （エイリアス名・接続中クライアント一覧を含む）を土台にすることで、
        自動起動が無効な環境でもPhase 2b時点と同様の情報量を保ったまま
        追加の理由を伝えられる。全条件を満たせば実際にブラウザを起動し、
        接続を待つ。

        呼び出し元（_resolve_client）は、resolvedへの一致候補が0件である
        ことを_count_candidates_for_resolved()で確認済みの前提で呼ぶこと。

        ロックについての重要な注意: このメソッドはsend_command() ->
        _send_command_locked() -> _resolve_client() の呼び出し chain の中で
        _command_lockを保持したまま実行される。起動後の接続待機
        （最大AUTO_LAUNCH_WAIT_TIMEOUT=30秒）の間、他の全コマンドがブロック
        される。これは既存の_wait_for_client()（15秒待機）と同じ設計判断
        （ロックを保持したまま待つ）を踏襲したものであり、単一ユーザーの
        ローカルツールで並行リクエストが稀なことを踏まえた現状維持である。
        ロックを外すと_pending_responseが単一スロットであることに起因する
        競合（ISSUES.md P1-1）が発生するため、Phase 4のreqId対応時に
        改めて見直す。

        Args:
            resolved: エイリアス解決後の解決対象文字列（NFC正規化済み）。
            not_connected_message: _resolve_by_targetが計算した、Phase 2b
                     時点のnot-connectedエラーメッセージ本文。

        Returns:
            起動・接続に成功すればWebSocketServerProtocol、それ以外は
            {'result': 'error', 'message': ...} 形式のエラーdict。
        """
        # 発動条件1: autoLaunchが有効であること。
        if not self._auto_launch:
            return {
                'result': 'error',
                'message': (
                    f'{not_connected_message} Auto-launch is disabled; set "autoLaunch": true in the '
                    f'config file to enable it, or open the browser window manually.'
                ),
            }

        # 発動条件3: 解決対象文字列が"profiles"に登録されていること
        # （発動条件2「targetフィールドが指定されている」は、このメソッドに
        # 到達した時点で自明に満たされている）。
        profile_directory = self._lookup_profile_directory(resolved)
        if profile_directory is None:
            return {
                'result': 'error',
                'message': f'{not_connected_message} No matching entry in "profiles" for auto-launch.',
            }

        # profilesのキーは"aliases"の値と同じ"browser:identifier"形式である
        # ことが前提だが、設定ミスでbrowser部を欠く/不正な値が登録されている
        # 可能性がある。安全側に倒して自動起動を行わない。
        browser_part, _identifier_part = _split_resolved_target(resolved)
        if browser_part is None or browser_part.casefold() not in ALLOWED_BROWSERS:
            return {
                'result': 'error',
                'message': (
                    f'{not_connected_message} The "profiles" entry does not specify a recognised '
                    f'browser; auto-launch skipped.'
                ),
            }
        browser = browser_part.casefold()

        # 発動条件4「該当するクライアントが接続していない」は呼び出し元
        # （_resolve_client）が_count_candidates_for_resolved()で確認済み。

        # 発動条件5: クールダウン中でないこと。試みた時刻からの経過で判定する
        # （成功・失敗を問わず記録される。仕様7節）。
        loop = asyncio.get_running_loop()
        now = loop.time()
        last_attempt = self._launch_attempts.get(resolved)
        if last_attempt is not None and (now - last_attempt) < AUTO_LAUNCH_COOLDOWN_SECONDS:
            remaining = AUTO_LAUNCH_COOLDOWN_SECONDS - (now - last_attempt)
            return {
                'result': 'error',
                'message': (
                    f'{not_connected_message} Auto-launch was attempted recently; waiting out a '
                    f'cooldown ({remaining:.0f}s remaining) before retrying.'
                ),
            }

        # 発動条件6: ブラウザ実行ファイルが見つかること。
        #
        # BROWSER_EXECUTABLE_CANDIDATESにキーを持たないブラウザ（現時点では
        # firefoxのみ。定義箇所のコメント参照）と、キーはあるがshutil.which()
        # でどの候補も解決できないケースを区別する。前者は「自動起動に対応
        # していない」、後者は「PATH上に実行ファイルが見つからない」であり、
        # ユーザーへの案内が異なるため別メッセージを返す。以前は両者を区別
        # せずBROWSER_EXECUTABLE_CANDIDATES[browser]で直接キーアクセスして
        # おり、前者のケースでKeyErrorになる欠陥があった（Phase F2で修正）。
        candidates = BROWSER_EXECUTABLE_CANDIDATES.get(browser, ())
        if not candidates:
            return {
                'result': 'error',
                'message': (
                    f'{not_connected_message} Auto-launch is not supported for {browser}; '
                    f'open the browser window manually.'
                ),
            }
        executable = _resolve_browser_executable(browser)
        if executable is None:
            candidates_str = ', '.join(candidates)
            return {
                'result': 'error',
                'message': (
                    f'{not_connected_message} Cannot auto-launch {browser}: none of the expected '
                    f'executables ({candidates_str}) were found on PATH.'
                ),
            }

        # 全ての発動条件を満たした。Popen呼び出し直前に「起動中」とクール
        # ダウンの起点時刻を記録する（仕様7節: 記録タイミングはPopen呼び出し
        # 直前——呼び出し後だと失敗時に記録が残らないケースと二重起動の隙が
        # 生じるため。クールダウンは試みた時刻から成功・失敗を問わず60秒）。
        self._launching.add(resolved)
        self._launch_attempts[resolved] = now
        try:
            try:
                # S603: 実行ファイルはBROWSER_EXECUTABLE_CANDIDATESの許可
                # リストからshutil.which()で解決したパスのみ。shell=False・
                # 引数配列渡しのため、profile_directoryにシェルメタ文字が
                # 含まれていても構造的に解釈されない。
                subprocess.Popen(  # noqa: S603
                    [executable, f'--profile-directory={profile_directory}'],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                logger.warning(
                    'Failed to auto-launch %s (profile %s): %s',
                    _sanitise_for_log(browser),
                    _sanitise_for_log(profile_directory),
                    _sanitise_for_log(exc),
                )
                return {
                    'result': 'error',
                    'message': (
                        f'{not_connected_message} Attempted to launch {browser} but the browser '
                        f'process could not be started.'
                    ),
                }

            logger.info(
                'Auto-launched %s (profile %s) for resolved target %s; waiting up to %.0fs for connection.',
                _sanitise_for_log(browser),
                _sanitise_for_log(profile_directory),
                _sanitise_for_log(resolved),
                AUTO_LAUNCH_WAIT_TIMEOUT,
            )

            client = await self._wait_for_target(resolved, AUTO_LAUNCH_WAIT_TIMEOUT)
            if client is None:
                return {
                    'result': 'error',
                    'message': (
                        f"Launched {browser} (profile '{_sanitise_for_log(profile_directory)}') but no "
                        f'extension connected within {AUTO_LAUNCH_WAIT_TIMEOUT:.0f}s.'
                    ),
                }
            return client.websocket
        finally:
            # 「起動中」の解除: クライアントが接続した時点、またはタイムアウト
            # した時点（仕様7節）。Popen自体が失敗した場合もここで解除される
            # （失敗後も「起動中」のままにすると、以降このtargetへの自動起動が
            # 永久にブロックされてしまうため）。
            self._launching.discard(resolved)

    async def _wait_for_client(self, browser: str | None) -> None:
        """指定されたブラウザ（または任意のブラウザ）が接続するまでブロックする。

        ISSUES.md P0-1（Phase 2a）: `_clients` のキーがbrowser:profileId単位に
        拡張されたため、キーの完全一致ではなくClientInfo.browser属性で判定する。

        Args:
            browser: 待機対象のブラウザ名、またはNoneで任意のクライアントを待つ。
        """
        while True:
            if browser is not None:
                if any(client.browser == browser for client in self._clients.values()):
                    return
            else:
                if self._clients:
                    return
            await asyncio.sleep(0.1)

    async def run_ping_loop(self, interval: float = 20.0) -> None:
        """接続中の全クライアントに定期的なWebSocket pingを送信し、接続を維持する。

        MV3 Service Workerは約30秒間の非活動後にサスペンドされる。
        ``interval``秒ごとにpingを送信することで、ブラウザが応答すべき
        ネットワークアクティビティを生成し、サスペンドを防止する。

        pingが失敗した場合（接続が既に閉じている）、クライアント参照をクリアし、
        次のコマンド試行時に古いソケットを使わず新しい接続待機が発生するようにする。

        このコルーチンはキャンセルされるまで実行される
        （つまり``run_serve_mode``の存続期間中）。

        Args:
            interval: pingフレーム間の秒数。Service Workerのアイドルタイムアウト
                      （約30秒）より短くする必要がある。十分な安全マージンを
                      確保するため20秒を選択している。
        """
        while True:
            await asyncio.sleep(interval)
            # イテレーション中の変更を避けるためスナップショットを取る。
            clients_snapshot = list(self._clients.items())
            for key, client in clients_snapshot:
                try:
                    pong = await client.websocket.ping()
                    await asyncio.wait_for(pong, timeout=5.0)
                except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
                    # スリープとping送信の間にクライアントが切断されたか、
                    # pongが5秒以内に到着しなかった。
                    # handle_connectionのfinallyブロックが_clientsをクリーンアップする。
                    # ここでは何もする必要がない。
                    logger.debug(
                        'Ping failed for key=%s; awaiting cleanup by handle_connection.',
                        _sanitise_for_log(key),
                    )


# ---------------------------------------------------------------------------
# HTTPコマンドハンドラー（サーブモード）
# ---------------------------------------------------------------------------

async def _handle_http_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    kontrol: ChromeKontrolServer,
    auth_token: str,
) -> None:
    """サーブモードで単一のHTTPリクエストを処理する。

    HTTP/1.0スタイルのリクエスト全体を読み取り、ボディを抽出し、
    ChromeKontrolコマンドとしてバリデーションし、WebSocket経由で拡張機能に転送し、
    JSONレスポンスをHTTP/1.1 200レスポンスとして書き戻す。

    設計判断: 追加の依存関係を避けるため、aiohttpやhttp.serverを使わず
    最小限のHTTP/1.1パーサーを実装している。Content-Lengthヘッダー付きの
    POSTリクエストのみサポートする。これはcurlベースの自動化に十分であり、
    攻撃対象面を小さく保つ。

    セキュリティに関する注意:
      - ボディサイズはMAX_MESSAGE_BYTESで制限し、メモリ枯渇を防止する。
      - asyncio.StreamWriterはfinallyブロックで必ず閉じ、fdリークを防ぐ。
      - _validate_commandはWebSocket呼び出し元と同じホワイトリストを再利用する。
      - CSRF対策: X-ChromeKontrol-Tokenヘッダーをsecrets.compare_digestで検証する。
        タイミング攻撃を防ぐため文字列の等値比較（==）は使用しない。
      - CSRF対策: Content-Type: application/jsonを必須とし、CORS preflightを強制する。
      - トークン値はログおよびエラーレスポンスに含めない（ヘッダー名のみ言及可）。
      - 401応答は欠落・不一致を区別しない（列挙攻撃対策）。

    Args:
        reader: 受信接続用の非同期バイトリーダー。
        writer: レスポンス用の非同期バイトライター。
        kontrol: 共有のChromeKontrolServerインスタンス（WSクライアント参照を保持）。
        auth_token: 起動時に生成または環境変数から取得したCSRF対策トークン。
    """
    peer_info = writer.get_extra_info('peername')
    peer = f'{peer_info[0]}:{peer_info[1]}' if peer_info else '?'
    logger.debug('HTTP request from %s', _sanitise_for_log(peer))

    try:
        # ヘッダーを読み取る（二重CRLFで終端）。
        # ヘッダーベースのDoSを緩和するため、ヘッダー読み取りを8 KiBに制限する。
        # 個別の5秒チャンクタイムアウト（1バイトずつ送る低速クライアントにより
        # 無限に延長される可能性がある）ではなく、ヘッダー読み取り全体に
        # 単一の10秒デッドラインを適用する。
        header_buf = b''
        MAX_HEADER_BYTES = 8 * 1024
        header_deadline = asyncio.get_running_loop().time() + 10.0
        while b'\r\n\r\n' not in header_buf:
            remaining_time = header_deadline - asyncio.get_running_loop().time()
            if remaining_time <= 0:
                raise asyncio.TimeoutError
            chunk = await asyncio.wait_for(reader.read(1024), timeout=remaining_time)
            if not chunk:
                break
            header_buf += chunk
            if len(header_buf) > MAX_HEADER_BYTES:
                await _write_http_error(writer, 431, 'Request Header Fields Too Large')
                return

        header_section, _, body_start = header_buf.partition(b'\r\n\r\n')
        header_text = header_section.decode('latin-1', errors='replace')
        # RFC 7230に従いCRLF分割を優先する。非準拠クライアント用にLFにフォールバック。
        lines = header_text.split('\r\n') if '\r\n' in header_text else header_text.split('\n')
        if not lines:
            await _write_http_error(writer, 400, 'Bad Request')
            return

        # HTTPメソッドをバリデーションする（POSTのみ受け付ける）。
        request_line = lines[0]
        parts = request_line.split(' ', 2)
        if len(parts) < 2 or parts[0].upper() != 'POST':
            await _write_http_error(writer, 405, 'Method Not Allowed')
            return

        # リクエストヘッダーを小文字名でdictに収集する（重複は後勝ち）。
        # Content-Length・Content-Type・認証トークンをここで一括取得する。
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, sep, value = line.partition(':')
            if sep:
                headers[name.strip().lower()] = value.strip()

        # CSRF対策: X-ChromeKontrol-Tokenヘッダーを検証する。
        # secrets.compare_digestでタイミング攻撃を防ぐ。
        # 欠落・不一致ともに同一メッセージで返し、列挙攻撃を防止する。
        # セキュリティ注記: トークン値はログに記録しない。
        request_token = headers.get(HTTP_AUTH_HEADER_NAME.lower(), '')
        if not secrets.compare_digest(request_token, auth_token):
            logger.warning(
                'HTTP request rejected: missing or invalid %s header from %s',
                HTTP_AUTH_HEADER_NAME,
                _sanitise_for_log(peer),
            )
            await _write_http_error(writer, 401, 'Unauthorized')
            return

        # CSRF対策: Content-Type検証。application/json以外を拒否し、
        # CORS preflightを強制することでsimple requestによる攻撃を防止する。
        content_type_raw = headers.get('content-type', '')
        # media-typeのみ比較（; charset=utf-8 等のパラメータを除外）。
        content_type_media = content_type_raw.split(';')[0].strip().lower()
        if content_type_media != REQUIRED_CONTENT_TYPE:
            logger.warning(
                'HTTP request rejected: Content-Type must be %r, got %r from %s',
                REQUIRED_CONTENT_TYPE,
                _sanitise_for_log(content_type_raw),
                _sanitise_for_log(peer),
            )
            await _write_http_error(writer, 415, 'Unsupported Media Type')
            return

        # Content-Lengthを取得・バリデーションする。
        content_length: int | None = None
        raw_cl = headers.get('content-length')
        if raw_cl is not None:
            try:
                content_length = int(raw_cl)
                if content_length < 0:
                    await _write_http_error(writer, 400, 'Bad Request: negative Content-Length')
                    return
            except ValueError:
                await _write_http_error(writer, 400, 'Bad Request: invalid Content-Length')
                return

        if content_length is None:
            await _write_http_error(writer, 411, 'Length Required')
            return

        if content_length > MAX_MESSAGE_BYTES:
            await _write_http_error(writer, 413, 'Request Entity Too Large')
            return

        # 残りのボディバイトを読み取る（header_bufに既にボディの一部が含まれている場合がある）。
        body = body_start
        remaining = content_length - len(body)
        if remaining > 0:
            try:
                extra = await asyncio.wait_for(reader.readexactly(remaining), timeout=10.0)
                body += extra
            except asyncio.IncompleteReadError:
                await _write_http_error(writer, 400, 'Bad Request: incomplete body')
                return

        # コマンドをパースしバリデーションする。
        try:
            raw_cmd: Any = json.loads(body.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            await _write_http_error(writer, 400, 'Bad Request: invalid JSON body')
            return

        is_valid, error_msg = _validate_command(raw_cmd)
        if not is_valid:
            response_body = json.dumps({'result': 'error', 'message': error_msg}).encode('utf-8')
            await _write_http_response(writer, 400, response_body)
            return

        # 転送前にオプションのブラウザ指定/target指定フィールドを抽出する。
        # _validate_commandで既にstrまたは不在であることが確認済み
        # （両方同時に非Noneになることも_validate_commandが拒否済み）。
        target_browser: str | None = raw_cmd.get('browser') if isinstance(raw_cmd, dict) else None
        target: str | None = raw_cmd.get('target') if isinstance(raw_cmd, dict) else None

        # 拡張機能に転送しレスポンスを返す。
        response = await kontrol.send_command(raw_cmd, browser=target_browser, target=target)
        response_body = json.dumps(response).encode('utf-8')
        await _write_http_response(writer, 200, response_body)

    except asyncio.TimeoutError:
        # ヘッダーまたはボディの読み取りがタイムアウトした。クライアントが停滞している可能性がある。
        logger.warning('HTTP request timed out from %s', _sanitise_for_log(peer))
        try:
            await _write_http_error(writer, 408, 'Request Timeout')
        except OSError:
            pass
    except OSError as exc:
        logger.warning('HTTP connection error from %s: %s', _sanitise_for_log(peer), _sanitise_for_log(exc))
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            # ソケットが既に閉じている可能性がある。実際のエラーを隠さないよう抑制する。
            pass


async def _write_http_response(writer: asyncio.StreamWriter, status: int, body: bytes) -> None:
    """最小限のHTTP/1.1レスポンスを書き込む。

    Args:
        writer: 接続用の非同期バイトライター。
        status: HTTPステータスコード。
        body: UTF-8エンコードされたJSONボディバイト。
    """
    reason = {200: 'OK', 400: 'Bad Request', 401: 'Unauthorized',
              405: 'Method Not Allowed', 408: 'Request Timeout',
              411: 'Length Required', 413: 'Request Entity Too Large',
              415: 'Unsupported Media Type',
              431: 'Request Header Fields Too Large'}.get(status, 'Unknown')
    response = (
        f'HTTP/1.1 {status} {reason}\r\n'
        f'Content-Type: application/json\r\n'
        f'Content-Length: {len(body)}\r\n'
        f'Connection: close\r\n'
        f'Cache-Control: no-store\r\n'
        f'X-Content-Type-Options: nosniff\r\n'
        f'\r\n'
    ).encode('latin-1') + body
    writer.write(response)
    try:
        await asyncio.wait_for(writer.drain(), timeout=5.0)
    except asyncio.TimeoutError:
        logger.warning('HTTP response write timed out')


async def _write_http_error(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    """JSONエラーレスポンスを書き込む。

    Args:
        writer: 接続用の非同期バイトライター。
        status: HTTPステータスコード。
        message: 人間可読なエラー説明（拡張機能には公開されない）。
    """
    body = json.dumps({'result': 'error', 'message': message}).encode('utf-8')
    await _write_http_response(writer, status, body)


# ---------------------------------------------------------------------------
# トークンファイル永続化（ISSUES.md P0-2）
# ---------------------------------------------------------------------------

# トークンファイルのデフォルト配置場所。CHROME_KONTROL_TOKEN_FILE環境変数で
# 上書き可能（主にテスト用途。tmp_pathを使う際に実ホームディレクトリを
# 汚さないため）。
DEFAULT_TOKEN_FILE: Path = Path.home() / '.config' / 'chromekontrol' / 'token'

# 設計判断: これは環境変数の「名前」を保持する定数であり、トークン値そのものではない。
# ruffのS105（ハードコードされたパスワード検知）は変数名に'TOKEN'を含むだけで
# 誤検知するため、この行に限り抑制する。
TOKEN_FILE_ENV_VAR: str = 'CHROME_KONTROL_TOKEN_FILE'  # noqa: S105


def _resolve_token_file_path() -> Path:
    """トークンファイルの書き込み先パスを解決する。

    Returns:
        CHROME_KONTROL_TOKEN_FILE環境変数が空でない値で設定されていればそのパス、
        なければDEFAULT_TOKEN_FILE。
    """
    override = os.environ.get(TOKEN_FILE_ENV_VAR, '').strip()
    return Path(override) if override else DEFAULT_TOKEN_FILE


def _determine_auth_token() -> tuple[str, bool]:
    """HTTP API認証用のCSRF対策トークンを決定する。

    設計判断: このロジック自体（環境変数優先、未設定なら暗号論的乱数生成）は
    Phase 0以前から変更していない。run_serve_modeから関数として切り出したのは、
    単体テストで環境変数分岐を実ソケットバインドなしに検証できるようにするため。

    環境変数 CHROME_KONTROL_TOKEN が設定されていればそれを採用し、常駐起動時の
    トークン固定を可能にする（手動起動時に固定トークンを使う運用を残すため）。
    未設定（または空白のみ）の場合は secrets.token_urlsafe(32) で新規生成する。

    Returns:
        (auth_token, env_token_used) のタプル。env_token_usedは環境変数由来か
        どうかを示す（呼び出し元のログ分岐・強度警告に使用）。
    """
    env_var_name = 'CHROME_KONTROL_TOKEN'
    env_token = os.environ.get(env_var_name, '').strip()
    env_token_used = bool(env_token)
    if env_token_used and len(env_token) < 32:
        logger.warning(
            '環境変数 %s のトークンが短すぎます（32文字未満）。ブルートフォース攻撃に弱くなります。',
            env_var_name,
        )
    auth_token = env_token if env_token_used else secrets.token_urlsafe(32)
    return auth_token, env_token_used


def _persist_token_to_file(token: str, token_file: Path) -> None:
    """トークンをファイルへ原子的に書き込む。権限0600をumaskに関わらず保証する。

    mcp_bridge.mjs（401リトライ時の読み直しを含む）や手動のcurl呼び出しが、
    このファイルからトークンを読み取れるようにする。書き込みは同一ディレクトリ内の
    一時ファイル + os.replace()による原子的リネームで行い、読み取り側が
    書き込み途中の不完全な内容を見ることを防ぐ。

    セキュリティ上の注意:
      - 親ディレクトリ・ファイルともにos.chmod()でモードを明示的に再設定する。
        Path.mkdir()のmode引数やtempfile.mkstemp()の内部os.open()はumaskの
        影響を受けるため、chmod()の併用でumask設定に関わらず意図した権限
        （ディレクトリ0700 / ファイル0600）を確実にする。
      - 一時ファイルは書き込み先と同じディレクトリに作成する
        （別ファイルシステム間のrenameは原子的でなくなるため。また
        書き込み先と同じ権限を確実に付与するため）。
      - トークン値そのものは（成功時・失敗時ともに）ログへ一切出力しない。

    Args:
        token: 書き込むトークン文字列。
        token_file: 書き込み先パス（通常は_resolve_token_file_path()の戻り値）。

    Note:
        書き込みに失敗した場合、例外は送出せず警告ログのみ出力する。
        環境変数CHROME_KONTROL_TOKEN経由でのトークン共有が引き続き機能するため、
        サーバーの起動処理はこの関数の失敗に関わらず継続する必要がある。
        トークンファイルの削除処理は意図的に実装しない。異常終了時に削除されず
        不整合が残る、次回起動で必ず上書きされる、サーバー停止中は接続不能で
        トークンが残っていても実害がない、という理由による。
    """
    tmp_path: Path | None = None
    try:
        parent = token_file.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # mkdirのmodeはumaskでマスクされるため、chmodで確実に0700にする。
        os.chmod(parent, 0o700)

        fd, tmp_name = tempfile.mkstemp(dir=parent, prefix=f'.{token_file.name}.', suffix='.tmp')
        tmp_path = Path(tmp_name)
        try:
            # mkstemp()のos.open()もumaskの影響を受けうるため明示的に再設定する。
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(token)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        os.replace(tmp_path, token_file)
        tmp_path = None  # リネーム成功。以降のクリーンアップ対象から外す。
        os.chmod(token_file, 0o600)
    except OSError as exc:
        logger.warning(
            'Failed to write token file at %s: %s. '
            'MCP bridge / curl callers must use $CHROME_KONTROL_TOKEN instead.',
            _sanitise_for_log(token_file),
            _sanitise_for_log(exc),
        )
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# サーブモードのエントリーポイント
# ---------------------------------------------------------------------------

def _ignore_sigchld_for_auto_launched_children() -> None:
    """自動起動でsubprocess.Popen()により生成した子プロセス（ブラウザ）が
    ゾンビプロセスとして残ることを防ぐ（Security-Audit.md M-6, Phase 2c）。

    背景: _auto_launch_response()はブラウザプロセスをsubprocess.Popen()で
    起動するが、戻り値のPopenオブジェクトを保持しない（意図的な設計。
    ブラウザの寿命管理はChromeKontrolの責務ではなく、ユーザーが手動で
    終了する・既存インスタンスへ通知して自身はすぐexitする等、ライフ
    サイクルはブラウザ側に委ねている）。Popenオブジェクトを保持して
    poll()/wait()するreap処理を導入すると、「いつ・どのタイミングで
    waitするか」という余分な責務・状態管理が発生するため採らない。

    対策: サーバー起動時に一度だけSIGCHLDをSIG_IGNへ設定する。Linuxでは
    SIGCHLDのハンドラを明示的にSIG_IGNにすると、子プロセス終了時に
    カーネルが自動的にreapしゾンビ化しない（POSIX.1-2001で規定された
    挙動）。これによりPopen呼び出し側は一切wait()を呼ぶ必要がなくなる。

    asyncioとの相互作用について: 本プロジェクトはserver.py全体を通じて
    asyncio.create_subprocess_exec() / create_subprocess_shell()を
    使用しない（起動する子プロセスはブラウザのみで、常にsubprocess.
    Popen()経由。2026-07-25確認）。asyncioのchild watcher機構は
    create_subprocess_*系のAPIが最初に呼ばれた時点で遅延初期化される
    ため、これらのAPIを一切使わない本プロジェクトではSIGCHLDをSIG_IGN
    にしてもasyncioのサブプロセス機能と衝突しない。将来
    asyncio.create_subprocess_*の導入を検討する場合は、この関数の
    呼び出しごと設計を見直すこと（child watcherがSIGCHLDに依存する
    実装だとasyncio側の子プロセス終了検知が機能しなくなる）。

    Linux専用: signal.SIGCHLDはPOSIX固有の定数でWindowsには存在しない。
    本プロジェクトはLinux（systemd常駐運用）前提だが、hasattr()で
    ガードしSIGCHLD非対応環境（Windows等）では何もしない
    （AttributeErrorを防ぐためだけの措置であり、Windows対応を新規に
    行うものではない）。
    """
    if not hasattr(signal, 'SIGCHLD'):
        return
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)


async def run_serve_mode(ws_port: int, http_port: int) -> None:
    """WebSocketサーバーとHTTPサーバーを起動し、中断されるまで実行する。

    この関数の動作:
      1. 自動起動された子プロセス（ブラウザ）がゾンビ化しないよう
         SIGCHLDをSIG_IGNに設定する（Security-Audit.md M-6, Phase 2c）。
      2. Chrome拡張機能を受け入れるためにWebSocketサーバー（ポートws_port）をバインドする。
      3. curl/スクリプトコマンドを受け入れるためにHTTPサーバー（ポートhttp_port）をバインドする。
      4. CancelledErrorまたはKeyboardInterruptまで両サーバーを無期限に実行する。

    全てのネットワークリスナーはBIND_HOST (127.0.0.1) にのみバインドする。

    Args:
        ws_port: WebSocketサーバー用のTCPポート（デフォルト9765）。
        http_port: HTTPコマンドAPI用のTCPポート（デフォルト9766）。
    """
    # Security-Audit.md M-6, Phase 2c: 自動起動されたブラウザ子プロセスの
    # ゾンビ化を防ぐ。--serveモード起動時に一度だけ設定すればよい
    # （ワンショットモードのrun_server()は自動起動を行わないため不要）。
    _ignore_sigchld_for_auto_launched_children()

    # ISSUES.md P0-1（Phase 2b/2c）: 設定ファイルからエイリアス・自動起動設定を
    # 起動時に1回だけ読み込む。変更の反映にはサーバー再起動が必要（README参照）。
    config_file = _resolve_config_file_path()
    aliases = _load_aliases(config_file)
    auto_launch = _load_auto_launch(config_file)
    profiles = _load_profiles(config_file)
    kontrol = ChromeKontrolServer(aliases=aliases, auto_launch=auto_launch, profiles=profiles)

    # CSRF対策トークンを決定する（環境変数優先、なければ新規生成。詳細は
    # _determine_auth_token()のdocstring参照）。
    auth_token, env_token_used = _determine_auth_token()

    # ISSUES.md P0-2: トークンをファイルへ永続化する。
    # mcp_bridge.mjsおよび手動のcurl呼び出しはこのファイルからトークンを読み取る。
    # サーバー再起動でトークンがローテーションされても、mcp_bridge.mjs側は
    # 401受信時にファイルを読み直すだけで自動追従できる。
    # 書き込みに失敗しても_persist_token_to_file内で警告ログのみ出し、
    # ここでは例外を送出しない（環境変数経由の運用が引き続き機能するため）。
    token_file_path = _resolve_token_file_path()
    _persist_token_to_file(auth_token, token_file_path)

    ws_server = await websockets.server.serve(
        kontrol.handle_connection,
        host=BIND_HOST,
        port=ws_port,
        max_size=MAX_MESSAGE_BYTES,
        compression=None,
    )
    logger.info('ChromeKontrol WebSocket listening on %s:%d', BIND_HOST, ws_port)

    # クロージャがkontrolとauth_tokenをキャプチャし、各接続が同じ状態を共有するようにする。
    async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_http_request(reader, writer, kontrol, auth_token)

    http_server = await asyncio.start_server(
        _http_handler,
        host=BIND_HOST,
        port=http_port,
    )
    logger.info('ChromeKontrol HTTP API listening on %s:%d', BIND_HOST, http_port)
    # M-8 / ISSUES.md P0-2: トークン値はstderr/journaldに一切出力しない。
    # 案内するのはファイルパスのみ。呼び出し元（curl等）は
    # `$(cat <path>)` でファイルから直接読み取れる。
    # 環境変数使用時・ランダム生成時のいずれも、生トークンは出力しない
    # （systemd常駐運用ではjournaldに永続化されてしまうため）。
    # Phase F2レビューM-1: ブラウザ追加時にここを個別修正しなくて済むよう、
    # ALLOWED_BROWSERSから動的に組み立てる（sorted()で出力順序を安定させる）。
    browser_hint = _format_browser_list(f'"browser":"{name}"' for name in sorted(ALLOWED_BROWSERS))
    logger.info(
        'Ready. Token available at %s (mode 0600)%s. Send commands: '
        'curl -s %s:%d '
        '-H "%s: $(cat %s)" '
        '-H "Content-Type: application/json" '
        '-d \'{"cmd":"get_dom"}\' '
        '(multi-browser: add %s)',
        token_file_path,
        ' (mirrors $CHROME_KONTROL_TOKEN)' if env_token_used else '',
        BIND_HOST,
        http_port,
        HTTP_AUTH_HEADER_NAME,
        token_file_path,
        browser_hint,
    )

    ping_task = asyncio.create_task(kontrol.run_ping_loop())

    try:
        async with http_server:
            await http_server.start_serving()
            # KeyboardInterruptによりタスクがキャンセルされるまで無期限にブロックする。
            # Python 3.10+で現在のループがない場合に発生するDeprecationWarningを
            # 避けるため、get_event_loop()ではなくasyncio.get_running_loop()を使用する。
            await asyncio.get_running_loop().create_future()
    except asyncio.CancelledError:
        pass
    finally:
        ping_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass
        ws_server.close()
        await ws_server.wait_closed()
        logger.info('ChromeKontrol server stopped.')


# ---------------------------------------------------------------------------
# stdinコマンドリーダー（CLIインターフェース）
# ---------------------------------------------------------------------------

async def read_stdin_command() -> Any:
    """stdinからJSON形式のコマンド行を1行読み取る（executorによるノンブロッキング）。

    Returns:
        パース済みJSON値（Any）、またはstdinが閉じている/空の場合はNone。
        構造バリデーションは呼び出し元で_validate_commandにより実行される。
    """
    loop = asyncio.get_running_loop()
    try:
        line: str = await loop.run_in_executor(None, sys.stdin.readline)
    except OSError as exc:
        logger.error('Failed to read from stdin: %s', _sanitise_for_log(exc))
        return None

    line = line.strip()
    if not line:
        return None

    try:
        return json.loads(line)
    except json.JSONDecodeError:
        logger.error('Invalid JSON from stdin: (content hidden for security)')
        return None


# ---------------------------------------------------------------------------
# メインエントリーポイント
# ---------------------------------------------------------------------------

async def run_server(port: int) -> None:
    """WebSocketサーバーを起動し、stdinから1つのコマンドを処理する。

    この関数の動作:
      1. サーバーをlocalhostのみにバインドする。
      2. stdinからJSONコマンドを1つ読み取る。
      3. バリデーションし、接続中のChrome拡張機能に転送する。
      4. JSONレスポンスをstdoutに書き込む。
      5. 終了する（ワンショットモード）。

    Args:
        port: リッスンするTCPポート。
    """
    # ISSUES.md P0-1（Phase 2b/2c）: ワンショットモードも「起動」に該当するため、
    # サーブモードと同様に起動時1回のみエイリアス・自動起動設定を読み込む。
    config_file = _resolve_config_file_path()
    aliases = _load_aliases(config_file)
    auto_launch = _load_auto_launch(config_file)
    profiles = _load_profiles(config_file)
    kontrol = ChromeKontrolServer(aliases=aliases, auto_launch=auto_launch, profiles=profiles)

    server = await websockets.server.serve(
        kontrol.handle_connection,
        host=BIND_HOST,
        port=port,
        # 過大フレームによるDoSを緩和するため最大メッセージサイズを制限する。
        max_size=MAX_MESSAGE_BYTES,
        # CPU使用量を予測可能に保つため圧縮を無効にする。
        compression=None,
    )

    logger.info('ChromeKontrol server listening on %s:%d', BIND_HOST, port)

    raw_cmd = await read_stdin_command()
    if raw_cmd is None:
        logger.error('No command received from stdin.')
        server.close()
        await server.wait_closed()
        sys.exit(1)

    is_valid, error_msg = _validate_command(raw_cmd)
    if not is_valid:
        response = {'result': 'error', 'message': error_msg}
        print(json.dumps(response), flush=True)
        server.close()
        await server.wait_closed()
        sys.exit(1)

    # stdinコマンドからオプションのブラウザ指定/target指定フィールドを抽出する。
    # _validate_commandで既にstrまたは不在であることが確認済み
    # （両方同時に非Noneになることも_validate_commandが拒否済み）。
    target_browser: str | None = raw_cmd.get('browser') if isinstance(raw_cmd, dict) else None
    target: str | None = raw_cmd.get('target') if isinstance(raw_cmd, dict) else None

    response = await kontrol.send_command(raw_cmd, browser=target_browser, target=target)
    print(json.dumps(response), flush=True)

    server.close()
    await server.wait_closed()
    logger.info('Server shut down.')


def _resolve_port(env_var: str, cli_flag: str, default: int, args: list[str]) -> int:
    """環境変数とCLI引数からTCPポートを解決する。

    優先順序: CLI引数 > 環境変数 > デフォルト値。

    Args:
        env_var: チェックする環境変数の名前。
        cli_flag: 探すCLIフラグ名（例: '--port'）。
        default: フォールバック用のデフォルトポート番号。
        args: パース済みsys.argv[1:]のリスト。

    Returns:
        解決されたポート番号（範囲: [1, 65535]）。
    """
    port = default

    env_val = os.environ.get(env_var, '')
    if env_val:
        try:
            parsed = int(env_val)
            if 1 <= parsed <= 65535:
                port = parsed
            else:
                logger.warning('%s value out of range (1-65535); using default %d.', env_var, default)
        except ValueError:
            logger.warning('%s is not a valid integer; using default %d.', env_var, default)

    if cli_flag in args:
        idx = args.index(cli_flag)
        if idx + 1 < len(args):
            try:
                parsed = int(args[idx + 1])
                if 1 <= parsed <= 65535:
                    port = parsed
                else:
                    logger.warning('%s value out of range; using %d.', cli_flag, port)
            except ValueError:
                logger.warning('%s value is not an integer; using %d.', cli_flag, port)

    return port


def main() -> None:
    """CLIエントリーポイント。

    使用方法（ワンショットモード）:
        echo '{"cmd":"get_dom"}' | python3 server.py [--port PORT]
        echo '{"cmd":"get_dom","browser":"edge"}' | python3 server.py [--port PORT]

    使用方法（サーブモード）:
        python3 server.py --serve [--port PORT] [--http-port PORT]

        # トークンは起動ごとに ~/.config/chromekontrol/token（権限0600）へ書き出される。
        # stderrにトークン値は一切表示されない（案内されるのはファイルパスのみ）。
        python3 server.py --serve
        curl -s localhost:9766 -H "X-ChromeKontrol-Token: $(cat ~/.config/chromekontrol/token)" \
          -H "Content-Type: application/json" -d '{"cmd":"get_dom","browser":"chrome"}'

        # 固定トークンで運用する場合（任意。ファイルにも同じ値が書き出される）:
        export CHROME_KONTROL_TOKEN=your_fixed_token_here
        python3 server.py --serve

    環境変数:
        CHROME_KONTROL_PORT       デフォルトのWebSocketポート（9765）を上書きする。
        CHROME_KONTROL_HTTP_PORT  デフォルトのHTTP APIポート（9766）を上書きする。
        CHROME_KONTROL_TOKEN      HTTP API認証トークンを固定する（省略時は起動ごとにランダム生成）。
        CHROME_KONTROL_TOKEN_FILE トークンファイルの書き込み先を上書きする
                                  （省略時は ~/.config/chromekontrol/token。主にテスト用途）。
        CHROME_KONTROL_CONFIG_FILE エイリアス設定ファイルの読み込み先を上書きする
                                  （省略時は ~/.config/chromekontrol/config.json。主にテスト用途）。
                                  設定ファイルは起動時に1回だけ読み込まれる。変更の反映には
                                  サーバー再起動が必要。
    """
    _configure_logging()

    args = sys.argv[1:]
    serve_mode = '--serve' in args

    ws_port = _resolve_port('CHROME_KONTROL_PORT', '--port', DEFAULT_PORT, args)
    http_port = _resolve_port('CHROME_KONTROL_HTTP_PORT', '--http-port', DEFAULT_HTTP_PORT, args)

    if serve_mode:
        try:
            asyncio.run(run_serve_mode(ws_port, http_port))
        except KeyboardInterrupt:
            logger.info('Interrupted by user; shutting down.')
            sys.exit(0)
    else:
        try:
            asyncio.run(run_server(ws_port))
        except KeyboardInterrupt:
            logger.info('Interrupted by user.')
            sys.exit(0)


if __name__ == '__main__':
    main()
