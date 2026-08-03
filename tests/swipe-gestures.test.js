// Regression test for issue #44: dragging an EQ/volume slider inside the
// now-playing footer must not register as a prev/next swipe.
// Runs the REAL initSwipeGestures source extracted from templates/index.html
// against the REAL footer markup extracted from the same file.
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const TEMPLATE = process.argv[2] || path.join(__dirname, '..', 'templates', 'index.html');
const html = fs.readFileSync(TEMPLATE, 'utf8');

// Pull the actual footer markup out of the template
const footerStart = html.indexOf('<div class="np-bar hidden" id="np-footer">');
if (footerStart < 0) throw new Error('np-footer not found');
const footerEnd = html.indexOf('<!-- Toast Container -->', footerStart);
const footerHtml = html.slice(footerStart, footerEnd);

// Pull the actual gesture code out of the template
// SWIPE_IGNORE_SEL is absent in the pre-fix source; the test then runs the old
// handler unchanged, which is the point of the regression check.
const selMatch = html.match(/var SWIPE_IGNORE_SEL=[^\n]*/) || [''];
const fnMatch = html.match(/function initSwipeGestures\(\)\{[\s\S]*?\n\}/);
if (!fnMatch) throw new Error('gesture source not found');

const dom = new JSDOM(`<body>${footerHtml}</body>`, {
  pretendToBeVisual: true,
  runScripts: 'dangerously',
});
const { window } = dom;

// Evaluate the real source in the jsdom window, with the transport calls
// stubbed to counters the test can read back off the window.
window.eval(`
  window.__prev=0; window.__next=0;
  function playerPrev(){window.__prev++}
  function playerNext(){window.__next++}
  var _swipeStartX=0,_swipeStartY=0,_swipeIgnore=false;
  ${selMatch[0]}
  ${fnMatch[0]}
  initSwipeGestures();
`);

// jsdom lacks PointerEvent; MouseEvent carries the clientX/clientY the handler reads
function drag(el, fromX, toX, y = 100) {
  el.dispatchEvent(new window.MouseEvent('pointerdown', { clientX: fromX, clientY: y, bubbles: true }));
  el.dispatchEvent(new window.MouseEvent('pointerup', { clientX: toX, clientY: y, bubbles: true }));
}

const results = [];
function check(name, expectPrev, expectNext, fn) {
  window.__prev = 0; window.__next = 0;
  fn();
  const pass = window.__prev === expectPrev && window.__next === expectNext;
  results.push({ name, pass, got: `prev=${window.__prev} next=${window.__next}`, want: `prev=${expectPrev} next=${expectNext}` });
}

const q = (s) => window.document.querySelector(s);

// The reported bug: a long rightward drag on a slider fired playerPrev,
// which jumped the seek bar backwards.
check('drag volume slider right', 0, 0, () => drag(q('#eq-volume'), 100, 300));
check('drag volume slider left', 0, 0, () => drag(q('#eq-volume'), 300, 100));
check('drag bass slider right', 0, 0, () => drag(q('#eq-bass'), 100, 300));
check('drag treble slider left', 0, 0, () => drag(q('#eq-treble'), 300, 100));
check('drag across the seek bar', 0, 0, () => drag(q('.np-progress-bar'), 100, 300));
check('tap a transport button', 0, 0, () => drag(q('#np-play-pause'), 100, 105));

// The gesture must still work where it was intended: on the footer body.
check('swipe right on footer body', 1, 0, () => drag(q('#np-footer .np-text'), 100, 300));
check('swipe left on footer body', 0, 1, () => drag(q('#np-footer .np-text'), 300, 100));
check('short tap on footer body', 0, 0, () => drag(q('#np-footer .np-text'), 100, 110));

// A drag that leaves the footer mid-gesture must not arm a later swipe.
check('slider drag cancelled, then real swipe', 1, 0, () => {
  q('#eq-volume').dispatchEvent(new window.MouseEvent('pointerdown', { clientX: 100, clientY: 100, bubbles: true }));
  q('#np-footer').dispatchEvent(new window.MouseEvent('pointercancel', { bubbles: true }));
  drag(q('#np-footer .np-text'), 100, 300);
});

let failed = 0;
for (const r of results) {
  if (!r.pass) failed++;
  console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}  (got ${r.got}, want ${r.want})`);
}
console.log(failed === 0 ? `\nAll ${results.length} checks passed` : `\n${failed} of ${results.length} FAILED`);
process.exit(failed === 0 ? 0 : 1);
