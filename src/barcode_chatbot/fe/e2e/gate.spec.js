// Smoke test against the REAL running stack (nginx gate, CSP, polyfill + ZXing WASM from the CDN):
//   GATE_URL=http://127.0.0.1:8080 npx playwright test e2e/gate.spec.js     (npm run test:gate)
// Skipped without GATE_URL. Guards the class of break where a CSP/cache change stops barcode decoding on every
// browser without a native BarcodeDetector (Chrome on Windows, Firefox, Safari). Needs network access to jsdelivr.
'use strict';
const path = require('path');
const { test, expect } = require('@playwright/test');

const GATE = process.env.GATE_URL;
test.skip(!GATE, 'set GATE_URL to run against the real stack');

test('gate sends the headers the scanner needs', async ({ request }) => {
  const h = (await request.get(GATE + '/')).headers();
  expect(h['cache-control']).toBe('no-cache');                       // else a new index.html meets an old core.js
  expect(h['content-security-policy']).toMatch(/connect-src[^;]*https:\/\/fastly\.jsdelivr\.net/); // ZXing WASM
  expect(h['x-frame-options']).toBe('DENY');
});

test('a real EAN-13 image is decoded through the polyfill', async ({ page }) => {
  await page.addInitScript(() => { delete window.BarcodeDetector; });   // force the polyfill + WASM path
  await page.route('**/ai/chat', (r) => r.abort());                     // no LLM cost
  const errors = [];
  page.on('console', (m) => { if (/Content Security Policy|wasm/i.test(m.text())) errors.push(m.text()); });
  await page.goto(GATE + '/');
  await page.click('#tabUp');
  await page.setInputFiles('#file', path.join(__dirname, 'fixtures', 'ean13-8935000100011.png'));
  await expect(page.locator('#result .val')).toHaveText('8935000100011', { timeout: 20000 });
  await expect(page.locator('#product')).toContainText('Tai nghe');     // and the BE lookup through /api/
  expect(errors).toEqual([]);
});
