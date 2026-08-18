/**
 * Phase F3a テスト: detectBrowser() の3ブラウザ対応の固定。
 *
 * 判定は2段階（フェーズ仕様3）:
 *   1. navigator.userAgentData.brands（Chromium専用）
 *   2. 該当しなければ navigator.userAgent に "Firefox/" が含まれるか
 *
 * 対象範囲（仕様書 phase-f3a-spec.md 4-5）:
 *   - userAgentData.brands に Microsoft Edge → 'edge'
 *   - userAgentData.brands に Google Chrome → 'chrome'
 *   - userAgentData なし + userAgent に Firefox/ → 'firefox'
 *   - どちらでもない → 'unknown'
 *   - navigator へのアクセスが例外を投げる場合 → 'unknown'
 */

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadBackground } from './helpers/load-background.mjs';

test("detectBrowser: userAgentData.brandsにMicrosoft Edgeが含まれれば'edge'", () => {
  const { context } = loadBackground({
    navigatorOverrides: {
      userAgentData: { brands: [{ brand: 'Chromium' }, { brand: 'Microsoft Edge' }, { brand: 'Not=A?Brand' }] },
    },
  });
  assert.strictEqual(context.detectBrowser(), 'edge');
});

test("detectBrowser: userAgentData.brandsにGoogle Chromeが含まれれば'chrome'", () => {
  const { context } = loadBackground({
    navigatorOverrides: {
      userAgentData: { brands: [{ brand: 'Chromium' }, { brand: 'Google Chrome' }, { brand: 'Not=A?Brand' }] },
    },
  });
  assert.strictEqual(context.detectBrowser(), 'chrome');
});

test("detectBrowser: userAgentDataがなくuserAgentにFirefox/が含まれれば'firefox'", () => {
  const { context } = loadBackground({
    navigatorOverrides: {
      userAgentData: undefined,
      userAgent: 'Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0',
    },
  });
  assert.strictEqual(context.detectBrowser(), 'firefox');
});

test("detectBrowser: どちらにも該当しなければ'unknown'", () => {
  const { context } = loadBackground({
    navigatorOverrides: {
      userAgentData: undefined,
      userAgent: 'SomeOtherBrowser/1.0',
    },
  });
  assert.strictEqual(context.detectBrowser(), 'unknown');
});

test("detectBrowser: userAgentDataはあるがbrandsがどちらとも一致しない場合、Firefox/を含まなければ'unknown'", () => {
  const { context } = loadBackground({
    navigatorOverrides: {
      userAgentData: { brands: [{ brand: 'Opera' }] },
      userAgent: 'Mozilla/5.0 Opera/100.0',
    },
  });
  assert.strictEqual(context.detectBrowser(), 'unknown');
});

test("detectBrowser: navigatorへのアクセスが例外を投げる場合は'unknown'（防御的try/catch）", () => {
  const { context } = loadBackground({ navigatorThrows: true });
  assert.strictEqual(context.detectBrowser(), 'unknown');
});
