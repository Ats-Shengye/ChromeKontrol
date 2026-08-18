/**
 * Phase F3b テスト: extensions/chromium/ と extensions/firefox/ のコード同一性を固定する。
 *
 * 背景（フェーズ仕様「最重要の方針」）: FirefoxKontrol が別リポジトリで管理されていた期間、
 * Phase 0〜3a の変更が一切反映されず2026-06-19時点の実装のまま取り残された。
 * Phase F3a で background.js / content.js の名前空間を `api` に統一したことで、
 * Chrome / Edge / Firefox は同一のコードで動作するようになった。このテストは
 * 「同一であるべきファイルが実際に同一である」ことを継続的に検証し、片方だけへの
 * 変更（同期漏れ）が発生した時点でテスト失敗として検出する。
 *
 * 対象範囲（フェーズ仕様 3）:
 *   検証項目1: background.js / content.js のバイト単位の同一性
 *   検証項目2: manifest.json を除くファイル構成（ファイル名集合）の一致
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CHROMIUM_DIR = path.join(__dirname, '..', 'chromium');
const FIREFOX_DIR = path.join(__dirname, '..', 'firefox');

/**
 * chromium/ と firefox/ の両方に存在し、バイト単位で同一であることを要求するファイル。
 * manifest.json は意図的にブラウザごとの差分を持つため、この一覧には含めない。
 */
const SHARED_FILES = ['background.js', 'content.js'];

/**
 * 2つの Buffer を先頭から比較し、最初に値が異なるバイトのインデックスを返す。
 * Buffer.equals() 自体は差分位置を教えてくれないため、失敗メッセージに具体的な
 * 手がかりを含めるための自前実装。
 *
 * 短い方の長さまで一致していれば（つまり長さのみが異なる場合）、短い方の長さを
 * 差分位置として返す（フェーズ仕様3の指定挙動）。
 *
 * @param {Buffer} bufA
 * @param {Buffer} bufB
 * @returns {number} 最初に異なるバイトのインデックス、または短い方の長さ。
 */
function findFirstDifferenceIndex(bufA, bufB) {
  const minLength = Math.min(bufA.length, bufB.length);
  for (let i = 0; i < minLength; i++) {
    if (bufA[i] !== bufB[i]) {
      return i;
    }
  }
  return minLength;
}

/**
 * ディレクトリ直下（非再帰）にあるファイル（ディレクトリを除く）の名前一覧を返す。
 * @param {string} dir
 * @returns {string[]}
 */
function listTopLevelFileNames(dir) {
  return readdirSync(dir, { withFileTypes: true })
    .filter((entry) => entry.isFile())
    .map((entry) => entry.name);
}

for (const filename of SHARED_FILES) {
  test(`parity: ${filename} は chromium と firefox でバイト単位で同一`, () => {
    const chromiumPath = path.join(CHROMIUM_DIR, filename);
    const firefoxPath = path.join(FIREFOX_DIR, filename);

    // 文字列比較ではなく Buffer.equals() を使う。改行コード（LF/CRLF）や
    // BOM の有無等、文字列化の過程で吸収されてしまう差異も検出するため。
    const chromiumBuf = readFileSync(chromiumPath);
    const firefoxBuf = readFileSync(firefoxPath);

    if (chromiumBuf.equals(firefoxBuf)) {
      return;
    }

    const diffIndex = findFirstDifferenceIndex(chromiumBuf, firefoxBuf);
    assert.fail(
      `${filename} differs between extensions/chromium/ and extensions/firefox/.\n` +
        `  chromium: ${chromiumPath} (${chromiumBuf.length} bytes)\n` +
        `  firefox:  ${firefoxPath} (${firefoxBuf.length} bytes)\n` +
        `  first differing byte offset: ${diffIndex}\n` +
        '  This file must be byte-identical in both directories (Phase F3b policy). ' +
        'Determine which side has the intended change, then `cp` it over the other — ' +
        'this test cannot determine which side is stale on its own.'
    );
  });
}

test('parity: ファイル構成が一致する（manifest.json を除く）', () => {
  const chromiumFiles = new Set(listTopLevelFileNames(CHROMIUM_DIR));
  const firefoxFiles = new Set(listTopLevelFileNames(FIREFOX_DIR));

  // manifest.json は permissions / background / content_security_policy /
  // browser_specific_settings 等がブラウザごとに構造的に異なることが仕様上
  // 正しいため、意図的にこの比較の対象から除外する。この除外の結果、
  // 「manifest.json だけが両ディレクトリ間で異なる」状態は本テストにおいて
  // 正常として扱われる（差分があっても検出しない）。
  chromiumFiles.delete('manifest.json');
  firefoxFiles.delete('manifest.json');

  const onlyInChromium = [...chromiumFiles].filter((name) => !firefoxFiles.has(name)).sort();
  const onlyInFirefox = [...firefoxFiles].filter((name) => !chromiumFiles.has(name)).sort();

  if (onlyInChromium.length === 0 && onlyInFirefox.length === 0) {
    return;
  }

  assert.fail(
    'File composition differs between extensions/chromium/ and extensions/firefox/ (manifest.json excluded).\n' +
      `  only in chromium: ${onlyInChromium.length > 0 ? onlyInChromium.join(', ') : '(none)'}\n` +
      `  only in firefox:  ${onlyInFirefox.length > 0 ? onlyInFirefox.join(', ') : '(none)'}\n` +
      '  A file was likely added to one browser directory without adding the equivalent ' +
      'to the other (e.g. options.html).'
  );
});
