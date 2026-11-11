// Pure scanner logic (no DOM) — same rules as barcodestalk.com's scanner. Loaded by index.html and by core.test.js.
(function (root) {
  'use strict';

  // GS1 mod-10 check digit (UPC/EAN/ITF): weights 3,1,3,1… from the right, excluding the check digit.
  function gs1CheckValid(s) {
    if (!/^\d+$/.test(s)) return false;
    var sum = 0, mult = 3;
    for (var i = s.length - 2; i >= 0; i--) { sum += +s[i] * mult; mult = mult === 3 ? 1 : 3; }
    return (10 - (sum % 10)) % 10 === +s[s.length - 1];
  }

  // Rejects misreads: retail numeric codes must pass the check digit before they count toward agreement.
  function isReadTrustworthy(value, fmt) {
    if (['ean_13', 'upc_a', 'ean_8', 'itf'].indexOf(fmt) !== -1 && /^\d+$/.test(value) &&
        [8, 12, 13, 14].indexOf(value.length) !== -1) return gs1CheckValid(value);
    return true;
  }

  // An EAN-13 with a leading 0 is really a UPC-A; show the 12-digit form.
  function normalizeCode(value, format) {
    var f = (format || '').toLowerCase();
    if ((f === 'ean_13' || f === '') && /^0\d{12}$/.test(value)) return { value: value.slice(1), format: 'upc_a' };
    return { value: value, format: f };
  }

  // Used for manually typed numbers, where no decoder told us the format.
  function formatFromValue(v) {
    if (/^B[0-9A-Z]{9}$/.test(v)) return 'asin';
    if (!/^\d+$/.test(v)) return /^https?:\/\//i.test(v) ? 'qr_code' : 'unknown';
    return { 12: 'upc_a', 13: 'ean_13', 14: 'itf', 8: 'ean_8' }[v.length] || (v.length >= 6 && v.length <= 8 ? 'upc_e' : 'unknown');
  }

  // Multi-frame agreement: a value is accepted only after `need` matching reads within `windowMs`.
  function createAgreement(need, windowMs) {
    var tally = {}, last = 0;
    return {
      push: function (value, now) {
        if (now - last > windowMs) tally = {};
        last = now;
        tally[value] = (tally[value] || 0) + 1;
        if (tally[value] >= need) { tally = {}; return { accepted: true, count: need }; }
        return { accepted: false, count: tally[value] };
      },
      reset: function () { tally = {}; },
      expire: function (now) { if (now - last > windowMs) tally = {}; },
    };
  }

  // Splits a QR payload into a label and fields (Wi-Fi, contact, phone, email, SMS, location, link, or plain text).
  function parseQr(v) {
    var m = function (re) { return (v.match(re) || [])[1] || ''; };
    var unesc = function (s) { return s.replace(/\\([\\;,:"])/g, '$1'); };
    if (/^wifi:/i.test(v)) return { label: 'Wi-Fi', fields: { Network: unesc(m(/S:((?:\\.|[^;])*)/i)), Password: unesc(m(/P:((?:\\.|[^;])*)/i)), Security: m(/T:([^;]*)/i).toUpperCase() } };
    if (/^begin:vcard/i.test(v)) return { label: 'Contact', fields: { Name: m(/(?:^|\n)FN:([^\r\n]*)/i), Phone: m(/TEL[^:\r\n]*:([^\r\n]*)/i), Email: m(/EMAIL[^:\r\n]*:([^\r\n]*)/i), Org: m(/(?:^|\n)ORG:([^\r\n]*)/i) } };
    if (/^tel:/i.test(v)) return { label: 'Phone', fields: { Number: v.slice(4) }, href: v };
    if (/^mailto:/i.test(v)) return { label: 'Email', fields: { To: v.slice(7).split('?')[0] }, href: v };
    if (/^smsto?:/i.test(v)) { var p = v.replace(/^smsto?:/i, '').split(/[:?]/); return { label: 'SMS', fields: { Number: p[0] || '', Message: p[1] || '' }, href: 'sms:' + (p[0] || '') }; }
    if (/^geo:/i.test(v)) { var c = v.slice(4).split('?')[0]; return { label: 'Location', fields: { Coordinates: c }, href: 'https://www.google.com/maps?q=' + encodeURIComponent(c) }; }
    if (/^https?:\/\//i.test(v)) return { label: 'Link', fields: {}, href: v };
    return { label: 'Text', fields: {} };
  }

  var api = { gs1CheckValid: gs1CheckValid, isReadTrustworthy: isReadTrustworthy, normalizeCode: normalizeCode, formatFromValue: formatFromValue, createAgreement: createAgreement, parseQr: parseQr };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.ScanCore = api;
})(this);
