// Regression test for issue #74: deciding when an album offers a Resume point,
// and what that Resume button says.
// Runs the REAL albumResumePoint / updateResumeButton source extracted from
// templates/index.html against the REAL modal button markup from the same file.
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
  grab(/function fmtTime\(s\)\{[^\n]*\}/, 'fmtTime'),
  grab(/var RESUME_MIN_SECS=[^\n]*/, 'RESUME_MIN_SECS'),
  grab(/function albumResumePoint\(a\)\{[\s\S]*?\n\}/, 'albumResumePoint'),
  grab(/function updateResumeButton\(a,hasAudio\)\{[\s\S]*?\n\}/, 'updateResumeButton'),
].join('\n');

// Only the two buttons the resume logic touches
const markup = `
  <button id="btn-modal-resume" style="display:none"><span id="btn-modal-resume-label">Resume</span></button>
  <button id="btn-modal-play"><span id="btn-modal-play-label">Play</span></button>`;

const dom = new JSDOM(`<body>${markup}</body>`, { runScripts: 'dangerously' });
const { window } = dom;
window.eval(`var _modalResume=null;\n${src}`);

const results = [];
function check(name, fn) {
  let pass = false, detail = '';
  try { detail = fn(); pass = detail === true || detail === undefined; }
  catch (e) { detail = e.message; }
  results.push({ name, pass: pass === true, detail: pass === true ? '' : detail });
}
function eq(got, want) { return got === want ? true : `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`; }

const resumePoint = (a) => window.eval(`JSON.stringify(albumResumePoint(${JSON.stringify(a)}))`);
function render(a, hasAudio = true) {
  window.eval(`updateResumeButton(${JSON.stringify(a)}, ${hasAudio})`);
  const d = window.document;
  return {
    shown: d.getElementById('btn-modal-resume').style.display !== 'none',
    resumeLabel: d.getElementById('btn-modal-resume-label').textContent,
    playLabel: d.getElementById('btn-modal-play-label').textContent,
  };
}

// --- albumResumePoint: when is a saved position worth offering? ---
check('no saved position means no resume', () =>
  eq(resumePoint({ last_position_secs: 0, last_position_side_idx: null }), 'null'));
check('a few seconds in is not worth resuming', () =>
  eq(resumePoint({ last_position_secs: 12, last_position_side_idx: 'A' }), 'null'));
check('exactly at the threshold is not offered', () =>
  eq(resumePoint({ last_position_secs: 30, last_position_side_idx: 'A' }), 'null'));
check('past the threshold is offered with its side', () =>
  eq(resumePoint({ last_position_secs: 750.4, last_position_side_idx: 'B' }),
     '{"position_secs":750.4,"side":"B"}'));
check('legacy numeric side index is ignored, offset kept', () =>
  eq(resumePoint({ last_position_secs: 300, last_position_side_idx: '1' }),
     '{"position_secs":300,"side":null}'));
check('missing side still resumes the offset', () =>
  eq(resumePoint({ last_position_secs: 300, last_position_side_idx: null }),
     '{"position_secs":300,"side":null}'));
check('a null album is safe', () => eq(resumePoint(null), 'null'));

// --- updateResumeButton: what the user actually sees ---
check('resume hidden and Play reads Play when nothing saved', () => {
  const r = render({ last_position_secs: 0, last_position_side_idx: null });
  return eq(r.shown, false) === true && eq(r.playLabel, 'Play') === true
    ? true : `shown=${r.shown} playLabel=${r.playLabel}`;
});
check('resume shows side and timestamp', () => {
  const r = render({ last_position_secs: 750, last_position_side_idx: 'B' });
  return eq(r.shown, true) === true && eq(r.resumeLabel, 'Resume Side B, 12:30') === true
    ? true : `shown=${r.shown} label=${r.resumeLabel}`;
});
check('Play becomes Start over when a resume point exists', () => {
  const r = render({ last_position_secs: 750, last_position_side_idx: 'B' });
  return eq(r.playLabel, 'Start over');
});
check('resume without a side omits the side text', () => {
  const r = render({ last_position_secs: 95, last_position_side_idx: null });
  return eq(r.resumeLabel, 'Resume 1:35');
});
check('an album with no recorded audio never offers resume', () => {
  const r = render({ last_position_secs: 750, last_position_side_idx: 'B' }, false);
  return eq(r.shown, false);
});
check('labels reset when moving from a resumable album to a fresh one', () => {
  render({ last_position_secs: 750, last_position_side_idx: 'B' });
  const r = render({ last_position_secs: 0, last_position_side_idx: null });
  return eq(r.shown, false) === true && eq(r.playLabel, 'Play') === true
    ? true : `shown=${r.shown} playLabel=${r.playLabel}`;
});

let failed = 0;
for (const r of results) {
  if (!r.pass) failed++;
  console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  (' + r.detail + ')' : ''}`);
}
console.log(failed === 0 ? `\nAll ${results.length} checks passed` : `\n${failed} of ${results.length} FAILED`);
process.exit(failed === 0 ? 0 : 1);
