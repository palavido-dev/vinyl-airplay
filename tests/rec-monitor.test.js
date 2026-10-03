// Regression test for issue #77: the recording panel must always offer a way
// back to listening.
//
// Playing something from the catalog stops the live vinyl stream, because both
// want the same output. That is fine, but it used to leave the active recording
// panel with no monitor controls and no way to start listening again short of
// stopping the recording.
//
// Runs the REAL syncRecMonitorControls source extracted from
// templates/index.html against the REAL recording-panel markup from the same file.
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const TEMPLATE = process.argv[2] || path.join(__dirname, '..', 'templates', 'index.html');
const APP_JS = process.argv[3] || path.join(__dirname, '..', 'static', 'js', 'app.js');
const html = fs.readFileSync(TEMPLATE, 'utf8') + '\n' + fs.readFileSync(APP_JS, 'utf8');

function grab(re, label) {
  const m = html.match(re);
  if (!m) throw new Error(`could not find ${label} in the template`);
  return m[0];
}

const src = grab(/function syncRecMonitorControls\(\)\{[\s\S]*?\n\}/, 'syncRecMonitorControls');

// The real monitor blocks, lifted from the template so the ids and default
// display values are the ones that actually ship.
const monitorBlock = grab(/<div id="rec-monitor-controls"[\s\S]*?\n            <\/div>/, 'monitor controls markup');
const resumeBlock = grab(/<div id="rec-monitor-resume"[\s\S]*?\n            <\/div>/, 'monitor resume markup');
const markup = `
  ${monitorBlock}
  ${resumeBlock}
  <input id="eq-volume" value="80"><input id="eq-bass" value="2"><input id="eq-treble" value="3">`;

const dom = new JSDOM(`<body>${markup}</body>`, { runScripts: 'dangerously' });
const { window } = dom;
window.eval(`var _isStreaming=false, _recAlbumId=null;\n${src}`);

const results = [];
function check(name, fn) {
  let pass = false, detail = '';
  try { detail = fn(); pass = detail === true || detail === undefined; }
  catch (e) { detail = e.message; }
  results.push({ name, pass: pass === true, detail: pass === true ? '' : detail });
}
function eq(got, want) { return got === want ? true : `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`; }

function render(streaming, albumId) {
  window.eval(`_isStreaming=${streaming}; _recAlbumId=${albumId === null ? 'null' : albumId}; syncRecMonitorControls()`);
  const d = window.document;
  return {
    sliders: d.getElementById('rec-monitor-controls').style.display !== 'none',
    listen: d.getElementById('rec-monitor-resume').style.display !== 'none',
  };
}

// --- What the panel offers, in each state a recording can be in ---
check('monitoring while recording shows the sliders, not the Listen button', () =>
  eq(JSON.stringify(render(true, 7)), JSON.stringify({ sliders: true, listen: false })));

check('recording with the output taken away offers Listen', () =>
  eq(JSON.stringify(render(false, 7)), JSON.stringify({ sliders: false, listen: true })));

check('no recording in progress offers neither', () =>
  eq(JSON.stringify(render(false, null)), JSON.stringify({ sliders: false, listen: false })));

check('streaming with no recording still shows no Listen prompt', () =>
  eq(JSON.stringify(render(true, null)), JSON.stringify({ sliders: true, listen: false })));

// --- The transition that produced the bug report ---
check('starting playback mid-recording swaps sliders for Listen', () => {
  const before = render(true, 7);           // listening while Side B records
  const after = render(false, 7);           // a catalog track takes the output
  if (!before.sliders) return 'was not monitoring to begin with';
  if (after.sliders) return 'sliders still shown after the stream stopped';
  return after.listen === true ? true : 'no way back to listening was offered';
});

check('listening again restores the sliders', () => {
  render(false, 7);
  const after = render(true, 7);
  return after.sliders && !after.listen ? true : JSON.stringify(after);
});

// --- The sliders must carry the live values when they appear ---
check('sliders adopt the current volume and tone when shown', () => {
  render(true, 7);
  const d = window.document;
  return eq(
    [d.getElementById('rec-mon-volume').value, d.getElementById('rec-mon-bass').value,
     d.getElementById('rec-mon-treble').value].join(','),
    '80,2,3');
});

let failed = 0;
for (const r of results) {
  if (!r.pass) failed++;
  console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? `  (${r.detail})` : ''}`);
}
console.log(failed ? `\n${failed} of ${results.length} FAILED` : `\nAll ${results.length} checks passed`);
process.exit(failed ? 1 : 0);
