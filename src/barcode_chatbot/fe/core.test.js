// Run: node core.test.js
const assert = require('node:assert');
const C = require('./core.js');

assert.ok(C.gs1CheckValid('036000291452'));        // valid UPC-A
assert.ok(!C.gs1CheckValid('036000291453'));       // bad check digit
assert.ok(C.gs1CheckValid('4006381333931'));       // valid EAN-13
assert.ok(C.gs1CheckValid('96385074'));            // valid EAN-8
assert.ok(!C.isReadTrustworthy('036000291453', 'upc_a'));
assert.ok(C.isReadTrustworthy('HELLO', 'code_128'));

assert.deepStrictEqual(C.normalizeCode('0036000291452', 'ean_13'), { value: '036000291452', format: 'upc_a' });
assert.deepStrictEqual(C.normalizeCode('4006381333931', 'EAN_13'), { value: '4006381333931', format: 'ean_13' });

assert.strictEqual(C.formatFromValue('036000291452'), 'upc_a');
assert.strictEqual(C.formatFromValue('B08N5WRWNW'), 'asin');
assert.strictEqual(C.formatFromValue('1234567'), 'upc_e');

const a = C.createAgreement(3, 1500);
assert.deepStrictEqual(a.push('X', 0), { accepted: false, count: 1 });
a.push('Y', 100);                                   // a stray misread doesn't reset X
assert.strictEqual(a.push('X', 200).count, 2);
assert.ok(a.push('X', 300).accepted);
assert.strictEqual(a.push('X', 400).count, 1);      // tally clears after acceptance
assert.strictEqual(a.push('X', 5000).count, 1);     // window expired

const w = C.parseQr('WIFI:T:WPA;S:My\\;Net;P:pa\\:ss;;');
assert.deepStrictEqual(w.fields, { Network: 'My;Net', Password: 'pa:ss', Security: 'WPA' });
assert.strictEqual(C.parseQr('https://x.com').label, 'Link');
assert.strictEqual(C.parseQr('hello').label, 'Text');

console.log('all core tests passed');
