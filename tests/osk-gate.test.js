// Regression test for the on-screen keyboard auto-mode gate.
// The OSK vanished on the kiosk itself after a wireless keyboard/mouse dongle
// was plugged into the Pi: that flips Chromium's primary pointer to "fine",
// and the old gate required `pointer: coarse`. This pins the rule that the
// kiosk keeps its keyboard while tablets and phones still defer to the
// native one (issue #50).
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const TEMPLATE = process.argv[2] || path.join(__dirname, '..', 'templates', 'index.html');
const html = fs.readFileSync(TEMPLATE, 'utf8');

function grab(re, label) {
  const m = html.match(re);
  if (!m) throw new Error(`could not find ${label} in the template`);
  return m[0];
}

const src = [
  grab(/function isKioskDisplay\(\)\{[\s\S]*?\n  \}/, 'isKioskDisplay'),
  grab(/function oskMode\(\)\{[\s\S]*?\n  \}/, 'oskMode'),
  grab(/function oskEnabled\(\)\{[\s\S]*?\n  \}/, 'oskEnabled'),
].join('\n');

// Each scenario: hostname, viewport, which pointer types exist, saved mode.
function evaluate({ hostname, width, height, anyCoarse, primaryCoarse, mode }) {
  const dom = new JSDOM('<body></body>', {
    url: `http://${hostname}:8080/`,
    runScripts: 'dangerously',
  });
  const { window } = dom;
  if (mode !== undefined) window.localStorage.setItem('vinyl_osk_mode', mode);
  // Model matchMedia for just the features the gate asks about
  window.matchMedia = (q) => {
    let matches = true;
    const w = q.match(/max-width:\s*(\d+)px/);
    const h = q.match(/max-height:\s*(\d+)px/);
    if (w) matches = matches && width <= Number(w[1]);
    if (h) matches = matches && height <= Number(h[1]);
    if (/\(any-pointer:\s*coarse\)/.test(q)) matches = matches && anyCoarse;
    if (/\((?<!any-)pointer:\s*coarse\)/.test(q) || /[^-]\(pointer:\s*coarse\)/.test(q)) {
      matches = matches && primaryCoarse;
    }
    return { matches };
  };
  window.eval(`
    ${src}
    window.__result = oskEnabled();
  `);
  return window.__result;
}

const results = [];
function check(name, want, scenario) {
  let pass = false, detail = '';
  try {
    const got = evaluate(scenario);
    pass = got === want;
    if (!pass) detail = `got ${got}, want ${want}`;
  } catch (e) { detail = e.message; }
  results.push({ name, pass, detail });
}

const KIOSK = { hostname: 'localhost', width: 1024, height: 600, anyCoarse: true, primaryCoarse: true };

// The reported bug: a keyboard/mouse dongle must not disable the kiosk keyboard
check('kiosk touchscreen, no dongle', true, { ...KIOSK });
check('kiosk touchscreen WITH keyboard/mouse dongle', true,
  { ...KIOSK, primaryCoarse: false });

// Issue #50 must stay fixed: other devices get their native keyboard
check('tablet over the network stays native', false,
  { hostname: 'vinyl.local', width: 820, height: 1180, anyCoarse: true, primaryCoarse: true });
check('phone in landscape stays native', false,
  { hostname: 'vinyl.local', width: 844, height: 390, anyCoarse: true, primaryCoarse: true });
check('phone by IP stays native', false,
  { hostname: '192.168.50.50', width: 390, height: 844, anyCoarse: true, primaryCoarse: true });
check('desktop browser stays native', false,
  { hostname: 'vinyl.local', width: 1920, height: 1080, anyCoarse: false, primaryCoarse: false });

// A localhost client that is not the little screen (e.g. a monitor on the Pi)
check('localhost but full-size display', false,
  { hostname: 'localhost', width: 1920, height: 1080, anyCoarse: false, primaryCoarse: false });
check('localhost, right size, but no touchscreen at all', false,
  { ...KIOSK, anyCoarse: false, primaryCoarse: false });

// Explicit overrides in Settings always win
check('forced on from a phone', true,
  { hostname: 'vinyl.local', width: 390, height: 844, anyCoarse: true, primaryCoarse: true, mode: 'on' });
check('forced off on the kiosk', false, { ...KIOSK, mode: 'off' });

let failed = 0;
for (const r of results) {
  if (!r.pass) failed++;
  console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  (' + r.detail + ')' : ''}`);
}
console.log(failed === 0 ? `\nAll ${results.length} checks passed` : `\n${failed} of ${results.length} FAILED`);
process.exit(failed === 0 ? 0 : 1);
