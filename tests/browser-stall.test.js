// Regression test for the looping reported on issue #49: when the live audio
// element runs dry, calling play() replays the buffered range forever instead
// of resuming live audio. The element must reconnect instead.
// Runs the REAL resumeBrowserElement / stall-watchdog source out of
// templates/index.html.
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
  grab(/var BROWSER_STALL_TICKS=[^\n]*/, 'BROWSER_STALL_TICKS'),
  grab(/var BROWSER_MAX_RECONNECTS=[^\n]*/, 'BROWSER_MAX_RECONNECTS'),
  grab(/var BROWSER_START_TICKS=[^\n]*/, 'BROWSER_START_TICKS'),
  grab(/function reconnectBrowserAudio\(reason\)\{[\s\S]*?\n\}/, 'reconnectBrowserAudio'),
  grab(/function resumeBrowserElement\(el\)\{[\s\S]*?\n\}/, 'resumeBrowserElement'),
  grab(/function startBrowserStallWatch\(\)\{[\s\S]*?\n\}/, 'startBrowserStallWatch'),
  grab(/function stopBrowserStallWatch\(\)\{[^\n]*\}/, 'stopBrowserStallWatch'),
].join('\n');

const dom = new JSDOM('<body><audio id="browser-audio"></audio></body>',
  { runScripts: 'dangerously' });
const { window } = dom;

// Stub the app globals the extracted source closes over. The <audio> element
// in jsdom has no real playback, so currentTime/buffered/readyState are
// modelled explicitly: that is exactly the state we need to control.
window.eval(`
  var _browserStreamActive=true, _browserStreamId='abc123';
  var _browserStallTimer=null,_browserLastTime=-1,_browserStallTicks=0,_browserReconnects=0;
  var _browserEverPlayed=false,_browserStartTicks=0;
  window.__toasts=[]; window.__plays=0;
  function showToast(m){window.__toasts.push(m)}
  function showError(m){window.__toasts.push('ERR:'+m)}
  ${src}
  // Model the media element
  window.__setEl=function(o){
    var el=document.getElementById('browser-audio');
    el.play=function(){window.__plays++;return {catch:function(){}}};
    Object.defineProperty(el,'paused',{value:o.paused,configurable:true});
    Object.defineProperty(el,'readyState',{value:o.readyState,configurable:true});
    Object.defineProperty(el,'currentTime',{value:o.currentTime,configurable:true});
    Object.defineProperty(el,'buffered',{configurable:true,value:{
      length:o.bufferedEnd===undefined?0:1,
      end:function(){return o.bufferedEnd}
    }});
    if(o.src!==undefined)el.setAttribute('src',o.src);
    return el;
  };
`);

const results = [];
function check(name, fn) {
  let pass = false, detail = '';
  try { detail = fn(); pass = detail === true || detail === undefined; }
  catch (e) { detail = e.message; }
  results.push({ name, pass: pass === true, detail: pass === true ? '' : detail });
}
const eq = (got, want) => got === want ? true : `got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`;
const BROWSER_START_TICKS_VALUE = window.eval('BROWSER_START_TICKS');
const srcOf = () => window.document.getElementById('browser-audio').getAttribute('src');
function reset(state, everPlayed = true) {
  window.eval('window.__plays=0;window.__toasts=[];_browserReconnects=0;_browserStallTicks=0;'
    + '_browserLastTime=-1;_browserStreamActive=true;_browserStartTicks=0;'
    + '_browserEverPlayed=' + (everPlayed ? 'true' : 'false') + ';');
  window.__setEl(state);
}

// ── resumeBrowserElement: the reported bug ──────────────────────────────────
check('spent buffer reconnects instead of replaying', () => {
  // This is ltdan's case: playback ran dry, so play() would loop the buffer
  reset({ paused: true, readyState: 4, currentTime: 30, bufferedEnd: 30, src: '/api/stream/abc123' });
  window.eval('resumeBrowserElement(document.getElementById("browser-audio"))');
  const s = srcOf();
  if (!s.includes('?r=')) return `expected a reconnect, src is ${s}`;
  return eq(s.startsWith('/api/stream/abc123?r='), true);
});

check('healthy buffer just resumes, no reconnect', () => {
  reset({ paused: true, readyState: 4, currentTime: 10, bufferedEnd: 30, src: '/api/stream/abc123' });
  window.eval('resumeBrowserElement(document.getElementById("browser-audio"))');
  if (srcOf().includes('?r=')) return 'should not have reconnected a healthy element';
  return eq(window.eval('window.__plays'), 1);
});

check('low readyState reconnects', () => {
  reset({ paused: true, readyState: 1, currentTime: 10, bufferedEnd: 30, src: '/api/stream/abc123' });
  window.eval('resumeBrowserElement(document.getElementById("browser-audio"))');
  return eq(srcOf().includes('?r='), true);
});

// ── stall watchdog ──────────────────────────────────────────────────────────
function tick(n) { for (let i = 0; i < n; i++) window.eval('__stallTick()'); }
// Drive the watchdog body directly rather than waiting on real timers. Lifted
// verbatim from the setInterval callback in startBrowserStallWatch so the test
// exercises the shipped logic, not a paraphrase of it.
const watchBody = grab(/function startBrowserStallWatch\(\)\{[\s\S]*?\n\}/, 'startBrowserStallWatch')
  .match(/setInterval\(function\(\)\{([\s\S]*?)\n  \},2000\)/)[1];
window.eval(`function __stallTick(){${watchBody}\n}`);

check('a frozen clock while playing triggers a reconnect', () => {
  reset({ paused: false, readyState: 4, currentTime: 12, bufferedEnd: 12, src: '/api/stream/abc123' });
  tick(1);                       // establishes the baseline
  if (srcOf().includes('?r=')) return 'reconnected far too eagerly';
  tick(3);                       // three ticks with no progress
  return eq(srcOf().includes('?r='), true);
});

check('advancing playback never reconnects', () => {
  reset({ paused: false, readyState: 4, currentTime: 0, bufferedEnd: 30, src: '/api/stream/abc123' });
  for (let i = 1; i <= 10; i++) {
    window.__setEl({ paused: false, readyState: 4, currentTime: i * 2, bufferedEnd: 30 });
    window.eval('__stallTick()');
  }
  return eq(srcOf().includes('?r='), false);
});

check('a genuinely paused element is not treated as stalled', () => {
  reset({ paused: true, readyState: 4, currentTime: 12, bufferedEnd: 30, src: '/api/stream/abc123' });
  tick(6);
  return eq(srcOf().includes('?r='), false);
});

check('reconnects are capped rather than looping forever', () => {
  reset({ paused: false, readyState: 4, currentTime: 5, bufferedEnd: 5, src: '/api/stream/abc123' });
  for (let i = 0; i < 60; i++) window.eval('__stallTick()');
  const n = window.eval('_browserReconnects');
  if (n > window.eval('BROWSER_MAX_RECONNECTS')) return `reconnected ${n} times, over the cap`;
  return eq(window.eval('_browserStreamActive'), false);
});

check('reconnect keeps the same stream id', () => {
  reset({ paused: true, readyState: 1, currentTime: 5, bufferedEnd: 5, src: '/api/stream/abc123' });
  window.eval('resumeBrowserElement(document.getElementById("browser-audio"))');
  return eq(srcOf().split('?')[0], '/api/stream/abc123');
});

// ── startup is not a stall (the "no sound until I pick it again" report) ────
check('waiting for the first audio does not reconnect early', () => {
  // Nothing has been fed yet: currentTime sits at 0 while the server starts
  // the player. The old watchdog reconnected after 3 ticks and killed it.
  reset({ paused: false, readyState: 2, currentTime: 0, bufferedEnd: 0, src: '/api/stream/abc123' }, false);
  for (let i = 0; i < BROWSER_START_TICKS_VALUE - 1; i++) window.eval('__stallTick()');
  return eq(srcOf().includes('?r='), false);
});

check('audio arriving late still plays, never reconnected', () => {
  reset({ paused: false, readyState: 2, currentTime: 0, bufferedEnd: 0, src: '/api/stream/abc123' }, false);
  for (let i = 0; i < 6; i++) window.eval('__stallTick()');   // 12s of nothing
  // ...then the server finally starts feeding
  for (let i = 1; i <= 5; i++) {
    window.__setEl({ paused: false, readyState: 4, currentTime: i * 2, bufferedEnd: 30 });
    window.eval('__stallTick()');
  }
  if (srcOf().includes('?r=')) return 'reconnected while audio was arriving';
  return eq(window.eval('_browserEverPlayed'), true);
});

check('a stream that never delivers is eventually reopened once', () => {
  reset({ paused: false, readyState: 2, currentTime: 0, bufferedEnd: 0, src: '/api/stream/abc123' }, false);
  for (let i = 0; i < BROWSER_START_TICKS_VALUE; i++) window.eval('__stallTick()');
  return eq(srcOf().includes('?r='), true);
});

check('startup grace is longer than the stall window', () => {
  const start = window.eval('BROWSER_START_TICKS'), stall = window.eval('BROWSER_STALL_TICKS');
  return start > stall ? true : `start=${start} stall=${stall}`;
});

check('a stopped session does not reconnect', () => {
  reset({ paused: false, readyState: 1, currentTime: 5, bufferedEnd: 5, src: '/api/stream/abc123' });
  window.eval('_browserStreamActive=false');
  window.eval('reconnectBrowserAudio("test")');
  return eq(srcOf().includes('?r='), false);
});

let failed = 0;
for (const r of results) {
  if (!r.pass) failed++;
  console.log(`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}${r.detail ? '  (' + r.detail + ')' : ''}`);
}
console.log(failed === 0 ? `\nAll ${results.length} checks passed` : `\n${failed} of ${results.length} FAILED`);
process.exit(failed === 0 ? 0 : 1);
