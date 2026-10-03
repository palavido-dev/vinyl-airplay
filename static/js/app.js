let ws,currentAlbumId=null,_catalogAlbums=[],_playerActive=false,_playerSource=null,_isStreaming=false,_lastUsedDevice=null,_pendingAlbumId=null,_pendingTrackId=null,_currentDevices=null,_playerAlbumId=null,_learnLog=[],_learnLevelInterval=null,_audioPlayer=null,_audioPlayingId=null,_reorderMode=false,_boundaryEditMode=false,_pendingRelease=null,_recTimerInterval=null,_browserAudioCtx=null,_browserStreamReader=null,_browserStreamActive=false,_homeShelvesData={},_multiSelectMode=false,_selectedAlbums=new Set(),_smartPlaylists=[],_pendingQueue=[];
var _viewState={sortMode:'artist',groupBy:null,filterMode:null,view:'library'};
var _exportStatus={};
var _svgPlay12='<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21"/></svg>';
var _svgPause12='<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="5" y="3" width="4" height="18" rx="1"/><rect x="15" y="3" width="4" height="18" rx="1"/></svg>';

function esc(s){if(!s)return'';const d=document.createElement('div');d.textContent=s;return d.innerHTML}
function artistSortName(a){return (a||'').replace(/^the\s+/i,'')}
function fmtTime(s){return Math.floor(s/60)+':'+String(Math.floor(s%60)).padStart(2,'0')}
function fmtTimePrecise(s){const min=Math.floor(s/60);const sec=Math.floor(s%60);const tenth=Math.floor((s%1)*10);return min+':'+String(sec).padStart(2,'0')+'.'+tenth}
function parseTimeToSecs(str){const parts=str.split(':');if(parts.length!==2)return null;const min=parseInt(parts[0]);const rest=parts[1].split('.');const sec=parseInt(rest[0]);const tenth=rest[1]?parseInt(rest[1].charAt(0)):0;if(isNaN(min)||isNaN(sec))return null;return min*60+sec+tenth*0.1}
function showError(msg){const el=document.getElementById('error-msg');el.textContent='\u26A0 '+msg;el.style.display='block';setTimeout(()=>el.style.display='none',5000)}
function showToast(msg){
  const container=document.getElementById('toast-container');
  const toast=document.createElement('div');
  toast.className='toast-msg';
  toast.textContent=msg;
  container.appendChild(toast);
  setTimeout(()=>{
    toast.classList.add('removing');
    setTimeout(()=>toast.remove(),300);
  },3000);
}

var _wsLastMsg=0,_wsWatchdog=null;
function connectWS(){const p=location.protocol==='https:'?'wss':'ws';ws=new WebSocket(`${p}://${location.host}/ws`);_wsLastMsg=Date.now();ws.onopen=function(){_wsLastMsg=Date.now();syncAlbumRecState()};ws.onmessage=e=>{_wsLastMsg=Date.now();try{handleEvent(JSON.parse(e.data))}catch(err){console.warn('WS',err)}};ws.onerror=()=>{try{ws.close()}catch(_e){}};ws.onclose=()=>{if(_wsWatchdog){clearInterval(_wsWatchdog);_wsWatchdog=null;}setTimeout(connectWS,2000)};if(_wsWatchdog)clearInterval(_wsWatchdog);_wsWatchdog=setInterval(function(){if(Date.now()-_wsLastMsg>30000){try{ws.close()}catch(_e){}}},10000)}
function syncAlbumRecState(){apiFetch('/api/album-recording/status').then(r=>r.json()).then(function(d){const enc=!!(d.encoding&&d.encoding.in_progress);if(enc&&currentAlbumId===d.encoding.album_id){showAlbumRecPanel();document.getElementById('album-rec-setup').style.display='none';document.getElementById('album-rec-active').style.display='none';document.getElementById('album-rec-flip-prompt').style.display='none';document.getElementById('album-rec-saving').style.display='block';if(d.encoding.message)document.getElementById('album-rec-status-text').textContent=d.encoding.message}if(d.awaiting_flip&&d.next_side){clearInterval(_recTimerInterval);_recTimerInterval=null;setRecordingIndicator(false);_recStartTime=null;_recAlbumId=null;_recAwaitingFlipAlbumId=d.album_id||null;_recAwaitingFlipDoneSide=d.side||null;_recAwaitingFlipNextSide=d.next_side;if(currentAlbumId===d.album_id){document.getElementById('album-rec-active').style.display='none';document.getElementById('album-rec-saving').style.display='none';document.getElementById('album-rec-flip-prompt').style.display='block';document.getElementById('album-rec-done-side').textContent=d.side||'?';document.getElementById('album-rec-next-side-label').textContent=d.next_side;showAlbumRecPanel();loadAlbumAudio(d.album_id);loadCatalog()}}else if(!d.recording){clearInterval(_recTimerInterval);_recTimerInterval=null;setRecordingIndicator(false);_recStartTime=null;_recAlbumId=null;_recAwaitingFlipAlbumId=null;_recAwaitingFlipDoneSide=null;_recAwaitingFlipNextSide=null;if(!enc)document.getElementById('album-rec-saving').style.display='none'}}).catch(function(){})}

function handleEvent(d){
  if(d.event==='status')setStatus(d);
  if(d.event==='error')showError(d.message);
  if(d.event==='now_playing')renderNowPlaying(d);
  if(d.event==='player_status')onPlayerStatus(d);
  if(d.event==='queue_updated')refreshQueueDisplay();
  if(d.event==='learn_audio_detected')onLearnAudioDetected(d);
  if(d.event==='learn_end_of_side')onLearnEndOfSide(d);
  if(d.event==='learn_update')onLearnUpdate(d);
  if(d.event==='learn_paused')onLearnPaused(d);
  if(d.event==='learn_done')onLearnDone(d);
  if(d.event==='album_recording_status')onAlbumRecStatus(d);
  if(d.event==='album_recording_side_saved')onAlbumRecSideSaved(d);
  if(d.event==='auto_stream_starting')showToast('\uD83D\uDCFB '+(d.message||'Auto-streaming\u2026'));
  if(d.event==='level')onLevel(d);
  if(d.event==='sync_progress')onSyncProgress(d);
  if(d.event==='artwork_fetch_progress')onArtworkFetchProgress(d);
  if(d.event==='export_progress')handleExportProgress(d);
  if(d.event==='rebuild_fingerprints_progress')onRebuildFpProgress(d);
  if(d.event==='live_listeners')updateLiveListeners(d.count,d);
  if(d.eq)applyEQValues(d.eq.bass,d.eq.treble,d.eq.volume);
}

function updateLiveListeners(n,detail){
  var el=document.getElementById('live-listeners');if(!el)return;
  if(n>0){
    el.textContent='🎧 '+n+' listening';
    // Break the count down on hover: "This Device" sessions used to be
    // invisible here entirely (#49)
    el.title=detail?((detail.browser||0)+' on this app, '+(detail.http||0)+' on the live MP3 URL'):'';
    el.style.display='';
  }
  else{el.style.display='none';el.title=''}
}

function setStatus(d){
  const s=d.streaming;_isStreaming=!!s;
  syncRecMonitorControls();
  document.getElementById('dot').className='status-dot'+(s?' on':'');
  let msg=d.message||(s?'Streaming':'Idle');
  if(s&&d.devices&&d.devices.length)msg='Streaming to '+d.devices.join(', ');
  if(d.listening)msg='Listening (audio capture active)';
  document.getElementById('status-text').textContent=msg;
  const bs=document.getElementById('btn-start-stream'),bt=document.getElementById('btn-stop-stream');
  if(bs)bs.style.display=s?'none':'';if(bt)bt.style.display=s?'':'none';
  if(bs)bs.disabled=false;if(bt)bt.disabled=false;
  if(s&&!_playerActive){_playerSource='vinyl';showNPFooter()}
  if(!s&&_playerSource==='vinyl'&&!_playerActive)hideNPFooter();
  // Update input bar if input_level is present
  if (typeof d.input_level === 'number') {
    const rms = Math.max(d.input_level, 1e-8);
    const db = 20 * Math.log10(rms);
    onLevel({db});
  }
}

function sleepMs(ms){return new Promise(resolve=>setTimeout(resolve,ms))}

async function refreshStatus(){
  try{
    const d=await apiFetch('/api/status',{cache:'no-store'}).then(r=>r.json());
    setStatus(d);
    if(typeof d.live_listeners==='number')updateLiveListeners(d.live_listeners,d.listener_detail);
    // Also update input bar if input_level is present
    if (typeof d.input_level === 'number') {
      const rms = Math.max(d.input_level, 1e-8);
      const db = 20 * Math.log10(rms);
      onLevel({db});
    }
    return d;
  }catch(e){
    return null;
  }
}

async function waitForStreamingState(targetState,maxAttempts,delayMs){
  const attempts=maxAttempts||12;
  const delay=delayMs||250;
  for(let i=0;i<attempts;i++){
    await refreshStatus();
    if(!!_isStreaming===!!targetState)return true;
    await sleepMs(delay);
  }
  return !!_isStreaming===!!targetState;
}
function showNPFooter(){const f=document.getElementById('np-footer');f.classList.remove('hidden');f.style.display='';document.getElementById('np-hero').classList.add('visible');document.getElementById('main-content').classList.add('has-hero')}
function hideNPFooter(){const f=document.getElementById('np-footer');f.classList.add('hidden');f.style.display='none';_playerSource=null;var mc=document.getElementById('main-content');document.getElementById('np-hero').classList.remove('visible');mc.classList.remove('has-hero');mc.classList.remove('library-browse');document.body.classList.remove('np-expandable')}
function showLibraryDuringPlayback(){document.getElementById('main-content').classList.add('library-browse');document.body.classList.add('np-expandable')}
function showHeroFromLibrary(){document.getElementById('main-content').classList.remove('library-browse');document.body.classList.remove('np-expandable')}

async function loadCatalog(){
  try{const [d,es]=await Promise.all([apiFetch('/api/catalog').then(r=>r.json()),apiFetch('/api/export/all-status').then(r=>r.json()).catch(()=>({albums:{}}))]);_catalogAlbums=d.albums||[];var ea=es.albums||{};_exportStatus={};Object.keys(ea).forEach(k=>{_exportStatus[parseInt(k)]=ea[k]});renderCatalog(getFilteredAlbums());updateAlbumCount();if(_viewState.view==='shelves')renderHomeView()}
  catch(e){showError('Failed to load catalog')}
}

// ── View Switching ──
async function switchView(view){
  _viewState.view=view;
  if(view==='shelves'){_activeShelfFilter=null}
  // If the now-playing hero is covering the main area, swap to library-browse
  // so clicking Shelves/Library from the header always reveals the grid.
  var _mc=document.getElementById('main-content');
  if(_mc&&_mc.classList.contains('has-hero')){_mc.classList.add('library-browse');document.body.classList.add('np-expandable')}
  document.getElementById('view-shelves').classList.toggle('active',view==='shelves');
  document.getElementById('view-library').classList.toggle('active',view==='library');
  document.getElementById('home-view').style.display=view==='shelves'?'':'none';
  document.getElementById('library-view').style.display=view==='library'?'':'none';
  document.getElementById('btn-sort').style.display=view==='library'?'':'none';
  document.getElementById('btn-select').style.display=view==='library'?'':'none';
  var shelfBanner=document.getElementById('shelf-filter-banner');
  if(_activeShelfFilter){
    document.getElementById('catalog-search').placeholder='Search within '+_activeShelfFilter+'...';
    if(shelfBanner)shelfBanner.style.display='flex';
    if(shelfBanner)document.getElementById('shelf-filter-name').textContent=_activeShelfFilter;
  }else{
    document.getElementById('catalog-search').placeholder=view==='shelves'?'Search shelves...':'Search your collection...';
    if(shelfBanner)shelfBanner.style.display='none';
  }
  if(view==='shelves'){await renderHomeView()}
  if(view==='library'){renderCatalog(getFilteredAlbums())}
}

var _shelfData={};
async function renderHomeView(){
  try{
    const resp=await apiFetch('/api/catalog/shelves').then(r=>r.json());
    _homeShelvesData=resp;
    _shelfData={};
    let html='';
    const shelves=[
      {title:'Recently Played',items:resp.recently_played},
      {title:'Recently Added',items:resp.recently_added},
      {title:'Most Played',items:resp.most_played},
      {title:'Unplayed',items:resp.unplayed},
      {title:'Favorites',items:resp.favorites},
      {title:'Top Rated',items:resp.top_rated}
    ];
    shelves.forEach(s=>{if(s.items.length>0){_shelfData[s.title]=s.items;html+=renderShelf(s.title,s.items)}});
    Object.entries(resp.decades||{}).forEach(([title,items])=>{if(items.length>0){_shelfData[title]=items;html+=renderShelf(title,items)}});
    Object.entries(resp.genres||{}).forEach(([title,items])=>{if(items.length>0){_shelfData[title]=items;html+=renderShelf(title,items)}});
    document.getElementById('home-view').innerHTML=html;
  }catch(e){showError('Failed to load home shelves')}
}

function renderShelf(title,items){
  const hasAny=items.some(a=>a.audio_count>0);
  const itemsHtml=items.map(a=>{const art=a.user_artwork_path||a.artwork_path;const src=art?'/artwork/'+art.split('/').pop()+'?t='+a.id:'';const artEl=src?`<img src="${src}" class="shelf-item-art" alt="" loading="lazy" onerror="this.nextElementSibling.style.display='flex';this.style.display='none'">`:'';const fallbackStyle=src?'display:none':'display:flex';return `<div class="shelf-item" onclick="openAlbumDetail(${a.id})" title="${esc(a.title)}">${artEl}<div style="width:80px;height:80px;border-radius:4px;${fallbackStyle};align-items:center;justify-content:center;background:var(--paper-dk);font-size:1.8rem">&#x1F4BF;</div></div>`}).join('');
  const safeTitle=esc(title).replace(/'/g,"\\'");
  const playBtns=hasAny?`<button class="shelf-play-btn" onclick="event.stopPropagation();playShelf('${safeTitle}')" title="Play">&#9654;</button><button class="shelf-play-btn" onclick="event.stopPropagation();shuffleShelf('${safeTitle}')" title="Shuffle">&#8645;</button>`:'';
  return `<div class="shelf"><div class="shelf-header"><span class="shelf-title">${esc(title)}</span><span class="shelf-header-actions">${playBtns}<span class="shelf-see-all" onclick="filterByShelf('${safeTitle}')">See All</span></span></div><div class="shelf-scroll">${itemsHtml}</div></div>`
}

var _activeShelfFilter=null;
function filterByShelf(shelf){
  _activeShelfFilter=shelf;
  switchView('library');
  document.getElementById('catalog-search').value='';
  filterCatalog();
}
function clearShelfFilter(){
  _activeShelfFilter=null;
  filterCatalog();
}

async function playShelf(title){
  var items=_shelfData[title];
  if(!items||!items.length){showError('No albums in this shelf');return}
  var playable=items.filter(a=>a.audio_count>0);
  if(!playable.length){showError('No recorded albums in this shelf');return}
  if(!_isStreaming&&!_playerActive){_pendingAlbumId=playable[0].id;showOutputPicker();return}
  var queue=[];
  for(var i=0;i<playable.length;i++){
    var a=playable[i];
    var sides=await apiFetch('/api/catalog/'+a.id+'/tracks').then(function(r){return r.json()}).then(function(d){
      var s=new Map();(d.tracks||[]).forEach(function(t){var side=t.side||'A';if(!s.has(side))s.set(side,[]);s.get(side).push(t)});
      return Array.from(s.keys()).sort().map(function(side){return {album_id:a.id,album_title:a.title,album_artist:a.artist,side:side,tracks:s.get(side)}});
    });
    queue.push.apply(queue,sides);
  }
  await apiFetch('/api/player/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({queue:queue,devices:_currentDevices})}).catch(function(e){showError('Playback failed')});
  showToast('Playing '+title);
}

async function shuffleShelf(title){
  var items=_shelfData[title];
  if(!items||!items.length){showError('No albums in this shelf');return}
  var playable=items.filter(a=>a.audio_count>0);
  if(!playable.length){showError('No recorded albums in this shelf');return}
  // Shuffle the album order
  for(var i=playable.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1));var tmp=playable[i];playable[i]=playable[j];playable[j]=tmp}
  if(!_isStreaming&&!_playerActive){_pendingAlbumId=playable[0].id;showOutputPicker();return}
  var queue=[];
  for(var i=0;i<playable.length;i++){
    var a=playable[i];
    var sides=await apiFetch('/api/catalog/'+a.id+'/tracks').then(function(r){return r.json()}).then(function(d){
      var s=new Map();(d.tracks||[]).forEach(function(t){var side=t.side||'A';if(!s.has(side))s.set(side,[]);s.get(side).push(t)});
      return Array.from(s.keys()).sort().map(function(side){return {album_id:a.id,album_title:a.title,album_artist:a.artist,side:side,tracks:s.get(side)}});
    });
    queue.push.apply(queue,sides);
  }
  await apiFetch('/api/player/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({queue:queue,devices:_currentDevices})}).catch(function(e){showError('Playback failed')});
  showToast('Shuffling '+title);
}

// ── Sort comparators ──
var _sortComparators={
  artist:function(a,b){return artistSortName(a.artist).localeCompare(artistSortName(b.artist))||( a.title||'').localeCompare(b.title||'')},
  title:function(a,b){return (a.title||'').localeCompare(b.title||'')},
  year:function(a,b){return (b.year||0)-(a.year||0)||artistSortName(a.artist).localeCompare(artistSortName(b.artist))},
  recent_play:function(a,b){return (b.last_played||'').localeCompare(a.last_played||'')||artistSortName(a.artist).localeCompare(artistSortName(b.artist))},
  recent_add:function(a,b){return (b.created_at||'').localeCompare(a.created_at||'')||artistSortName(a.artist).localeCompare(artistSortName(b.artist))},
  favorites:function(a,b){return (b.favorite||0)-(a.favorite||0)||artistSortName(a.artist).localeCompare(artistSortName(b.artist))},
  rating:function(a,b){return (b.rating||0)-(a.rating||0)||artistSortName(a.artist).localeCompare(artistSortName(b.artist))}
};

function sortAlbums(albums){
  var cmp=_sortComparators[_viewState.sortMode]||_sortComparators.artist;
  return albums.slice().sort(cmp);
}

function renderAlbumCard(a){
  var art=a.user_artwork_path||a.artwork_path;
  var artH=art?'<img src="/artwork/'+art.split('/').pop()+'?t='+a.id+'" alt="" loading="lazy">':'<div class="album-art-placeholder">\uD83D\uDCBF</div>';
  var has=a.audio_count>0;
  var expInfo=_exportStatus[a.id];
  var badge=has?(expInfo?'<div class="album-badge exported">'+expInfo.format.toUpperCase()+'</div>':''):'<div class="album-badge unrecorded">not recorded</div>';
  var isFav=a.favorite?'is-favorite':'';
  var heart=a.favorite?'\u2665':'\u2661';
  var queueBtn=has?'<button class="album-queue-btn" onclick="event.stopPropagation();addToQueue('+a.id+')" title="Add to queue">+</button>':'';
  var stars=a.rating>0?'<div style="font-size:0.55rem;color:var(--amber-dk);letter-spacing:-0.05em">'+'\u2605'.repeat(a.rating)+'</div>':'';
  var multiSelectClass=_multiSelectMode?' multi-select-mode':'';
  var isSelected=_selectedAlbums.has(a.id)?' selected':'';
  var checkbox=_multiSelectMode?'<div class="album-card-checkbox" onclick="event.stopPropagation();toggleAlbumSelection('+a.id+')"></div>':'';
  return '<div class="album-card'+multiSelectClass+isSelected+(has?'':' unrecorded')+'" onclick="onAlbumTap('+a.id+','+has+')">'+checkbox+'<div class="album-art-wrap">'+artH+'<button class="album-favorite-btn '+isFav+'" onclick="event.stopPropagation();toggleAlbumFavorite('+a.id+')" title="Favorite">'+heart+'</button><button class="album-info-btn" onclick="event.stopPropagation();openAlbumDetail('+a.id+')" title="Details">\u2139</button>'+queueBtn+badge+'</div><div class="album-meta"><div class="title">'+esc(a.title)+'</div><div class="artist">'+esc(a.artist)+'</div>'+stars+'</div></div>';
}

function renderCatalog(albums){
  var g=document.getElementById('album-grid');
  if(!albums.length){
    if(_catalogAlbums.length===0){g.innerHTML='<div class="catalog-empty">No albums yet.<br>Tap + to add your first record.</div>'}
    else{g.innerHTML='<div class="catalog-empty">No matching albums.<br>Try adjusting your search or filters.</div>'}
    return;
  }
  var sorted=sortAlbums(albums);
  if(_viewState.groupBy){
    var groups={},order=[];
    sorted.forEach(function(a){
      if(_viewState.groupBy==='favorites'&&!a.favorite) return;
      var key=(_viewState.groupBy==='genre'?(a.genre||'Unknown Genre'):_viewState.groupBy==='favorites'?'Favorites':(a.artist||'Unknown Artist'));
      if(!groups[key]){groups[key]=[];order.push(key)}
      groups[key].push(a);
    });
    var html='';
    order.forEach(function(key){
      var playable=groups[key].filter(function(a){return a.audio_count>0}).length;
      var playBtn=playable>0?'<button class="group-play-btn" data-group="'+esc(key)+'" onclick="event.stopPropagation();playGroup(this.dataset.group)" title="Play this group"><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21"/></svg></button>':'';
      html+='<div class="group-header"><span>'+esc(key)+'</span><span class="group-count">'+groups[key].length+' album'+(groups[key].length!==1?'s':'')+'</span>'+playBtn+'</div>';
      groups[key].forEach(function(a){html+=renderAlbumCard(a)});
    });
    g.innerHTML=html;
  }else{
    g.innerHTML=sorted.map(renderAlbumCard).join('');
  }
}

function filterCatalog(q){
  renderCatalog(getFilteredAlbums());
}

var _searchDebounce=null;
var _searchDropdownOpen=false;
function onSearchInput(q){
  // Always filter album grid in background
  filterCatalog(q);
  clearTimeout(_searchDebounce);
  if(!q||!q.trim()){
    closeSearchDropdown();
    return;
  }
  _searchDebounce=setTimeout(function(){unifiedSearch(q.trim())},250);
}
function onSearchFocus(){
  var q=(document.getElementById('catalog-search').value||'').trim();
  if(q&&document.getElementById('search-dropdown').innerHTML){
    openSearchDropdown();
  }
}
function openSearchDropdown(){
  var dd=document.getElementById('search-dropdown');
  dd.classList.add('open');
  _searchDropdownOpen=true;
}
function closeSearchDropdown(){
  var dd=document.getElementById('search-dropdown');
  dd.classList.remove('open');
  _searchDropdownOpen=false;
}
async function unifiedSearch(q){
  var ql=q.toLowerCase();
  // Album matches (from local data, instant)
  var albumMatches=(_catalogAlbums||[]).filter(function(a){
    return (a.title||'').toLowerCase().indexOf(ql)!==-1
      ||(a.artist||'').toLowerCase().indexOf(ql)!==-1
      ||(a.genre||'').toLowerCase().indexOf(ql)!==-1
      ||(a.label||'').toLowerCase().indexOf(ql)!==-1
      ||String(a.year||'').indexOf(ql)!==-1;
  }).slice(0,5);
  // Song matches (from API)
  var songMatches=[];
  try{
    var r=await apiFetch('/api/catalog/tracks/search?q='+encodeURIComponent(q)).then(function(d){return d.json()});
    songMatches=(r.tracks||[]).slice(0,8);
  }catch(e){console.warn('Song search error:',e)}
  // Build dropdown
  var dd=document.getElementById('search-dropdown');
  if(!albumMatches.length&&!songMatches.length){
    dd.innerHTML='<div class="search-no-results">No results for "'+esc(q)+'"</div>';
    openSearchDropdown();
    return;
  }
  var html='';
  if(albumMatches.length){
    html+='<div class="search-group-label">Albums</div>';
    albumMatches.forEach(function(a){
      var art=a.user_artwork_path||a.artwork_path||'';
      var imgSrc=art?'/artwork/'+art.split('/').pop():'';
      html+='<div class="search-dropdown-item" onclick="searchPickAlbum('+a.id+')">';
      html+=imgSrc?'<img class="sdi-art" src="'+imgSrc+'" alt="">':'<div class="sdi-art-placeholder">&#127926;</div>';
      html+='<div class="sdi-info">';
      html+='<div class="sdi-title">'+esc(a.title)+'</div>';
      html+='<div class="sdi-sub">'+esc(a.artist)+(a.year?' ('+a.year+')':'')+'</div>';
      html+='</div>';
      html+='<span class="sdi-badge album">Album</span>';
      html+='</div>';
    });
  }
  if(songMatches.length){
    html+='<div class="search-group-label">Songs</div>';
    songMatches.forEach(function(t){
      var artPath=t.user_artwork_path||t.artwork_path;
      var artSrc=artPath?'/artwork/'+artPath.split('/').pop()+'?t='+t.album_id:'';
      var dur=t.duration_secs?fmtTime(t.duration_secs):'';
      html+='<div class="search-dropdown-item" onclick="searchPickAlbum('+t.album_id+')">';
      html+=artSrc?'<img class="sdi-art" src="'+artSrc+'" alt="">':'<div class="sdi-art-placeholder">&#9835;</div>';
      html+='<div class="sdi-info">';
      html+='<div class="sdi-title">'+esc(t.title)+'</div>';
      html+='<div class="sdi-sub">'+esc(t.artist)+' - '+esc(t.album_title)+(dur?' ('+dur+')':'')+'</div>';
      html+='</div>';
      html+='<span class="sdi-badge song">Song</span>';
      html+='</div>';
    });
  }
  dd.innerHTML=html;
  openSearchDropdown();
}
function searchPickAlbum(id){
  closeSearchDropdown();
  document.getElementById('catalog-search').value='';
  filterCatalog('');
  openAlbumDetail(id);
}
// Close dropdown when clicking outside or pressing Escape
document.addEventListener('click',function(e){
  var wrap=document.getElementById('search-wrap');
  if(wrap&&!wrap.contains(e.target)){closeSearchDropdown()}
});
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'&&_searchDropdownOpen){
    closeSearchDropdown();
    document.getElementById('catalog-search').blur();
  }
});

// ── Multi-Select Mode ──
function toggleMultiSelect(){
  _multiSelectMode=!_multiSelectMode;
  if(!_multiSelectMode){_selectedAlbums.clear()}
  updateMultiSelectBar();
  var q=(document.getElementById('catalog-search').value||'').trim();
  filterCatalog(q);
}

function cancelMultiSelect(){
  _multiSelectMode=false;
  _selectedAlbums.clear();
  updateMultiSelectBar();
  var q=(document.getElementById('catalog-search').value||'').trim();
  filterCatalog(q);
}

function toggleAlbumSelection(albumId){
  if(_selectedAlbums.has(albumId)){_selectedAlbums.delete(albumId)}else{_selectedAlbums.add(albumId)}
  updateMultiSelectBar();
  var q=(document.getElementById('catalog-search').value||'').trim();
  filterCatalog(q);
}

function updateMultiSelectBar(){
  const bar=document.getElementById('multi-select-bar');
  const info=document.getElementById('multi-select-info');
  const count=_selectedAlbums.size;
  if(_multiSelectMode&&count>0){bar.classList.add('visible');info.textContent=count+' selected'}else{bar.classList.remove('visible')}
  document.getElementById('btn-select').classList.toggle('has-filter',_multiSelectMode);
}

async function playSelectedAlbums(){
  if(_selectedAlbums.size===0){showError('No albums selected');return}
  if(!_isStreaming&&!_playerActive){_pendingAlbumId=Array.from(_selectedAlbums)[0];showOutputPicker();return}
  const albums=Array.from(_selectedAlbums).map(id=>_catalogAlbums.find(a=>a.id===id)).filter(Boolean);
  const queue=[];
  for(const a of albums){const sides=await apiFetch(`/api/catalog/${a.id}/tracks`).then(r=>r.json()).then(d=>{const s=new Map();(d.tracks||[]).forEach(t=>{if(!s.has(t.side||'A'))s.set(t.side||'A',[]);s.get(t.side||'A').push(t)});return Array.from(s.keys()).sort().map(side=>({album_id:a.id,album_title:a.title,album_artist:a.artist,side:side,tracks:s.get(side)}))});queue.push(...sides)}
  if(!_currentDevices){showError('No output device selected');return}
  await apiFetch('/api/player/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({queue:queue,devices:_currentDevices})}).catch(e=>showError('Playback failed'));
  showToast('Playing selected albums');
}

async function queueSelectedAlbums(){
  if(_selectedAlbums.size===0){showError('No albums selected');return}
  var ids=Array.from(_selectedAlbums);
  for(var i=0;i<ids.length;i++){await addToQueue(ids[i])}
  var n=ids.length;
  showToast(n+' album'+(n!==1?'s':'')+' added to queue');
  cancelMultiSelect();
}

async function favoriteSelectedAlbums(){
  if(_selectedAlbums.size===0){showError('No albums selected');return}
  for(const id of _selectedAlbums){await apiFetch(`/api/catalog/${id}/favorite`,{method:'POST'}).catch(()=>{})}
  loadCatalog();
  showToast(_selectedAlbums.size+' album'+((_selectedAlbums.size!==1)?'s':'')+' marked as favorite');
}

async function deleteSelectedAlbums(){
  if(_selectedAlbums.size===0){showError('No albums selected');return}
  if(!confirm('Delete '+_selectedAlbums.size+' album'+((_selectedAlbums.size!==1)?'s':'')+'?')){return}
  for(const id of _selectedAlbums){await apiFetch(`/api/catalog/${id}`,{method:'DELETE'}).catch(()=>{})}
  loadCatalog();
  showToast(_selectedAlbums.size+' album'+((_selectedAlbums.size!==1)?'s':'')+' deleted');
}

// ── Sort Panel ──
function toggleSortPanel(){
  var p=document.getElementById('sort-panel');
  var b=document.getElementById('sort-panel-backdrop');
  var open=p.classList.toggle('open');
  b.classList.toggle('open',open);
}
function closeSortPanel(){
  document.getElementById('sort-panel').classList.remove('open');
  document.getElementById('sort-panel-backdrop').classList.remove('open');
}
function updateFilterIndicator(){
  var btn=document.getElementById('btn-sort');
  if(_viewState.sortMode!=='artist'||_viewState.groupBy||_viewState.filterMode){btn.classList.add('has-filter')}else{btn.classList.remove('has-filter')}
}
function applySortMode(mode){
  _viewState.sortMode=mode;
  // Update active state on sort option buttons
  var opts=document.getElementById('sort-options').children;
  var labels={artist:0,title:1,year:2,recent_play:3,recent_add:4,favorites:5,rating:6};
  for(var i=0;i<opts.length;i++){opts[i].classList.toggle('active',i===labels[mode])}
  updateFilterIndicator();
  var q=(document.getElementById('catalog-search').value||'').trim();
  filterCatalog(q);
}

function toggleGroupBy(field){
  if(_viewState.groupBy===field){_viewState.groupBy=null}else{_viewState.groupBy=field}
  document.getElementById('grp-genre').classList.toggle('active',_viewState.groupBy==='genre');
  document.getElementById('grp-artist').classList.toggle('active',_viewState.groupBy==='artist');
  document.getElementById('grp-favorites').classList.toggle('active',_viewState.groupBy==='favorites');
  updateFilterIndicator();
  var q=(document.getElementById('catalog-search').value||'').trim();
  filterCatalog(q);
}

function toggleFilter(mode){
  if(_viewState.filterMode===mode){_viewState.filterMode=null}else{_viewState.filterMode=mode}
  document.getElementById('flt-not-recorded').classList.toggle('active',_viewState.filterMode==='not_recorded');
  document.getElementById('flt-recorded').classList.toggle('active',_viewState.filterMode==='recorded');
  updateFilterIndicator();
  var q=(document.getElementById('catalog-search').value||'').trim();
  filterCatalog(q);
}

function getFilteredAlbums(){
  var albums=_catalogAlbums;
  if(_activeShelfFilter&&_shelfData[_activeShelfFilter]){
    var shelfIds=new Set(_shelfData[_activeShelfFilter].map(function(a){return a.id}));
    albums=albums.filter(function(a){return shelfIds.has(a.id)});
  }
  if(_viewState.filterMode==='not_recorded'){albums=albums.filter(function(a){return !a.audio_count||a.audio_count===0})}
  else if(_viewState.filterMode==='recorded'){albums=albums.filter(function(a){return a.audio_count>0})}
  var q=(document.getElementById('catalog-search').value||'').trim();
  if(q){var ql=q.toLowerCase();albums=albums.filter(function(a){return (a.title||'').toLowerCase().includes(ql)||(a.artist||'').toLowerCase().includes(ql)||(a.genre||'').toLowerCase().includes(ql)||(a.label||'').toLowerCase().includes(ql)||(a.notes||'').toLowerCase().includes(ql)||String(a.year||'').includes(ql)})}
  return sortAlbums(albums);
}

function updateAlbumCount(){
  const count=_catalogAlbums.length;
  const recorded=_catalogAlbums.filter(a=>a.audio_count>0).length;
  const el=document.getElementById('album-count');
  if(el)el.textContent=`${count} album${count!==1?'s':''}${recorded>0?', '+recorded+' recorded':''}`;
}


function onAlbumTap(id,has){if(!has){openAlbumDetail(id);return}if(_isStreaming||_playerActive){playAlbum(id)}else{_pendingAlbumId=id;showOutputPicker(id)}}
async function playAlbum(id,trackId,resume){
  showToast('Starting playback\u2026');
  var body={album_id:id};
  if(trackId)body.track_id=trackId;
  if(resume){if(resume.position_secs>0)body.resume_position_secs=resume.position_secs;if(resume.side)body.resume_side=resume.side}
  // If current device is browser, create a fresh stream
  var isBrowser=false;
  if(_currentDevices&&_currentDevices.length>0&&_currentDevices[0].id&&_currentDevices[0].id.startsWith('browser:')){
    stopBrowserAudioStream();
    var sr=await createBrowserStream();
    if(sr.ok){_currentDevices[0].id='browser:'+sr.stream_id;isBrowser=true}
    else{showError(sr.error||'Failed to create browser stream');return}
  }
  if(_currentDevices)body.devices=_currentDevices;
  var r=await apiFetch('/api/player/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json()});
  if(!r.ok){showError(r.error||'Playback failed')}
  else if(isBrowser){startBrowserAudioStream(_currentDevices[0].id.replace('browser:',''))}
}
async function shufflePlay(){
  const p=_catalogAlbums.filter(a=>a.audio_count>0);
  if(!p.length){showToast('No recorded albums to shuffle');return}
  // Shuffle the array (Fisher-Yates)
  const shuffled=p.slice();
  for(var i=shuffled.length-1;i>0;i--){var j=Math.floor(Math.random()*(i+1));var tmp=shuffled[i];shuffled[i]=shuffled[j];shuffled[j]=tmp}
  var ids=shuffled.map(a=>a.id);
  if(_isStreaming||_playerActive){
    showToast('Shuffling '+ids.length+' albums...');
    playMultipleAlbums(ids);
  }else{
    _pendingAlbumIds=ids;_pendingAlbumId=ids[0];showOutputPicker(ids[0]);
  }
}

// Track-level shuffle: every track in the catalog, in random order. The
// server does the shuffle and builds single-track playlist entries; we
// just need to pick an output device (if not already streaming) and POST.
async function shuffleTracks(){
  const anyRecorded=(_catalogAlbums||[]).some(a=>a.audio_count>0);
  if(!anyRecorded){showToast('No recorded tracks to shuffle');return}
  if(_isStreaming||_playerActive){
    const body={devices:_currentDevices,volume:parseInt(document.getElementById('eq-volume').value)};
    const r=await apiFetch('/api/player/play-shuffle-tracks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json()});
    if(r.ok)showToast('Shuffling '+r.queued+' tracks...');
    else showError(r.error||'Shuffle failed');
  }else{
    // Defer until output picked. Reuse the existing pending-album-ids
    // hook by setting a sentinel; output picker calls back through
    // playMultipleAlbums otherwise, so we need a small branch there too.
    _pendingShuffleTracks=true;
    _pendingAlbumIds=null;
    _pendingAlbumId=null;
    showOutputPicker(null);
  }
}

async function toggleAlbumFavorite(id){
  try{
    const r=await apiFetch(`/api/catalog/${id}/favorite`,{method:'POST'}).then(r=>r.json());
    if(r.ok){
      const album=_catalogAlbums.find(a=>a.id===id);
      if(album){album.favorite=r.favorite?1:0}
      loadCatalog();
    }
  }catch(e){showError('Failed to toggle favorite')}
}

// ── Per-Group Play (multi-album queue) ──
var _pendingAlbumIds=null;
// Sentinel: when set, the next output-picker selection should kick off
// the track-level shuffle endpoint instead of normal album playback.
var _pendingShuffleTracks=false;
function playGroup(groupKey){
  var field=_viewState.groupBy;
  if(!field)return;
  var albums=getFilteredAlbums().filter(function(a){
    if(field==='favorites') return a.favorite&&a.audio_count>0;
    var val=(field==='genre'?(a.genre||'Unknown Genre'):(a.artist||'Unknown Artist'));
    return val===groupKey&&a.audio_count>0;
  });
  if(!albums.length){showToast('No recorded albums in this group');return}
  var ids=albums.map(function(a){return a.id});
  if(_isStreaming||_playerActive){
    playMultipleAlbums(ids);
  }else{
    _pendingAlbumIds=ids;
    _pendingAlbumId=ids[0];
    showOutputPicker(ids[0]);
  }
}
async function playMultipleAlbums(ids,devices,entries){
  var label=entries?entries.length+' side'+(entries.length!==1?'s':''):ids.length+' album'+(ids.length!==1?'s':'');
  showToast('Queueing '+label+'\u2026');
  var body={};
  if(entries)body.entries=entries;
  else body.album_ids=ids;
  var devs=devices||_currentDevices;
  // If target is browser, create a fresh stream
  var isBrowser=devs&&devs.length>0&&devs[0].id&&devs[0].id.startsWith('browser:');
  if(isBrowser){
    stopBrowserAudioStream();
    var sr=await createBrowserStream();
    if(sr.ok){devs[0].id='browser:'+sr.stream_id}
    else{showError(sr.error||'Failed to create browser stream');return}
  }
  if(devs)body.devices=devs;
  var r=await apiFetch('/api/player/play-queue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json()});
  if(!r.ok){showError(r.error||'Playback failed')}
  else if(isBrowser){startBrowserAudioStream(devs[0].id.replace('browser:',''))}
}

// ── Browser Audio Stream ("This Device") ──
// Plays the session's server-side MP3 stream through an <audio> media element.
// A media element (unlike Web Audio) keeps playing when Safari is backgrounded
// or the screen is locked on iOS, and drives the lock-screen controls (#54).
// Browsers block audio playback that is not tied to a user gesture. Because we
// only get the stream id after awaiting /api/stream/create + /api/player/play,
// the eventual el.play() is outside the click that started it and gets blocked
// (this was the "no sound on PC" bug). So we "unlock" the element on the user's
// first interaction by playing a silent clip once; after that, play() is
// allowed for the rest of the session.
var _browserAudioPrimed=false;
function primeBrowserAudio(){
  if(_browserAudioPrimed)return;
  var el=document.getElementById('browser-audio');
  if(!el)return;
  _browserAudioPrimed=true;
  if(el.getAttribute('src'))return;  // already playing a real stream
  el.muted=true;
  el.src='data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQIAAAAAAA==';
  var p=el.play();
  var done=function(){try{el.pause()}catch(e){}el.removeAttribute('src');el.muted=false};
  if(p&&p.then)p.then(done).catch(done); else done();
}
['pointerdown','touchend','click'].forEach(function(ev){
  document.addEventListener(ev,primeBrowserAudio,{capture:true,passive:true});
});

function stopBrowserAudioStream(){
  _browserStreamActive=false;
  stopBrowserStallWatch();
  var el=document.getElementById('browser-audio');
  if(el){el.onended=null;el.onerror=null;try{el.pause()}catch(e){}el.removeAttribute('src');try{el.load()}catch(e){}}
  stopKeepAlive();
}
// Create a browser stream, handing back the one this device was using so
// replacing your own stream never counts against the listener cap (#49).
async function createBrowserStream(){
  var body=_browserStreamId?{release:_browserStreamId}:{};
  var r=await apiFetch('/api/stream/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(res){return res.json()}).catch(function(){return{ok:false,error:'Network error'}});
  if(r.ok)_browserStreamId=r.stream_id;
  return r;
}
// Attach to whatever is already playing, instead of taking it over silently
// or being refused with "Already streaming" (#49). The user chooses between
// listening along and moving the session to this device (handover).
var _joinChoiceResolver=null;
function askJoinChoice(){
  return new Promise(function(resolve){
    _joinChoiceResolver=resolve;
    document.getElementById('join-choice-overlay').classList.add('open');
  });
}
function resolveJoinChoice(choice){
  document.getElementById('join-choice-overlay').classList.remove('open');
  var r=_joinChoiceResolver;_joinChoiceResolver=null;
  if(r)r(choice);
}
function closeJoinChoice(){resolveJoinChoice(null)}

async function joinThisDevice(mode){
  if(!mode){mode=await askJoinChoice();if(!mode)return false}
  var takeover=mode==='takeover';
  showToast(takeover?'Moving playback here…':'Joining…');
  stopBrowserAudioStream();
  var sr=await createBrowserStream();
  if(!sr.ok){showError(sr.error||'Failed to create browser stream');return false}
  var jr=await apiFetch('/api/stream/join',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stream_id:sr.stream_id,takeover:takeover})})
    .then(function(res){return res.json()}).catch(function(){return{ok:false,error:'Network error'}});
  if(!jr.ok){showError(jr.error||'Could not join');return false}
  startBrowserAudioStream(sr.stream_id);
  showToast(takeover?'Playing here now':'Listening along on this device');
  return true;
}
function somethingIsPlaying(){return !!(_isStreaming||_playerActive)}
// ── Live element health (issue #49) ──────────────────────────────────────────
// The live MP3 has no duration and cannot seek. When the element runs out of
// buffered audio (a second listener starting its encoder briefly starves the
// Pi, or the connection drops) the browser pauses it. Calling play() then does
// NOT resume live audio: it replays whatever is still buffered, so the same
// track repeats forever while other devices play on correctly. The cure is to
// reopen the stream rather than press play on a dead buffer.
var _browserStreamId=null;
var _browserStallTimer=null,_browserLastTime=-1,_browserStallTicks=0,_browserReconnects=0;
var _browserEverPlayed=false,_browserStartTicks=0;
var BROWSER_STALL_TICKS=3;      // ~6s of no progress once audio HAS been flowing
var BROWSER_MAX_RECONNECTS=5;   // give up rather than reconnect in a tight loop
// Starting up is not stalling. Nothing has been fed to this stream until the
// server has finished spinning up the player and attached it as a sink, which
// routinely takes longer than a stall window. Reconnecting during that gap
// yanks the source out from under a session that was about to start, which is
// exactly what caused "no sound until I pick This Device again" (#49).
var BROWSER_START_TICKS=10;     // ~20s of patience before the first audio

function reconnectBrowserAudio(reason){
  if(!_browserStreamActive||!_browserStreamId)return;
  if(_browserReconnects>=BROWSER_MAX_RECONNECTS){
    console.warn('[browser-audio] giving up after '+_browserReconnects+' reconnects');
    showToast('Audio on this device stopped. Pick This Device again to restart.');
    _browserStreamActive=false;stopBrowserStallWatch();return;
  }
  _browserReconnects++;
  console.warn('[browser-audio] reconnecting ('+reason+'), attempt '+_browserReconnects);
  var el=document.getElementById('browser-audio');
  if(!el)return;
  // A fresh query string forces a new connection instead of the browser
  // reusing the dead one. Same stream id, so the server-side sink (and its
  // place in the player) is untouched: only this listener reconnects.
  _browserLastTime=-1;_browserStallTicks=0;_browserStartTicks=0;
  el.src='/api/stream/'+_browserStreamId+'?r='+Date.now();
  var p=el.play();if(p&&p.catch)p.catch(function(){});
}

// Resume after a server-side pause. If the element is merely paused it plays
// on; if its buffer is spent, play() would loop the buffered range, so
// reconnect instead.
function resumeBrowserElement(el){
  var spent=(el.readyState<3)||(el.buffered.length>0&&el.currentTime>=el.buffered.end(el.buffered.length-1)-0.05);
  if(spent){reconnectBrowserAudio('buffer spent on resume');return}
  var p=el.play();if(p&&p.catch)p.catch(function(){});
}

function startBrowserStallWatch(){
  stopBrowserStallWatch();
  _browserLastTime=-1;_browserStallTicks=0;
  _browserEverPlayed=false;_browserStartTicks=0;
  _browserStallTimer=setInterval(function(){
    if(!_browserStreamActive)return;
    var el=document.getElementById('browser-audio');
    if(!el||!el.getAttribute('src'))return;
    if(el.paused)return;                 // a real pause is not a stall
    var t=el.currentTime;
    if(t>0)_browserEverPlayed=true;
    if(!_browserEverPlayed){
      // Still waiting for the very first audio. Sit tight: the server may
      // still be starting the player. Only after a long grace is it worth
      // assuming the stream is dead and reopening it once.
      if(++_browserStartTicks>=BROWSER_START_TICKS){
        _browserStartTicks=0;
        reconnectBrowserAudio('no audio after startup grace');
      }
      return;
    }
    if(_browserLastTime>=0&&t<=_browserLastTime){
      // Playing, but the clock is not advancing: the buffer has run dry
      if(++_browserStallTicks>=BROWSER_STALL_TICKS)reconnectBrowserAudio('stalled');
    }else{
      _browserStallTicks=0;
      _browserReconnects=0;              // healthy again, reset the budget
    }
    _browserLastTime=t;
  },2000);
}
function stopBrowserStallWatch(){if(_browserStallTimer){clearInterval(_browserStallTimer);_browserStallTimer=null}}

function startBrowserAudioStream(streamId){
  stopBrowserAudioStream();
  _browserStreamActive=true;
  _browserStreamId=streamId;
  _browserReconnects=0;
  var el=document.getElementById('browser-audio');
  if(!el){showError('Browser stream failed');return}
  el.muted=false; el.volume=1;
  el.src='/api/stream/'+streamId;
  el.onerror=function(){if(_browserStreamActive){console.warn('[browser-audio] element error');reconnectBrowserAudio('element error')}};
  // A live stream should never end. If it does, the connection dropped.
  el.onended=function(){if(_browserStreamActive)reconnectBrowserAudio('stream ended')};
  var attempts=0;
  function tryPlay(){
    var p=el.play();
    if(p&&p.catch)p.catch(function(e){
      // If autoplay was still blocked, prime and retry once
      if(attempts++<1){primeBrowserAudio();setTimeout(tryPlay,150);}
      else{console.warn('[browser-audio] play() rejected:',e);showToast('Tap the play button to start audio on this device');}
    });
  }
  tryPlay();
  startBrowserStallWatch();
  // Register the lock-screen transport now rather than waiting for the first
  // now-playing update: the screen may be locked before that arrives (#55).
  setupMediaSessionHandlers();
  markMediaSessionLive();
}

// ── Output Picker ──
var _outputDevices=[];
var _pickerAlbumId=null;
async function showOutputPicker(albumId){
  _pickerAlbumId=albumId;
  var overlay=document.getElementById('output-picker-overlay');
  var grid=document.getElementById('output-device-grid');
  var album=_catalogAlbums.find(function(a){return a.id===albumId});
  if(_pendingAlbumIds&&_pendingAlbumIds.length>1){
    document.getElementById('output-picker-title').innerHTML=_svgPlay12+' '+_pendingAlbumIds.length+' album'+(_pendingAlbumIds.length!==1?'s':'');
    document.getElementById('output-picker-subtitle').textContent='Choose where to play';
  }else if(album){
    document.getElementById('output-picker-title').textContent=album.artist+' \u2014 '+album.title;
    document.getElementById('output-picker-subtitle').textContent='Choose where to play';
  }
  grid.innerHTML='<div style="color:var(--muted);font-size:0.82rem">Loading devices\u2026</div>';
  overlay.classList.add('open');
  try{
    var d=await apiFetch('/api/devices').then(function(r){return r.json()});
    var allDevices=(d.devices||[]).filter(function(dev){return dev.paired!==false});
    if(!allDevices.filter(function(dev){return !dev.hidden}).length){
      grid.innerHTML='<div style="color:var(--muted);font-size:0.82rem">Scanning\u2026</div>';
      d=await apiFetch('/api/scan').then(function(r){return r.json()});
      allDevices=(d.devices||[]).filter(function(dev){return dev.paired!==false});
    }
    renderOutputCards(allDevices);
  }catch(e){grid.innerHTML='<div style="color:var(--rust);font-size:0.82rem">Failed: '+e.message+'</div>'}
}
function renderOutputCards(allDevices){
  var visible=allDevices.filter(function(d){return !d.hidden});
  var hidden=allDevices.filter(function(d){return d.hidden});
  _outputDevices=visible;
  var grid=document.getElementById('output-device-grid');
  if(!visible.length&&!hidden.length){grid.innerHTML='<div style="color:var(--muted);font-size:0.82rem">No devices found. <span style="color:var(--amber-dk);cursor:pointer;text-decoration:underline" onclick="rescanOutputDevices()">Rescan</span></div>';return}
  var html=visible.map(function(dev,idx){
    var name=dev.custom_name||dev.name;
    var orig=dev.custom_name?dev.name:null;
    var icon=guessDeviceIcon(dev.name,dev.type);
    var last=_lastUsedDevice&&_lastUsedDevice===dev.id;
    var isLocal=dev.type==='local';
    var isBT=dev.type==='bluetooth';
    var isBrowser=dev.type==='browser';
    var typeBadge=isBrowser?'<div class="device-type-badge browser">This Device</div>'
      :isLocal?'<div class="device-type-badge local">Local</div>'
      :isBT?'<div class="device-type-badge bt">Bluetooth</div>'
      :'<div class="device-type-badge airplay">AirPlay</div>';
    return '<div class="output-device-card'+(last?' last-used':'')+'" onclick="pickOutput('+idx+')" data-device-id="'+dev.id+'">'
      +'<div class="output-device-icon">'+icon+'</div>'
      +'<div class="output-device-name">'+esc(name)+'</div>'
      +(orig?'<div class="output-device-orig">'+esc(orig)+'</div>':'')
      +typeBadge
      +'<div class="output-device-rename" onclick="event.stopPropagation();renameDevice(\''+dev.id+'\',\''+esc(name).replace(/'/g,"\\'")+'\')">rename</div>'
      +'<div class="output-device-rename" onclick="event.stopPropagation();hideOutputDevice(\''+dev.id+'\',\''+esc(name).replace(/'/g,"\\'")+'\')">hide</div>'
      +'</div>';
  }).join('');
  html+='<div style="text-align:center;grid-column:1/-1"><span style="color:var(--amber-dk);cursor:pointer;font-size:0.72rem;text-decoration:underline" onclick="rescanOutputDevices()">Rescan for devices</span></div>';
  if(hidden.length){
    html+='<div style="grid-column:1/-1;margin-top:0.4rem;border-top:1px solid var(--paper-dk);padding-top:0.4rem">'
      +'<div style="font-size:0.7rem;color:var(--muted);cursor:pointer" onclick="var el=this.nextElementSibling;var open=el.style.display!==\'none\';el.style.display=open?\'none\':\'block\';this.textContent=open?\'Hidden devices (\'+'+hidden.length+'+\') ▸\':\'Hidden devices ▾\'">'
      +'Hidden devices ('+hidden.length+') ▸</div>'
      +'<div style="display:none">'
      +hidden.map(function(dev){
        var name=dev.custom_name||dev.name;
        return '<div style="display:flex;justify-content:space-between;align-items:center;padding:0.15rem 0">'
          +'<span style="font-size:0.75rem;color:var(--muted)">'+esc(name)+'</span>'
          +'<span style="font-size:0.6rem;color:var(--amber-dk);cursor:pointer;text-decoration:underline" onclick="unhideDevice(\''+dev.id+'\')">show</span>'
          +'</div>';
      }).join('')
      +'</div></div>';
  }
  grid.innerHTML=html;
}
function guessDeviceIcon(name,type){
  var n=(name||'').toLowerCase();
  if(type==='browser')return'\uD83D\uDD0A';
  if(type==='bluetooth')return'\u{1F4FB}';
  if(n.includes('homepod'))return'\uD83C\uDFDB\uFE0F';
  if(n.includes('tv')||n.includes('apple tv'))return'\uD83D\uDCFA';
  if(n.includes('mac')||n.includes('book'))return'\uD83D\uDCBB';
  if(n.includes('iphone')||n.includes('phone'))return'\uD83D\uDCF1';
  if(n.includes('sonos'))return'\uD83D\uDD0A';
  if(n.includes('hdmi'))return'\uD83D\uDCFA';
  if(n.includes('touchscreen'))return'\uD83D\uDD0A';
  if(n.includes('headphone')||n.includes('scarlett'))return'\uD83C\uDFA7';
  return'\uD83D\uDD0A';
}
async function pickOutput(idx){
  var dev=_outputDevices[idx];
  if(!dev)return;
  var aid=_pendingAlbumId;
  var aids=_pendingAlbumIds;
  var trackId=_pendingTrackId;
  var shuffleTracks=_pendingShuffleTracks;
  closeOutputPicker();
  _lastUsedDevice=dev.id;
  var displayName=dev.custom_name||dev.name;
  showToast('Connecting to '+displayName+'\u2026');
  var target={id:dev.id,name:dev.name};
  if(dev.type==='local'){if(dev.alsa_device)target.alsa_device=dev.alsa_device}
  if(dev.type==='bluetooth'){target.address=dev.address||dev.id.replace('bt:','')}
  if(dev.type==='browser'){
    (async function(){
      // Audio is already going somewhere: listen along rather than taking it
      // over, which used to cut off whoever was already listening (#49).
      if(somethingIsPlaying()&&!_browserStreamActive){await joinThisDevice();return}
      var streamResp=await createBrowserStream();
      if(streamResp.ok){
        target.id='browser:'+streamResp.stream_id;
        startPlayback(aid,aids,target,trackId,shuffleTracks);
      }else{
        showError(streamResp.error||'Failed to create browser stream')
      }
    })();
    return
  }
  startPlayback(aid,aids,target,trackId,shuffleTracks);
  if(window._deviceVolumes&&window._deviceVolumes[dev.id]){setTimeout(()=>{document.getElementById('eq-volume').value=window._deviceVolumes[dev.id];sendVolume()},500)}
}
function startPlayback(aid,aids,target,trackId,shuffleTracks){
  _currentDevices=[target];
  apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({saved_devices:[target]})});
  if(shuffleTracks){
    var vol=parseInt(document.getElementById('eq-volume').value);
    apiFetch('/api/player/play-shuffle-tracks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({devices:[target],volume:vol})}).then(function(r){return r.json()}).then(function(r){
      if(r.ok)showToast('Shuffling '+r.queued+' tracks...');
      else showError(r.error||'Shuffle failed');
    });
    return;
  }
  if(_pendingPlaylistEntries){var ent=_pendingPlaylistEntries;_pendingPlaylistEntries=null;playMultipleAlbums(null,[target],ent)}
  else if(aids&&aids.length>1){_pendingAlbumIds=null;playMultipleAlbums(aids,[target])}
  else if(aid){
    var resume=_resumeInfo;_resumeInfo=null;
    if(trackId){if(!resume)resume={};resume.track_id=trackId;_pendingTrackId=null}
    playAlbumTo(aid,[target],resume,target.id.startsWith('browser:'));
  }
}
async function playAlbumTo(id,devices,resume,isBrowser){
  showToast('Starting playback\u2026');
  var body={album_id:id};
  if(devices)body.devices=devices;
  if(resume){if(resume.track_id)body.track_id=resume.track_id;if(resume.position_secs)body.resume_position_secs=resume.position_secs;if(resume.side)body.resume_side=resume.side}
  var r=await apiFetch('/api/player/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(function(r){return r.json()});
  if(!r.ok){
    showError(r.error||'Playback failed');
  }else if(isBrowser&&devices&&devices.length>0){
    var streamId=devices[0].id.replace('browser:','');
    startBrowserAudioStream(streamId);
  }
}
function closeOutputPicker(){document.getElementById('output-picker-overlay').classList.remove('open');_pendingAlbumId=null;_pendingAlbumIds=null;_pendingTrackId=null;_pickerAlbumId=null;_pendingPlaylistEntries=null;_pendingShuffleTracks=false}
async function rescanOutputDevices(){
  var grid=document.getElementById('output-device-grid');
  grid.innerHTML='<div style="color:var(--muted);font-size:0.82rem">Scanning\u2026</div>';
  try{var d=await apiFetch('/api/scan').then(function(r){return r.json()});renderOutputCards((d.devices||[]).filter(function(dev){return !dev.hidden&&dev.paired!==false}))}
  catch(e){grid.innerHTML='<div style="color:var(--rust);font-size:0.82rem">Scan failed</div>'}
}
async function hideOutputDevice(deviceId,name){
  await apiFetch('/api/devices/'+deviceId+'/hide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hidden:true})});
  showToast('Hidden: '+name);
  if(_pickerAlbumId)showOutputPicker(_pickerAlbumId);
}
async function unhideDevice(deviceId){
  await apiFetch('/api/devices/'+deviceId+'/hide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hidden:false})});
  showToast('Device restored');
  if(_pickerAlbumId)showOutputPicker(_pickerAlbumId);
}
var _renameDeviceId=null;
async function renameDevice(deviceId,currentName){
  _renameDeviceId=deviceId;
  var inp=document.getElementById('rename-input');
  inp.value=currentName||'';
  inp.style.display='block';
  inp.focus();
}
function onRenameDone(){
  var inp=document.getElementById('rename-input');
  var newName=inp.value.trim();
  inp.style.display='none';
  if(_renameDeviceId===null)return;
  var did=_renameDeviceId;_renameDeviceId=null;
  apiFetch('/api/devices/'+did+'/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:newName})}).then(function(){
    showToast(newName?'Renamed to '+newName:'Name reset');
    if(_pickerAlbumId)showOutputPicker(_pickerAlbumId);
  });
}

// Media Session + keep-alive for mobile background playback
var _keepAliveAudio=null;
var _mediaSessionHandlersSet=false;
function setupMediaSessionHandlers(){
  if(_mediaSessionHandlersSet||!('mediaSession' in navigator))return;
  // Wire the lock-screen / hardware media keys to the server-side transport so
  // every output (This Device, AirPlay, local) stays in sync (issue #54).
  var set=function(a,fn){try{navigator.mediaSession.setActionHandler(a,fn)}catch(e){}};
  set('play',function(){playerToggle()});
  set('pause',function(){playerToggle()});
  set('previoustrack',function(){playerPrev()});
  set('nexttrack',function(){playerNext()});
  set('stop',function(){playerStop()});
  _mediaSessionHandlersSet=true;
}
function updateMediaSession(title,artist,album,artUrl){
  if(!('mediaSession' in navigator))return;
  setupMediaSessionHandlers();
  navigator.mediaSession.metadata=new MediaMetadata({
    title:title||'Vinyl Streamer',
    artist:artist||'',
    album:album||'',
    artwork:artUrl?[{src:artUrl,sizes:'300x300',type:'image/jpeg'},{src:artUrl,sizes:'512x512',type:'image/jpeg'}]:[]
  });
  navigator.mediaSession.playbackState='playing';
  markMediaSessionLive();
}
// This Device is one continuous, non-seekable stream. Left undeclared, iOS
// assumes the element is a normal seekable track and turns the lock-screen
// skip buttons into seek, so they never reach the next track (issue #55).
// Declaring an infinite duration marks it as live, which is the signal for a
// skip-button transport rather than a scrubber.
function markMediaSessionLive(){
  if(!('mediaSession' in navigator)||!navigator.mediaSession.setPositionState)return;
  if(!_browserStreamActive)return;
  try{navigator.mediaSession.setPositionState({duration:Infinity})}
  catch(e){try{navigator.mediaSession.setPositionState(null)}catch(e2){}}
}
function startKeepAlive(){
  // When This Device is the output, the real <audio> element already holds the
  // media session and keeps the tab alive, so skip the silent keep-alive to
  // avoid two elements competing for audio focus on iOS.
  if(_browserStreamActive)return;
  if(_keepAliveAudio)return;
  // Silent looping audio element keeps the browser tab alive on mobile
  _keepAliveAudio=document.createElement('audio');
  _keepAliveAudio.loop=true;
  // Tiny silent WAV (44 bytes header + 2 bytes of silence)
  _keepAliveAudio.src='data:audio/wav;base64,UklGRiYAAABXQVZFZm10IBAAAAABAAEAQB8AAIA+AAACABAAZGF0YQIAAAAAAA==';
  _keepAliveAudio.volume=0.01;
  _keepAliveAudio.play().catch(function(){});
}
function stopKeepAlive(){
  if(_keepAliveAudio){_keepAliveAudio.pause();_keepAliveAudio=null}
  if('mediaSession' in navigator)navigator.mediaSession.playbackState='none';
}

function renderNowPlaying(d){
  if(typeof ssOnNowPlaying==='function')ssOnNowPlaying(d);
  if(!d.track_title){if(!_playerActive)hideNPFooter();stopKeepAlive();return}
  if(d.album_id)_playerAlbumId=d.album_id;
  showNPFooter();_playerSource=d.source||'vinyl';
  // Keep mobile browser alive and show lock screen controls
  startKeepAlive();
  updateMediaSession(d.track_title,d.track_artist||d.album_artist,d.album_title,d.artwork_url);
  const w=document.getElementById('np-art-wrap');
  if(d.artwork_url)w.outerHTML=`<img class="np-art" id="np-art-wrap" src="${d.artwork_url}?t=${Date.now()}" alt="" onclick="if(_playerAlbumId){event.stopPropagation();openAlbumDetail(_playerAlbumId)}">`;
  else w.outerHTML='<div class="np-art-placeholder" id="np-art-wrap" style="cursor:pointer" onclick="if(_playerAlbumId){event.stopPropagation();openAlbumDetail(_playerAlbumId)}">\uD83D\uDCBF</div>';
  document.getElementById('np-title').innerHTML=esc(d.track_title)+(d.source==='player'?'<span class="np-source-badge">catalog</span>':'');
  document.getElementById('np-artist-album').textContent=[d.track_artist||d.album_artist||'',d.album_title||''].filter(Boolean).join(' \u2014 ')+(d.year?' ('+d.year+')':'');
  document.getElementById('np-transport').style.display=d.source==='player'?'flex':'none';
  document.getElementById('np-progress-row').style.display=d.source==='player'?'flex':'none';
  // Update hero
  const ha=document.getElementById('np-hero-art');
  if(d.artwork_url)ha.innerHTML=`<img src="${d.artwork_url}?t=${Date.now()}" alt="">`;
  else ha.innerHTML='<div class="np-hero-art-placeholder"></div>';
  document.getElementById('np-hero-track').textContent=d.track_title||'';
  document.getElementById('np-hero-artist').textContent=d.track_artist||d.album_artist||'';
  const albumLine=[d.album_title||'',d.year?'('+d.year+')':''].filter(Boolean).join(' ');
  document.getElementById('np-hero-album').textContent=albumLine;
  const src=document.getElementById('np-hero-source');
  src.textContent=d.source==='player'?'catalog':'vinyl';
  src.className='np-hero-source '+(d.source==='player'?'catalog':'vinyl');
}

var _lastPositionSave=0;
var _npDurationSecs=0; // current side duration from player status, used by playerSeekClick
function onPlayerStatus(d){
  if(typeof ssOnPlayerStatus==='function')ssOnPlayerStatus(d);
  if(d.state==='stopped'){_playerActive=false;_playerAlbumId=null;document.getElementById('np-transport').style.display='none';document.getElementById('np-progress-row').style.display='none';updateRepeatButton('off');if(_playerSource==='player')hideNPFooter();stopKeepAlive();closeNowPlayingFullscreen();updateQueueFab();return}
  _playerActive=true;_playerSource='player';showNPFooter();updateQueueFab();
  document.getElementById('np-transport').style.display='flex';document.getElementById('np-progress-row').style.display='flex';
  document.getElementById('np-play-pause').innerHTML=d.state==='paused'?'<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21"/></svg>':'<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="5" y="3" width="4" height="18" rx="1"/><rect x="15" y="3" width="4" height="18" rx="1"/></svg>';
  updateVinylSpin(d.state);
  // Keep the lock-screen play/pause state in sync with the server player
  if('mediaSession' in navigator)navigator.mediaSession.playbackState=(d.state==='paused')?'paused':'playing';
  // If This Device is the sink, mirror server pause/resume onto the element
  if(_browserStreamActive){var _bel=document.getElementById('browser-audio');if(_bel&&_bel.getAttribute('src')){if(d.state==='paused'&&!_bel.paused){try{_bel.pause()}catch(e){}}else if(d.state==='playing'&&_bel.paused){resumeBrowserElement(_bel)}}}
  const pos=d.position_secs||0,dur=d.duration_secs||1;_npDurationSecs=dur;
  document.getElementById('np-progress-fill').style.width=Math.min(100,(pos/dur)*100)+'%';
  document.getElementById('np-time-cur').textContent=fmtTime(pos);var rem=Math.max(0,dur-pos);document.getElementById('np-time-dur').textContent='-'+fmtTime(rem);
  if(d.side_index!==undefined&&d.side_count>1){var sideEl=document.getElementById('np-side-count');if(sideEl)sideEl.textContent='Side '+(d.side_index+1)+' of '+d.side_count}
  if(d.repeat_mode!==undefined)updateRepeatButton(d.repeat_mode);
  if(_playerAlbumId&&d.state==='playing'&&Date.now()-_lastPositionSave>10000){
    _lastPositionSave=Date.now();
    // Save the side LABEL ("A"), not the playlist index: an index silently
    // points at the wrong side if a side is later added or re-recorded (#74).
    const side=d.side!==undefined&&d.side!==null?String(d.side):null;
    apiFetch('/api/catalog/'+_playerAlbumId+'/position',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({side:side,secs:pos})}).catch(()=>{});
  }
}

async function playerToggle(){await apiFetch('/api/player/pause',{method:'POST'})}
async function playerStop(){
  stopBrowserAudioStream();
  closeNowPlayingFullscreen();
  await apiFetch('/api/player/stop',{method:'POST'})
}
var _resumeInfo=null;
function switchOutput(){var aid=_playerAlbumId;if(!aid)return;_resumeInfo=null;apiFetch('/api/player/status').then(function(r){return r.json()}).then(function(s){if(s.track_id||s.position_secs)_resumeInfo={track_id:s.track_id,position_secs:s.position_secs};_pendingAlbumId=aid;showOutputPicker(aid)})}
async function playerNext(){await apiFetch('/api/player/next',{method:'POST'})}
async function playerPrev(){await apiFetch('/api/player/prev',{method:'POST'})}

var _swipeStartX=0,_swipeStartY=0,_swipeIgnore=false;
// Controls inside the footer that own their own horizontal drag. A gesture
// starting on one of these is never a prev/next swipe: dragging an EQ, volume,
// or seek control used to bubble up here and skip a track (issue #44), which
// read as the sliders moving the seek bar.
var SWIPE_IGNORE_SEL='input,button,select,textarea,a,label,.np-progress-bar,.np-progress-row,.eq-row';
function initSwipeGestures(){
  const npBar=document.getElementById('np-footer');
  if(!npBar)return;
  npBar.addEventListener('pointerdown',e=>{
    const t=e.target;
    _swipeIgnore=!!(t&&t.closest&&t.closest(SWIPE_IGNORE_SEL));
    _swipeStartX=e.clientX;_swipeStartY=e.clientY;
  },{passive:true});
  npBar.addEventListener('pointerup',e=>{
    if(_swipeIgnore){_swipeIgnore=false;return}
    const dx=e.clientX-_swipeStartX;
    const dy=e.clientY-_swipeStartY;
    const adx=Math.abs(dx);
    const ady=Math.abs(dy);
    if(adx>50&&adx>ady){if(dx>0){playerPrev()}else{playerNext()}}
  },{passive:true});
  // A drag that leaves the footer (finger sliding past the edge) never fires
  // pointerup here, so clear the flag on cancel too.
  npBar.addEventListener('pointercancel',()=>{_swipeIgnore=false},{passive:true});
}
async function playerRepeat(){const r=await apiFetch('/api/player/repeat',{method:'POST'}).then(r=>r.json());if(r.ok)updateRepeatButton(r.repeat_mode)}
function updateRepeatButton(mode){var btn=document.getElementById('np-repeat');if(!btn)return;var repeatSvg='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>';var repeatOneSvg='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/><text x="12" y="15" text-anchor="middle" fill="currentColor" stroke="none" font-size="8" font-weight="bold">1</text></svg>';if(mode==='album'){btn.style.opacity='1';btn.innerHTML=repeatSvg;btn.title='Repeat album'}else if(mode==='track'){btn.style.opacity='1';btn.innerHTML=repeatOneSvg;btn.title='Repeat track'}else{btn.style.opacity='0.4';btn.innerHTML=repeatSvg;btn.title='Repeat off'}}
function updateVinylSpin(state){var w=document.getElementById('np-art-wrap');if(!w)return;var img=w.querySelector('img');var el=img||w;if(state==='playing'){el.classList.add('vinyl-spinning');el.classList.remove('paused','vinyl-recording')}else if(state==='paused'){el.classList.add('vinyl-spinning','paused');el.classList.remove('vinyl-recording')}else{el.classList.remove('vinyl-spinning','paused','vinyl-recording')}}
var _recStripInterval=null;
function setRecordingIndicator(active){
  var w=document.getElementById('np-art-wrap');if(w){var img=w.querySelector('img');var el=img||w;if(active){el.classList.add('vinyl-recording');el.classList.remove('vinyl-spinning','paused')}else{el.classList.remove('vinyl-recording')}}
  var strip=document.getElementById('rec-strip');if(!strip)return;
  if(active){
    strip.classList.add('visible');
    var album=_recAlbumId?_catalogAlbums.find(function(a){return a.id===_recAlbumId}):null;
    document.getElementById('rec-strip-info').textContent=album?(album.title+' -- '+album.artist):'Recording...';
    document.getElementById('rec-strip-side').textContent='Side '+(_recSide||'A');
    clearInterval(_recStripInterval);
    _recStripInterval=setInterval(function(){
      if(!_recStartTime){clearInterval(_recStripInterval);return}
      var s=Math.floor((Date.now()-_recStartTime)/1000);
      document.getElementById('rec-strip-timer').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
    },1000);
  }else{
    strip.classList.remove('visible');
    clearInterval(_recStripInterval);_recStripInterval=null;
  }
}
function jumpToRecording(){if(_recAlbumId){openAlbumDetail(_recAlbumId)}}
function playerSeekClick(e){if(!_npDurationSecs)return;const b=e.currentTarget,r=b.getBoundingClientRect(),pct=Math.max(0,Math.min(1,(e.clientX-r.left)/r.width));apiFetch('/api/player/seek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position_secs:pct*_npDurationSecs})})}

var _queuePanelOpen=false;
function toggleQueue(){var p=document.getElementById('queue-panel');_queuePanelOpen=!_queuePanelOpen;p.classList.toggle('open',_queuePanelOpen);document.getElementById('queue-backdrop').style.display=_queuePanelOpen?'block':'none';if(_queuePanelOpen){refreshQueueDisplay();document.addEventListener('click',closeQueueOnClickOutside,true)}else{closeNowPlayingFullscreen();document.removeEventListener('click',closeQueueOnClickOutside,true)}}
function closeQueueOnClickOutside(e){var p=document.getElementById('queue-panel');if(!p.contains(e.target)&&e.target.id!=='np-repeat'&&!e.target.closest('[onclick*="toggleQueue"]')){_queuePanelOpen=false;p.classList.remove('open');document.getElementById('queue-backdrop').style.display='none';closeNowPlayingFullscreen();document.removeEventListener('click',closeQueueOnClickOutside,true)}}
async function refreshQueueDisplay(){
  if(!_queuePanelOpen)return;
  var list=document.getElementById('queue-list');
  var actions=document.getElementById('queue-actions');
  // If player is active, show live queue
  if(_playerActive){
    if(actions)actions.innerHTML='<button class="btn btn-ghost" onclick="saveQueueAsPlaylist()" style="font-size:0.72rem">Save as Playlist</button><button class="btn btn-ghost" onclick="clearQueuePrompt()" style="font-size:0.72rem;color:var(--rust)">Clear</button>';
    try{const r=await apiFetch('/api/player/queue').then(d=>d.json());if(!r.ok||!r.queue)return;if(!r.queue.length){list.innerHTML='<div class="queue-empty">Queue is empty</div>';return}
    var chevronSvg='<svg class="queue-chevron" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="8 4 18 12 8 20"/></svg>';
    let html='';r.queue.forEach((item,idx)=>{
      const isCurrent=idx===r.current_index;
      let art='<div class="queue-art" style="display:flex;align-items:center;justify-content:center;font-size:1.5rem">\uD83D\uDCBF</div>';
      if(item.artwork){const artFile=item.artwork.split('/').pop();art=`<img src="/artwork/${artFile}?t=${item.album_id}" class="queue-art" alt="">`}
      const removeBtn=isCurrent?'':'<button class="queue-remove-btn" onclick="event.stopPropagation();removeFromQueue('+idx+')" title="Remove">\u2715</button>';
      const dragHandle=isCurrent?'<div style="width:20px"></div>':'<div class="queue-drag-handle" data-idx="'+idx+'" style="cursor:grab;font-size:1.1rem;color:var(--muted);padding:0 0.15rem">\u2261</div>';
      const expanded=isCurrent?' expanded':'';
      html+=`<div class="queue-item${isCurrent?' current':''}${expanded}" data-queue-idx="${idx}" onclick="toggleQueueExpand(this)">${dragHandle}${chevronSvg}${art}<div class="queue-info"><div class="queue-album-title">${esc(item.album_title)}</div><div class="queue-album-artist">${esc(item.album_artist)}</div><div class="queue-side">Side ${esc(item.side)}</div></div>${removeBtn}</div>`;
      // Track list accordion
      var tracks=item.tracks||[];
      html+='<div class="queue-tracks'+(isCurrent?' open':'')+'">';
      if(tracks.length){tracks.forEach((t,ti)=>{
        var isActive=isCurrent&&ti===r.current_track_idx;
        var dur=t.duration_secs!=null?fmtTime(t.duration_secs):'';
        var clickAttr=t.id?' onclick="event.stopPropagation();seekToQueueTrack('+t.id+')"':'';
        html+='<div class="queue-track-row'+(isActive?' qt-active':'')+'"'+clickAttr+'><span class="qt-num">'+(t.track_number||ti+1)+'</span><span class="qt-title">'+esc(t.title)+'</span><span class="qt-dur">'+dur+'</span></div>';
      })}else{html+='<div class="queue-track-row"><span class="qt-title" style="color:var(--muted)">No track info</span></div>'}
      html+='</div>';
    });
    list.innerHTML=html;initQueueDrag()}catch(e){console.warn('Queue refresh error:',e)}
    return;
  }
  // Otherwise show pending queue
  if(!_pendingQueue.length){
    list.innerHTML='<div class="queue-empty">Queue is empty. Select albums and add them here.</div>';
    if(actions)actions.innerHTML='';
    return;
  }
  if(actions)actions.innerHTML='<button class="btn btn-accent" onclick="playPendingQueue()" style="font-size:0.75rem">Play Queue</button><button class="btn btn-ghost" onclick="clearPendingQueue()" style="font-size:0.72rem;color:var(--rust)">Clear</button>';
  var html='';
  _pendingQueue.forEach(function(item,idx){
    var artSrc=item.artwork?'/artwork/'+item.artwork.split('/').pop()+'?t='+item.id:'';
    var art=artSrc?'<img src="'+artSrc+'" class="queue-art" alt="">':'<div class="queue-art" style="display:flex;align-items:center;justify-content:center;font-size:1.5rem">\uD83D\uDCBF</div>';
    html+='<div class="queue-item"><div style="width:20px"></div>'+art+'<div class="queue-info"><div class="queue-album-title">'+esc(item.title)+'</div><div class="queue-album-artist">'+esc(item.artist)+'</div></div><button class="queue-remove-btn" onclick="event.stopPropagation();removeFromPendingQueue('+idx+')" title="Remove">\u2715</button></div>';
  });
  list.innerHTML=html;
}
function toggleQueueExpand(el){
  var wasExpanded=el.classList.contains('expanded');
  el.classList.toggle('expanded',!wasExpanded);
  var tracks=el.nextElementSibling;
  if(tracks&&tracks.classList.contains('queue-tracks')){tracks.classList.toggle('open',!wasExpanded)}
}
async function seekToQueueTrack(trackId){
  try{await apiFetch('/api/player/seek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track_id:trackId})});setTimeout(refreshQueueDisplay,300)}catch(e){showToast('Failed to seek')}
}
async function removeFromQueue(idx){try{const r=await apiFetch('/api/player/queue/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:idx})}).then(d=>d.json());if(r.ok){refreshQueueDisplay()}else{showToast(r.error||'Cannot remove')}}catch(e){showToast('Failed to remove')}}
async function reorderQueue(fromIdx,toIdx){try{const r=await apiFetch('/api/player/queue/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:fromIdx,to:toIdx})}).then(d=>d.json());if(r.ok){refreshQueueDisplay()}else{showToast(r.error||'Cannot reorder')}}catch(e){showToast('Failed to reorder')}}
function initQueueDrag(){const list=document.getElementById('queue-list');let dragIdx=null;let dragEl=null;let startY=0;let items=[];function getItemAtY(y){for(const it of items){const r=it.el.getBoundingClientRect();if(y>=r.top&&y<=r.bottom)return it}return null}list.querySelectorAll('.queue-drag-handle').forEach(handle=>{handle.addEventListener('pointerdown',function(e){e.preventDefault();dragIdx=parseInt(this.dataset.idx);dragEl=this.closest('.queue-item');dragEl.classList.add('dragging');startY=e.clientY;items=[];list.querySelectorAll('.queue-item').forEach(el=>{const i=parseInt(el.dataset.queueIdx);items.push({el:el,idx:i})});document.addEventListener('pointermove',onMove);document.addEventListener('pointerup',onUp)})});function onMove(e){e.preventDefault();items.forEach(it=>it.el.classList.remove('drag-over'));const target=getItemAtY(e.clientY);if(target&&target.idx!==dragIdx){target.el.classList.add('drag-over')}}function onUp(e){document.removeEventListener('pointermove',onMove);document.removeEventListener('pointerup',onUp);if(dragEl)dragEl.classList.remove('dragging');items.forEach(it=>it.el.classList.remove('drag-over'));const target=getItemAtY(e.clientY);if(target&&target.idx!==dragIdx&&dragIdx!==null){reorderQueue(dragIdx,target.idx)}dragIdx=null;dragEl=null}}
async function addToQueue(albumId){
  if(_playerActive){
    try{const r=await apiFetch('/api/player/queue/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:albumId})}).then(d=>d.json());if(r.ok){showToast(r.message||'Added to queue');refreshQueueDisplay()}else showError(r.error||'Failed to add to queue')}catch(e){showError('Failed to add to queue')}
  }else{
    addToPendingQueue(albumId);
  }
}
async function addToQueueFromModal(){if(!currentAlbumId)return;await addToQueue(currentAlbumId);closeModal()}
function addToPendingQueue(albumId){
  var a=_catalogAlbums.find(function(x){return x.id===albumId});
  if(!a)return;
  if(_pendingQueue.some(function(q){return q.id===albumId}))return;
  _pendingQueue.push({id:a.id,title:a.title,artist:a.artist,artwork:a.user_artwork_path||a.artwork_path});
  showToast('Added "'+a.title+'" to queue');
  updateQueueFab();
}
function removeFromPendingQueue(idx){
  _pendingQueue.splice(idx,1);
  updateQueueFab();
  refreshQueueDisplay();
}
function clearPendingQueue(){
  _pendingQueue=[];
  updateQueueFab();
  refreshQueueDisplay();
}
function playPendingQueue(){
  if(!_pendingQueue.length){showToast('Queue is empty');return}
  var ids=_pendingQueue.map(function(q){return q.id});
  _pendingQueue=[];
  updateQueueFab();
  toggleQueue();
  if(_isStreaming||_playerActive){
    playMultipleAlbums(ids);
  }else{
    _pendingAlbumIds=ids;
    _pendingAlbumId=ids[0];
    showOutputPicker(ids[0]);
  }
}
function updateQueueFab(){
  var fab=document.getElementById('queue-fab');
  var badge=document.getElementById('queue-fab-badge');
  var count=_pendingQueue.length;
  // Show FAB when there are pending items and player isn't active
  // Also show when player is active (as a quick way to open queue)
  if(count>0&&!_playerActive){
    fab.style.display='flex';
    badge.textContent=count;
  }else if(_playerActive){
    // Hide when player is active (queue accessible from now-playing bar)
    fab.style.display='none';
  }else{
    fab.style.display='none';
  }
}
async function clearQueuePrompt(){if(!confirm('Clear queue? Playback will stop after the current album.'))return;clearQueue()}
async function clearQueue(){try{const r=await apiFetch('/api/player/queue/clear',{method:'POST'}).then(d=>d.json());if(r.ok){showToast('Queue cleared');refreshQueueDisplay()}else showError(r.error||'Failed to clear queue')}catch(e){showError('Failed to clear queue')}}

async function saveQueueAsPlaylist(){
  var r=await apiFetch('/api/player/queue').then(d=>d.json());
  if(!r.ok||!r.queue||!r.queue.length){showToast('Queue is empty');return}
  var name=prompt('Playlist name:');
  if(!name||!name.trim())return;
  // Save as side-level entries preserving queue order
  var entries=r.queue.map(function(item){return {a:item.album_id,s:item.side}});
  var res=await apiFetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim(),entries:entries})}).then(d=>d.json());
  if(res.ok)showToast('Playlist "'+name.trim()+'" saved');
  else showToast(res.error||'Failed to save');
}
async function togglePlaylistPicker(){
  var el=document.getElementById('playlist-picker');
  if(el.style.display!=='none'){el.style.display='none';return}
  el.style.display='block';
  var r=await apiFetch('/api/playlists').then(d=>d.json());
  var list=document.getElementById('playlist-list');
  if(!r.ok||!r.playlists||!r.playlists.length){list.innerHTML='<div style="color:var(--muted);padding:0.3rem">No saved playlists</div>';return}
  var html='';
  r.playlists.forEach(function(p){
    var entries=p.entries||[];
    html+='<div style="display:flex;align-items:center;gap:0.4rem;padding:0.3rem 0;border-bottom:1px solid rgba(255,255,255,0.05)">'
      +'<span style="flex:1;color:var(--amber);cursor:pointer" onclick="loadPlaylist('+p.id+',null,'+JSON.stringify(entries).replace(/"/g,'&quot;')+')">'+esc(p.name)+'</span>'
      +'<span style="color:var(--muted);font-size:0.65rem">'+entries.length+' side'+(entries.length!==1?'s':'')+'</span>'
      +'<button style="background:none;border:none;color:var(--muted);cursor:pointer;font-size:0.7rem" onclick="deletePlaylist('+p.id+')" title="Delete">\u2715</button>'
      +'</div>';
  });
  list.innerHTML=html;
}
async function loadPlaylist(id,albumIds,entries){
  if(entries&&entries.length){
    document.getElementById('playlist-picker').style.display='none';
    if(_isStreaming||_playerActive){
      playMultipleAlbums(null,null,entries);
    }else{
      _pendingPlaylistEntries=entries;_pendingAlbumId=entries[0].a;showOutputPicker(entries[0].a);
    }
    return;
  }
  if(!albumIds||!albumIds.length){showToast('Empty playlist');return}
  document.getElementById('playlist-picker').style.display='none';
  if(_isStreaming||_playerActive){
    playMultipleAlbums(albumIds);
  }else{
    _pendingAlbumIds=albumIds;_pendingAlbumId=albumIds[0];showOutputPicker(albumIds[0]);
  }
}
var _pendingPlaylistEntries=null;
async function deletePlaylist(id){
  if(!confirm('Delete this playlist?'))return;
  await apiFetch('/api/playlists/'+id,{method:'DELETE'});
  togglePlaylistPicker();togglePlaylistPicker();
}

// --- Playlists Panel ---
var _playlistsPanelOpen=false;
var _plCurrentId=null; // playlist being viewed in detail
function togglePlaylistsPanel(){
  _playlistsPanelOpen=!_playlistsPanelOpen;
  var p=document.getElementById('playlists-panel');
  p.classList.toggle('open',_playlistsPanelOpen);
  document.getElementById('playlists-backdrop').style.display=_playlistsPanelOpen?'block':'none';
  if(_playlistsPanelOpen){plShowList();document.addEventListener('click',closePlOnClickOutside,true)}
  else{document.removeEventListener('click',closePlOnClickOutside,true)}
}
function closePlOnClickOutside(e){
  var p=document.getElementById('playlists-panel');
  if(!p.contains(e.target)&&!e.target.closest('[onclick*="togglePlaylistsPanel"]')){
    _playlistsPanelOpen=false;p.classList.remove('open');
    document.getElementById('playlists-backdrop').style.display='none';
    document.removeEventListener('click',closePlOnClickOutside,true);
  }
}
function plSwitchListTab(tab){
  ['regular','songs','smart'].forEach(function(t){
    var tabEl=document.getElementById('pl-tab-'+t);
    if(tabEl)tabEl.classList.toggle('active',t===tab);
    var viewEl=document.getElementById('pl-view-'+t);
    if(viewEl)viewEl.style.display=t===tab?'':'none';
    var newEl=document.getElementById('pl-new-'+t);
    if(newEl)newEl.style.display=t===tab?'':'none';
  });
  // Hide detail views when switching tabs
  document.getElementById('pl-view-detail').style.display='none';
  document.getElementById('pl-view-song-detail').style.display='none';
  if(tab==='regular')plShowList();
  else if(tab==='songs')splShowList();
  else plLoadSmartPlaylists();
}

async function plLoadSmartPlaylists(){
  const r=await apiFetch('/api/smart-playlists').then(d=>d.json());
  const el=document.getElementById('smart-playlists-list');
  if(!r.ok||!r.playlists||!r.playlists.length){
    _smartPlaylists=[];
    el.innerHTML='<div style="color:var(--muted);padding:1rem;text-align:center;font-size:0.82rem">No smart playlists yet</div>';
    return;
  }
  _smartPlaylists=r.playlists;
  let html='';
  r.playlists.forEach(p=>{
    const count=p.album_count||0;
    html+=`<div class="pl-item" onclick="spShowDetail(${p.id})">`
      +`<span class="pl-item-name">${esc(p.name)}</span>`
      +`<span class="pl-item-count">${count} album${count!==1?'s':''}</span>`
      +`<div class="pl-item-actions">`
      +`<button class="pl-item-btn" onclick="event.stopPropagation();spPlay(${p.id})" title="Play">&#9654;</button>`
      +`<button class="pl-item-btn" onclick="event.stopPropagation();spDelete(${p.id})" title="Delete" style="color:var(--rust)">&#10005;</button>`
      +`</div></div>`;
  });
  el.innerHTML=html;
}

var _spBuilderEditId=null;
var _spBuilderRules=[];
var _spFieldDefs=[
  {field:'genre',label:'Genre',type:'text',ops:['contains','eq','neq']},
  {field:'artist',label:'Artist',type:'text',ops:['contains','eq','neq']},
  {field:'title',label:'Title',type:'text',ops:['contains','eq','neq']},
  {field:'label',label:'Label',type:'text',ops:['contains','eq','neq']},
  {field:'year',label:'Year',type:'number',ops:['eq','gte','lte','gt','lt']},
  {field:'rating',label:'Rating',type:'number',ops:['gte','lte','eq','gt','lt']},
  {field:'audio_count',label:'Recorded',type:'recorded',ops:['gt','eq']},
  {field:'play_count',label:'Play Count',type:'number',ops:['gte','lte','eq','gt','lt']},
  {field:'favorite',label:'Favorite',type:'boolean',ops:['eq']}
];
var _spOpLabels={eq:'is',neq:'is not',contains:'contains',gt:'greater than',gte:'at least',lt:'less than',lte:'at most'};

function showSmartPlaylistBuilder(editId){
  _spBuilderEditId=editId||null;
  _spBuilderRules=[];
  var nameVal='';
  if(editId){
    var existing=(_smartPlaylists||[]).find(function(p){return p.id===editId});
    if(existing){
      nameVal=existing.name||'';
      try{_spBuilderRules=JSON.parse(existing.rules||'[]')}catch(e){_spBuilderRules=[]}
    }
  }
  if(!_spBuilderRules.length){_spBuilderRules=[{field:'genre',op:'contains',value:''}]}

  var overlay=document.getElementById('sp-builder-overlay');
  if(!overlay){
    overlay=document.createElement('div');
    overlay.id='sp-builder-overlay';
    overlay.style.cssText='position:fixed;inset:0;z-index:600;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;padding:1rem';
    overlay.innerHTML=`<div style="background:var(--cream);border-radius:10px;width:100%;max-width:480px;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.3);overflow:hidden">
      <div style="padding:0.8rem 1rem;border-bottom:1px solid var(--paper-dk);display:flex;align-items:center;justify-content:space-between">
        <span id="sp-builder-title" style="font-family:var(--display-font);font-weight:700;font-size:0.95rem;color:var(--ink)">New Smart Playlist</span>
        <button onclick="closeSpBuilder()" style="background:none;border:none;font-size:1.2rem;cursor:pointer;color:var(--muted);padding:0 0.2rem">&#10005;</button>
      </div>
      <div style="flex:1;overflow-y:auto;padding:0.8rem 1rem">
        <div style="margin-bottom:0.8rem">
          <label style="font-size:0.75rem;color:var(--muted);display:block;margin-bottom:0.25rem">Name</label>
          <input id="sp-builder-name" type="text" placeholder="e.g. 80s Jazz, Unplayed Favorites..." style="width:100%;padding:0.4rem 0.5rem;border:1px solid var(--paper-dk);border-radius:5px;font-size:0.85rem;background:var(--paper);color:var(--ink);box-sizing:border-box">
        </div>
        <div style="margin-bottom:0.6rem">
          <label style="font-size:0.75rem;color:var(--muted);display:block;margin-bottom:0.4rem">Rules <span style="font-weight:400">(all must match)</span></label>
          <div id="sp-builder-rules"></div>
          <button onclick="spAddRule()" style="background:none;border:1px dashed var(--paper-dk);border-radius:5px;padding:0.35rem 0.6rem;font-size:0.75rem;color:var(--amber-dk);cursor:pointer;margin-top:0.3rem;width:100%">+ Add Rule</button>
        </div>
        <div style="border-top:1px solid var(--paper-dk);padding-top:0.6rem">
          <div style="font-size:0.75rem;color:var(--muted);margin-bottom:0.3rem">Preview: <strong id="sp-builder-count">0</strong> matching albums</div>
          <div id="sp-builder-preview" style="display:flex;flex-wrap:wrap;gap:0.3rem;max-height:120px;overflow-y:auto"></div>
        </div>
      </div>
      <div style="padding:0.6rem 1rem;border-top:1px solid var(--paper-dk);display:flex;gap:0.4rem;justify-content:flex-end">
        <button class="btn btn-ghost" onclick="closeSpBuilder()">Cancel</button>
        <button class="btn btn-primary" onclick="saveSmartPlaylist()">Save</button>
      </div>
    </div>`;
    document.body.appendChild(overlay);
  }
  overlay.style.display='flex';
  document.getElementById('sp-builder-title').textContent=editId?'Edit Smart Playlist':'New Smart Playlist';
  document.getElementById('sp-builder-name').value=nameVal;
  spRenderRules();
  spPreviewRules();
}

function closeSpBuilder(){
  var overlay=document.getElementById('sp-builder-overlay');
  if(overlay)overlay.style.display='none';
  _spBuilderEditId=null;
}

function spRenderRules(){
  var container=document.getElementById('sp-builder-rules');
  var html='';
  _spBuilderRules.forEach(function(rule,idx){
    var fieldDef=_spFieldDefs.find(function(f){return f.field===rule.field})||_spFieldDefs[0];
    var fieldOpts=_spFieldDefs.map(function(f){return '<option value="'+f.field+'"'+(f.field===rule.field?' selected':'')+'>'+esc(f.label)+'</option>'}).join('');
    var opOpts=fieldDef.ops.map(function(o){return '<option value="'+o+'"'+(o===rule.op?' selected':'')+'>'+esc(_spOpLabels[o]||o)+'</option>'}).join('');
    var valCb='spUpdateRule('+idx+',&quot;value&quot;,this.value);spPreviewRules()';
    var opCb='spUpdateRule('+idx+',&quot;op&quot;,this.value);spPreviewRules()';
    var ss='padding:0.35rem 0.4rem;border:1px solid var(--paper-dk);border-radius:4px;font-size:0.78rem;background:var(--paper);color:var(--ink)';
    var valueHtml='';
    if(fieldDef.type==='recorded'){
      var recCb='var v=this.value;if(v==="recorded"){spUpdateRule('+idx+',&quot;op&quot;,&quot;gt&quot;);spUpdateRule('+idx+',&quot;value&quot;,0)}else{spUpdateRule('+idx+',&quot;op&quot;,&quot;eq&quot;);spUpdateRule('+idx+',&quot;value&quot;,0)};spPreviewRules()';
      valueHtml='<select onchange="'+recCb+'" style="flex:1;'+ss+'">'
        +'<option value="not_recorded"'+(rule.op==='eq'?' selected':'')+'>Not Recorded</option>'
        +'<option value="recorded"'+(rule.op==='gt'?' selected':'')+'>Recorded</option>'
        +'</select>';
    }else if(fieldDef.type==='boolean'){
      valueHtml='<select onchange="'+valCb+'" style="flex:1;'+ss+'">'
        +'<option value="1"'+(String(rule.value)==='1'||rule.value===1?' selected':'')+'>Yes</option>'
        +'<option value="0"'+(String(rule.value)==='0'||rule.value===0?' selected':'')+'>No</option>'
        +'</select>';
    }else{
      var inputType=fieldDef.type==='number'?'number':'text';
      valueHtml='<input type="'+inputType+'" value="'+esc(String(rule.value||''))+'" placeholder="value" onchange="'+valCb+'" oninput="clearTimeout(window._spPreviewTimer);window._spPreviewTimer=setTimeout(spPreviewRules,400)" style="flex:1;'+ss+';min-width:0">';
    }

    html+='<div style="display:flex;gap:0.3rem;align-items:center;margin-bottom:0.3rem">'
      +'<select onchange="spChangeField('+idx+',this.value)" style="width:80px;'+ss+'">'+fieldOpts+'</select>'
      +'<select onchange="'+opCb+'" style="width:80px;'+ss+'">'+opOpts+'</select>'
      +valueHtml
      +(_spBuilderRules.length>1?'<button onclick="spRemoveRule('+idx+')" style="background:none;border:none;color:var(--rust);cursor:pointer;font-size:1rem;padding:0 0.3rem;flex-shrink:0">&#10005;</button>':'')
      +'</div>';
  });
  container.innerHTML=html;
}

function spChangeField(idx,field){
  var def=_spFieldDefs.find(function(f){return f.field===field})||_spFieldDefs[0];
  _spBuilderRules[idx].field=field;
  _spBuilderRules[idx].op=def.ops[0];
  if(def.type==='recorded'){_spBuilderRules[idx].op='gt';_spBuilderRules[idx].value=0}
  else if(def.type==='boolean'){_spBuilderRules[idx].value=1}
  else{_spBuilderRules[idx].value=''}
  spRenderRules();
  spPreviewRules();
}

function spUpdateRule(idx,key,val){_spBuilderRules[idx][key]=val}
function spAddRule(){_spBuilderRules.push({field:'genre',op:'contains',value:''});spRenderRules()}
function spRemoveRule(idx){_spBuilderRules.splice(idx,1);spRenderRules();spPreviewRules()}

async function spPreviewRules(){
  var rules=_spBuilderRules.filter(function(r){return r.value!==''&&r.value!==undefined&&r.value!==null});
  // Client-side preview using _catalogAlbums
  var matching=_catalogAlbums;
  if(rules.length>0){
    matching=_catalogAlbums.filter(function(album){
      for(var i=0;i<rules.length;i++){
        var r=rules[i];
        var val=album[r.field];
        var target=r.value;
        if(r.op==='eq'){
          if(typeof val==='number'||r.field==='year'||r.field==='rating'||r.field==='favorite'||r.field==='audio_count'||r.field==='play_count'){
            if(Number(val)!==Number(target))return false;
          }else{if(String(val||'').toLowerCase()!==String(target).toLowerCase())return false}
        }else if(r.op==='neq'){
          if(String(val||'').toLowerCase()===String(target).toLowerCase())return false;
        }else if(r.op==='contains'){
          if(!val||String(val).toLowerCase().indexOf(String(target).toLowerCase())===-1)return false;
        }else if(r.op==='gt'){if(!(Number(val||0)>Number(target)))return false}
        else if(r.op==='gte'){if(!(Number(val||0)>=Number(target)))return false}
        else if(r.op==='lt'){if(!(Number(val||0)<Number(target)))return false}
        else if(r.op==='lte'){if(!(Number(val||0)<=Number(target)))return false}
      }
      return true;
    });
  }
  document.getElementById('sp-builder-count').textContent=matching.length;
  var previewEl=document.getElementById('sp-builder-preview');
  if(!matching.length){previewEl.innerHTML='<span style="font-size:0.75rem;color:var(--muted);font-style:italic">No albums match these rules</span>';return}
  previewEl.innerHTML=matching.slice(0,20).map(function(a){
    var art=a.user_artwork_path||a.artwork_path;
    var src=art?'/artwork/'+art.split('/').pop()+'?t='+a.id:'';
    return src?'<img src="'+src+'" style="width:44px;height:44px;border-radius:4px;object-fit:cover" title="'+esc(a.title)+' - '+esc(a.artist)+'">'
      :'<div style="width:44px;height:44px;border-radius:4px;background:var(--paper-dk);display:flex;align-items:center;justify-content:center;font-size:1.2rem" title="'+esc(a.title)+'">&#x1F4BF;</div>';
  }).join('')+(matching.length>20?'<span style="font-size:0.72rem;color:var(--muted);align-self:center">+'+(matching.length-20)+' more</span>':'');
}

async function saveSmartPlaylist(){
  var name=document.getElementById('sp-builder-name').value.trim();
  if(!name){showError('Please enter a name');return}
  var rules=_spBuilderRules.filter(function(r){return r.value!==''&&r.value!==undefined&&r.value!==null});
  if(!rules.length){showError('Add at least one rule with a value');return}
  // Normalize numeric values
  rules=rules.map(function(r){
    var def=_spFieldDefs.find(function(f){return f.field===r.field});
    if(def&&(def.type==='number'||def.type==='recorded'||def.type==='boolean')){
      return {field:r.field,op:r.op,value:Number(r.value)};
    }
    return {field:r.field,op:r.op,value:r.value};
  });

  if(_spBuilderEditId){
    await apiFetch('/api/smart-playlists/'+_spBuilderEditId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,rules:rules})}).then(function(r){return r.json()}).then(function(r){
      if(r.ok){showToast('Smart playlist updated');closeSpBuilder();plLoadSmartPlaylists()}else{showError(r.error||'Update failed')}
    });
  }else{
    await apiFetch('/api/smart-playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,rules:rules})}).then(function(r){return r.json()}).then(function(r){
      if(r.ok){showToast('Smart playlist created');closeSpBuilder();plLoadSmartPlaylists()}else{showError(r.error||'Create failed')}
    });
  }
}

async function spPlay(playlistId){
  var r=await apiFetch('/api/smart-playlists/'+playlistId+'/albums').then(function(d){return d.json()});
  var albums=(r.albums||[]).filter(function(a){return a.audio_count>0});
  if(!albums.length){showError('No recorded albums in this smart playlist');return}
  var albumIds=albums.map(function(a){return a.id});
  togglePlaylistsPanel();
  if(_isStreaming||_playerActive){
    playMultipleAlbums(albumIds);
  }else{
    _pendingAlbumIds=albumIds;_pendingAlbumId=albumIds[0];showOutputPicker(albumIds[0]);
  }
}

async function spDelete(playlistId){
  if(!confirm('Delete this smart playlist?'))return;
  await apiFetch('/api/smart-playlists/'+playlistId,{method:'DELETE'});
  plLoadSmartPlaylists();
}

async function spShowDetail(playlistId){
  showSmartPlaylistBuilder(playlistId);
}

async function plShowList(){
  document.getElementById('pl-view-regular').style.display='block';
  document.getElementById('pl-view-detail').style.display='none';
  _plCurrentId=null;
  var r=await apiFetch('/api/playlists').then(d=>d.json());
  var el=document.getElementById('pl-view-regular');
  if(!r.ok||!r.playlists||!r.playlists.length){
    el.innerHTML='<div style="color:var(--muted);padding:1rem;text-align:center;font-size:0.82rem">No playlists yet. Add albums from the catalog.</div>';
    return;
  }
  var html='';
  r.playlists.forEach(function(p){
    var entries=p.entries||[];
    var sideCount=entries.length;
    var albumCount=p.album_ids.length;
    var countLabel=albumCount+' album'+(albumCount!==1?'s':'')+', '+sideCount+' side'+(sideCount!==1?'s':'');
    html+='<div class="pl-item" onclick="plShowDetail('+p.id+')">'
      +'<span class="pl-item-name">'+esc(p.name)+'</span>'
      +'<span class="pl-item-count">'+countLabel+'</span>'
      +'<div class="pl-item-actions">'
      +'<button class="pl-item-btn" onclick="event.stopPropagation();plPlayDirect('+p.id+',null,'+JSON.stringify(entries).replace(/"/g,'&quot;')+')" title="Play">&#9654;</button>'
      +'<button class="pl-item-btn" onclick="event.stopPropagation();plRenamePlaylist('+p.id+',\''+esc(p.name).replace(/'/g,"\\'")+'\')" title="Rename">&#9998;</button>'
      +'<button class="pl-item-btn" onclick="event.stopPropagation();plDeleteFromList('+p.id+')" title="Delete" style="color:var(--rust)">\u2715</button>'
      +'</div></div>';
  });
  el.innerHTML=html;
}
var _plCurrentEntries=[]; // side-level entries [{a, s}, ...]
async function plShowDetail(playlistId){
  _plCurrentId=playlistId;
  document.getElementById('pl-view-regular').style.display='none';
  document.getElementById('pl-view-detail').style.display='flex';
  var r=await apiFetch('/api/playlists').then(d=>d.json());
  var pl=r.playlists.find(function(p){return p.id===playlistId});
  if(!pl){plBackToList();return}
  _plCurrentAlbumIds=pl.album_ids.slice();
  _plCurrentEntries=pl.entries||[];
  document.getElementById('pl-detail-name').textContent=pl.name;
  var sideCount=_plCurrentEntries.length;
  document.getElementById('pl-detail-count').textContent=sideCount+' side'+(sideCount!==1?'s':'');
  _plRenderContents(pl);
  // If playlist is empty, auto-switch to Add tab
  if(!_plCurrentEntries.length){plSwitchTab('add')}
  else{plSwitchTab('contents')}
}
function _plRenderContents(pl){
  var entries=pl?pl.entries:_plCurrentEntries;
  var albumsById={};
  _catalogAlbums.forEach(function(a){albumsById[a.id]=a});
  var html='';
  entries.forEach(function(entry,idx){
    var aid=entry.a,side=entry.s;
    var a=albumsById[aid];
    if(!a)return;
    var art=a.user_artwork_path||a.artwork_path||'';
    var imgSrc=art?'/artwork/'+art.split('/').pop():'';
    html+='<div class="pl-album-item" data-pl-idx="'+idx+'">'
      +'<div class="queue-drag-handle" data-idx="'+idx+'" style="cursor:grab;font-size:1.1rem;color:var(--muted);padding:0 0.15rem">\u2261</div>'
      +(imgSrc?'<img src="'+imgSrc+'" alt="" onerror="this.style.display=\'none\'">':'')
      +'<div class="pl-album-info"><div class="pl-album-title">'+esc(a.title)+'</div><div class="pl-album-artist">'+esc(a.artist)+'</div></div>'
      +'<span style="font-size:0.68rem;color:var(--muted);white-space:nowrap;margin-right:0.2rem">Side '+esc(side)+'</span>'
      +'<button class="pl-album-remove" onclick="plRemoveEntry('+idx+')" title="Remove">\u2715</button>'
      +'</div>';
  });
  if(!html)html='<div style="color:var(--muted);padding:0.8rem;text-align:center;font-size:0.78rem">Tap "Add Albums" to browse your collection</div>';
  document.getElementById('pl-detail-albums').innerHTML=html;
  initPlDrag();
}
function plBackToList(){
  document.getElementById('pl-add-search').value='';
  document.getElementById('pl-browse-grid').innerHTML='';
  _plCurrentAlbumIds=[];
  _plActiveTab='contents';
  plShowList();
}
async function plPlayCurrent(){
  if(!_plCurrentId)return;
  if(!_plCurrentEntries.length){showToast('Empty playlist');return}
  loadPlaylist(_plCurrentId,null,_plCurrentEntries);
  togglePlaylistsPanel();
}
function plPlayDirect(id,albumIds,entries){
  loadPlaylist(id,albumIds,entries);
  togglePlaylistsPanel();
}
async function plDeleteFromList(id){
  if(!confirm('Delete this playlist?'))return;
  await apiFetch('/api/playlists/'+id,{method:'DELETE'});
  plShowList();
}
async function plRenamePlaylist(id,currentName){
  var newName=prompt('New name:',currentName);
  if(!newName||!newName.trim())return;
  const r=await apiFetch('/api/playlists/'+id+'/rename',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:newName.trim()})}).then(d=>d.json());
  if(r.ok){showToast('Playlist renamed');plShowList()}else showError(r.error||'Failed to rename');
}
async function plRemoveEntry(entryIdx){
  if(!_plCurrentId)return;
  await apiFetch('/api/playlists/'+_plCurrentId+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({entry_idx:entryIdx})});
  _plRefreshContents();
}
async function plRemoveAlbum(playlistId,albumId){
  await apiFetch('/api/playlists/'+playlistId+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:albumId})});
  _plRefreshContents();
}
async function plCreateNew(){
  var name=prompt('Playlist name:');
  if(!name||!name.trim())return;
  var r=await apiFetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim(),album_ids:[]})}).then(d=>d.json());
  showToast('Playlist "'+name.trim()+'" created');
  if(r.ok&&r.id){plShowDetail(r.id)}else{plShowList()}
}

// --- Album picker inside playlist detail ---
var _plCurrentAlbumIds=[];  // track what's already in the playlist for checkmarks
var _plActiveTab='contents';
function plSwitchTab(tab){
  _plActiveTab=tab;
  document.getElementById('pl-tab-contents').classList.toggle('active',tab==='contents');
  document.getElementById('pl-tab-add').classList.toggle('active',tab==='add');
  document.getElementById('pl-tab-contents-view').style.display=tab==='contents'?'block':'none';
  var addView=document.getElementById('pl-tab-add-view');
  addView.style.display=tab==='add'?'flex':'none';
  if(tab==='add')plRenderBrowseGrid();
}
function plRenderBrowseGrid(){
  var grid=document.getElementById('pl-browse-grid');
  var q=(document.getElementById('pl-add-search').value||'').toLowerCase().trim();
  var albums=_catalogAlbums.slice();
  // Sort by artist then title (ignore leading "The")
  albums.sort(function(a,b){return artistSortName(a.artist).localeCompare(artistSortName(b.artist))||( a.title||'').localeCompare(b.title||'')});
  if(q){
    albums=albums.filter(function(a){
      return (a.title||'').toLowerCase().indexOf(q)!==-1
        || (a.artist||'').toLowerCase().indexOf(q)!==-1
        || (a.genre||'').toLowerCase().indexOf(q)!==-1;
    });
  }
  var html='';
  albums.forEach(function(a){
    var art=a.user_artwork_path||a.artwork_path||'';
    var imgSrc=art?'/artwork/'+art.split('/').pop():'';
    var inList=_plCurrentAlbumIds.indexOf(a.id)!==-1;
    html+='<div class="pl-browse-item'+(inList?' in-playlist':'')+'" onclick="plToggleAlbum('+a.id+')" title="'+esc(a.artist)+' - '+esc(a.title)+'">'
      +(imgSrc?'<img src="'+imgSrc+'" alt="" loading="lazy">':'<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:0.6rem;color:var(--muted);padding:0.2rem;text-align:center">'+esc(a.title)+'</div>')
      +'<div class="pl-browse-check">&#10003;</div>'
      +'<div class="pl-browse-label">'+esc(a.title)+'</div>'
      +'</div>';
  });
  if(!html)html='<div style="grid-column:1/-1;color:var(--muted);padding:1rem;text-align:center;font-size:0.78rem">No albums found</div>';
  grid.innerHTML=html;
}
var _plFilterDebounce=null;
function plFilterBrowse(q){
  clearTimeout(_plFilterDebounce);
  _plFilterDebounce=setTimeout(plRenderBrowseGrid,150);
}
async function plToggleAlbum(albumId){
  if(!_plCurrentId)return;
  var inList=_plCurrentAlbumIds.indexOf(albumId)!==-1;
  if(inList){
    // Remove all sides of this album
    await apiFetch('/api/playlists/'+_plCurrentId+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:albumId})});
  }else{
    // Add all sides of this album
    await apiFetch('/api/playlists/'+_plCurrentId+'/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:albumId})});
  }
  // Refresh everything from server
  await _plRefreshContents();
  plRenderBrowseGrid();
}
async function _plRefreshContents(){
  var r=await apiFetch('/api/playlists').then(d=>d.json());
  var pl=r.playlists.find(function(p){return p.id===_plCurrentId});
  if(!pl)return;
  _plCurrentAlbumIds=pl.album_ids.slice();
  _plCurrentEntries=pl.entries||[];
  var sideCount=_plCurrentEntries.length;
  document.getElementById('pl-detail-count').textContent=sideCount+' side'+(sideCount!==1?'s':'');
  _plRenderContents(pl);
}

// --- Playlist drag-to-reorder ---
function initPlDrag(){
  var list=document.getElementById('pl-detail-albums');
  var dragIdx=null,dragEl=null,items=[];
  function getItemAtY(y){
    for(var i=0;i<items.length;i++){var r=items[i].el.getBoundingClientRect();if(y>=r.top&&y<=r.bottom)return items[i]}
    return null;
  }
  list.querySelectorAll('.queue-drag-handle').forEach(function(handle){
    handle.addEventListener('pointerdown',function(e){
      e.preventDefault();
      dragIdx=parseInt(this.dataset.idx);
      dragEl=this.closest('.pl-album-item');
      dragEl.style.opacity='0.5';
      items=[];
      list.querySelectorAll('.pl-album-item[data-pl-idx]').forEach(function(el){
        items.push({el:el,idx:parseInt(el.dataset.plIdx)});
      });
      document.addEventListener('pointermove',onMove);
      document.addEventListener('pointerup',onUp);
    });
  });
  function onMove(e){
    e.preventDefault();
    items.forEach(function(it){it.el.style.background=''});
    var target=getItemAtY(e.clientY);
    if(target&&target.idx!==dragIdx){target.el.style.background='rgba(212,162,78,0.15)'}
  }
  function onUp(e){
    document.removeEventListener('pointermove',onMove);
    document.removeEventListener('pointerup',onUp);
    if(dragEl)dragEl.style.opacity='';
    items.forEach(function(it){it.el.style.background=''});
    var target=getItemAtY(e.clientY);
    if(target&&target.idx!==dragIdx&&dragIdx!==null){
      reorderPlaylist(dragIdx,target.idx);
    }
    dragIdx=null;dragEl=null;
  }
}
async function reorderPlaylist(fromIdx,toIdx){
  if(!_plCurrentId)return;
  var r=await apiFetch('/api/playlists/'+_plCurrentId+'/reorder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({from:fromIdx,to:toIdx})}).then(d=>d.json());
  if(r.ok){_plRefreshContents()}
  else{showToast(r.error||'Cannot reorder')}
}

// --- Song Playlists ---
var _splCurrentId=null;
var _splSearchDebounce=null;

async function splShowList(){
  document.getElementById('pl-view-songs').style.display='block';
  document.getElementById('pl-view-song-detail').style.display='none';
  _splCurrentId=null;
  var r=await apiFetch('/api/song-playlists').then(function(d){return d.json()});
  var el=document.getElementById('song-playlists-list');
  if(!r.ok||!r.playlists||!r.playlists.length){
    el.innerHTML='<div style="color:var(--muted);padding:1rem;text-align:center;font-size:0.82rem">No song playlists yet. Create one to get started.</div>';
    return;
  }
  var html='';
  r.playlists.forEach(function(p){
    html+='<div class="pl-item" onclick="splShowDetail('+p.id+')">'
      +'<span class="pl-item-name">'+esc(p.name)+'</span>'
      +'<span class="pl-item-count">'+p.track_count+' song'+(p.track_count!==1?'s':'')+'</span>'
      +'<div class="pl-item-actions">'
      +'<button class="pl-item-btn" onclick="event.stopPropagation();splPlayDirect('+p.id+')" title="Play">&#9654;</button>'
      +'<button class="pl-item-btn" onclick="event.stopPropagation();splRename('+p.id+',\''+esc(p.name).replace(/'/g,"\\'")+'\')" title="Rename">&#9998;</button>'
      +'<button class="pl-item-btn" onclick="event.stopPropagation();splDelete('+p.id+')" title="Delete" style="color:var(--rust)">&#10005;</button>'
      +'</div></div>';
  });
  el.innerHTML=html;
}

async function splShowDetail(id){
  _splCurrentId=id;
  document.getElementById('pl-view-songs').style.display='none';
  document.getElementById('pl-view-song-detail').style.display='flex';
  var r=await apiFetch('/api/song-playlists/'+id).then(function(d){return d.json()});
  if(!r.ok||!r.playlist){splBackToList();return}
  var pl=r.playlist;
  document.getElementById('spl-detail-name').textContent=pl.name;
  document.getElementById('spl-detail-count').textContent=pl.tracks.length+' song'+(pl.tracks.length!==1?'s':'');
  splRenderTracks(pl.tracks);
  if(!pl.tracks.length)splSwitchTab('add');
  else splSwitchTab('contents');
}

function splRenderTracks(tracks){
  var html='';
  tracks.forEach(function(t,idx){
    var artPath=t.user_artwork_path||t.artwork_path;
    var artSrc=artPath?'/artwork/'+artPath.split('/').pop()+'?t='+t.album_id:'';
    var dur=t.duration_secs?fmtTime(t.duration_secs):'';
    html+='<div class="pl-album-item">'
      +(artSrc?'<img src="'+artSrc+'" alt="" style="width:36px;height:36px;border-radius:4px;object-fit:cover;flex-shrink:0">':'')
      +'<div class="pl-album-info"><div class="pl-album-title">'+esc(t.title)+'</div>'
      +'<div class="pl-album-artist">'+esc(t.artist||t.track_artist||'')+' - '+esc(t.album_title)+'</div></div>'
      +'<span style="font-size:0.65rem;color:var(--muted);white-space:nowrap">'+(dur||'')+'</span>'
      +'<button class="pl-album-remove" onclick="splRemoveTrack('+idx+')" title="Remove">&#10005;</button>'
      +'</div>';
  });
  if(!html)html='<div style="color:var(--muted);padding:0.8rem;text-align:center;font-size:0.78rem">Tap "Add Songs" to search for tracks</div>';
  document.getElementById('spl-detail-tracks').innerHTML=html;
}

function splSwitchTab(tab){
  document.getElementById('spl-tab-contents').classList.toggle('active',tab==='contents');
  document.getElementById('spl-tab-add').classList.toggle('active',tab==='add');
  document.getElementById('spl-tab-contents-view').style.display=tab==='contents'?'':'none';
  document.getElementById('spl-tab-add-view').style.display=tab==='add'?'flex':'none';
}

function splSearchSongs(q){
  clearTimeout(_splSearchDebounce);
  if(!q||!q.trim()){document.getElementById('spl-search-results').innerHTML='';return}
  _splSearchDebounce=setTimeout(async function(){
    var r=await apiFetch('/api/catalog/tracks/search?q='+encodeURIComponent(q.trim())).then(function(d){return d.json()});
    var tracks=r.tracks||[];
    var html='';
    if(!tracks.length){
      html='<div style="color:var(--muted);padding:0.5rem;text-align:center;font-size:0.78rem">No songs found</div>';
    }else{
      tracks.forEach(function(t){
        var artPath=t.user_artwork_path||t.artwork_path;
        var artSrc=artPath?'/artwork/'+artPath.split('/').pop()+'?t='+t.album_id:'';
        html+='<div class="pl-album-item" style="cursor:pointer" onclick="splAddTrack('+t.id+')">'
          +(artSrc?'<img src="'+artSrc+'" alt="" style="width:32px;height:32px;border-radius:3px;object-fit:cover;flex-shrink:0">':'')
          +'<div class="pl-album-info"><div class="pl-album-title">'+esc(t.title)+'</div>'
          +'<div class="pl-album-artist">'+esc(t.artist)+' - '+esc(t.album_title)+'</div></div>'
          +'<span style="font-size:0.62rem;color:var(--sage);flex-shrink:0">+ Add</span>'
          +'</div>';
      });
    }
    document.getElementById('spl-search-results').innerHTML=html;
  },250);
}

async function splAddTrack(trackId){
  if(!_splCurrentId)return;
  var r=await apiFetch('/api/song-playlists/'+_splCurrentId+'/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track_id:trackId})}).then(function(d){return d.json()});
  if(r.ok){showToast('Song added');splShowDetail(_splCurrentId)}
}

async function splRemoveTrack(index){
  if(!_splCurrentId)return;
  await apiFetch('/api/song-playlists/'+_splCurrentId+'/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:index})});
  splShowDetail(_splCurrentId);
}

function splBackToList(){
  document.getElementById('pl-view-song-detail').style.display='none';
  document.getElementById('pl-view-songs').style.display='block';
  _splCurrentId=null;
  splShowList();
}

async function splCreateNew(){
  var name=prompt('Song playlist name:');
  if(!name||!name.trim())return;
  var r=await apiFetch('/api/song-playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim()})}).then(function(d){return d.json()});
  showToast('Playlist "'+name.trim()+'" created');
  if(r.ok&&r.id){splShowDetail(r.id)}else{splShowList()}
}

async function splPlayDirect(id){
  if(!_isStreaming&&!_playerActive){
    showToast('Select an output device first by playing an album');return;
  }
  var r=await apiFetch('/api/song-playlists/'+id).then(function(d){return d.json()});
  if(!r.ok||!r.playlist||!r.playlist.tracks.length){showToast('Empty playlist');return}
  var first=r.playlist.tracks[0];
  playAlbum(first.album_id,first.id);
  togglePlaylistsPanel();
  showToast('Playing: '+first.title);
}

async function splPlayCurrent(){
  if(!_splCurrentId)return;
  splPlayDirect(_splCurrentId);
}

async function splRename(id,currentName){
  var newName=prompt('New name:',currentName);
  if(!newName||!newName.trim())return;
  await apiFetch('/api/song-playlists/'+id,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:newName.trim()})});
  showToast('Playlist renamed');splShowList();
}

async function splDelete(id){
  if(!confirm('Delete this song playlist?'))return;
  await apiFetch('/api/song-playlists/'+id,{method:'DELETE'});
  splShowList();
}

// --- Add to Playlist from Modal ---
async function showAddToPlaylistMenu(){
  var menu=document.getElementById('modal-playlist-menu');
  if(menu.style.display!=='none'){menu.style.display='none';return}
  menu.style.display='block';
  var r=await apiFetch('/api/playlists').then(d=>d.json());
  var opts=document.getElementById('modal-playlist-options');
  if(!r.ok||!r.playlists||!r.playlists.length){
    opts.innerHTML='<div style="color:var(--muted);font-size:0.72rem;padding:0.2rem 0">No playlists yet</div>';
    return;
  }
  var html='';
  r.playlists.forEach(function(p){
    html+='<div style="display:flex;align-items:center;gap:0.4rem;padding:0.3rem 0;border-bottom:1px solid rgba(0,0,0,0.04);cursor:pointer" onclick="addCurrentAlbumToPlaylist('+p.id+',\''+esc(p.name).replace(/'/g,"\\'")+'\')">'
      +'<span style="flex:1;font-size:0.78rem;color:var(--ink)">'+esc(p.name)+'</span>'
      +'<span style="font-size:0.65rem;color:var(--muted)">'+p.album_ids.length+' album'+(p.album_ids.length!==1?'s':'')+'</span>'
      +'</div>';
  });
  opts.innerHTML=html;
}
async function addCurrentAlbumToPlaylist(playlistId,playlistName){
  if(!currentAlbumId)return;
  var r=await apiFetch('/api/playlists/'+playlistId+'/add',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:currentAlbumId})}).then(d=>d.json());
  if(r.ok){showToast('Added to "'+playlistName+'"')}
  else{showToast(r.error||'Failed to add')}
  document.getElementById('modal-playlist-menu').style.display='none';
}
async function addToNewPlaylistFromModal(){
  if(!currentAlbumId)return;
  var name=prompt('New playlist name:');
  if(!name||!name.trim())return;
  var r=await apiFetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim(),album_ids:[currentAlbumId]})}).then(d=>d.json());
  if(r.ok){showToast('Added to new playlist "'+name.trim()+'"')}
  else{showToast(r.error||'Failed')}
  document.getElementById('modal-playlist-menu').style.display='none';
}

let _eqT=null;
function sendEQ(){clearTimeout(_eqT);_eqT=setTimeout(()=>{apiFetch('/api/eq',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bass:parseInt(document.getElementById('eq-bass').value),treble:parseInt(document.getElementById('eq-treble').value)})})},150)}
function sendVolume(){clearTimeout(_eqT);_eqT=setTimeout(()=>{const vol=parseInt(document.getElementById('eq-volume').value);apiFetch('/api/volume',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({volume:vol})}).then(r=>r.json()).catch(()=>{});if(_currentDevices&&_currentDevices[0]){const dev=_currentDevices[0];apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_volumes:{...(window._deviceVolumes||{}), [dev.id]:vol}})}).catch(()=>{})}},150)}
function applyEQValues(b,t,v){document.getElementById('eq-bass').value=b;document.getElementById('eq-treble').value=t;document.getElementById('eq-volume').value=v;var ba=document.getElementById('eq-bass-adv');if(ba)ba.value=b;var ta=document.getElementById('eq-treble-adv');if(ta)ta.value=t;var rb=document.getElementById('rec-mon-bass');if(rb)rb.value=b;var rt=document.getElementById('rec-mon-treble');if(rt)rt.value=t;var rv=document.getElementById('rec-mon-volume');if(rv)rv.value=v}
// Monitor controls shown while recording with playback (issue #66). They drive
// the same /api/volume and /api/eq plumbing as the main EQ row: the recording
// itself captures raw pre-EQ audio, so these only change what the user hears.
function recMonVolume(){document.getElementById('eq-volume').value=document.getElementById('rec-mon-volume').value;sendVolume()}
function recMonEQ(){document.getElementById('eq-bass').value=document.getElementById('rec-mon-bass').value;document.getElementById('eq-treble').value=document.getElementById('rec-mon-treble').value;sendEQ()}
function syncRecMonitorControls(){
  var el=document.getElementById('rec-monitor-controls');if(!el)return;
  var show=!!_isStreaming;
  el.style.display=show?'block':'none';
  if(show){document.getElementById('rec-mon-volume').value=document.getElementById('eq-volume').value;document.getElementById('rec-mon-bass').value=document.getElementById('eq-bass').value;document.getElementById('rec-mon-treble').value=document.getElementById('eq-treble').value}
  // While a side is recording but nothing is being monitored, offer a way back
  // instead of an empty panel (issue #77).
  var resume=document.getElementById('rec-monitor-resume');
  if(resume)resume.style.display=(!show&&_recAlbumId!==null)?'block':'none';
}
// Start listening to the live vinyl again without disturbing the recording.
// Playback and the live stream contend for the same output, so anything the
// player is doing has to stop first.
async function resumeRecordingMonitor(){
  var btn=document.getElementById('btn-rec-listen');
  if(btn){btn.disabled=true;btn.textContent='Starting…'}
  try{
    if(_playerActive){
      await apiFetch('/api/player/stop',{method:'POST'});
      await new Promise(function(r){setTimeout(r,400)});
    }
    var target=_recOutputDevice;
    if(!target){
      // Recording was started without audio: fall back to the saved output,
      // which is whatever was last streamed to.
      var d=await apiFetch('/api/status').then(function(r){return r.json()});
      var saved=(d.settings&&d.settings.saved_devices)||[];
      target=saved.length?saved[0]:null;
    }
    if(!target){showError('Pick an output device first');return}
    if(target.id&&String(target.id).indexOf('browser')===0){
      // "This Device" needs a fresh browser stream for the server to attach.
      stopBrowserAudioStream();
      var sr=await createBrowserStream();
      if(!sr.ok){showError(sr.error||'Failed to create browser stream');return}
      target={id:'browser:'+sr.stream_id,name:target.name||'This Device'};
    }
    _recOutputDevice=target;
    var r=await apiFetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({devices:[target],volume:parseInt(document.getElementById('eq-volume').value||80)})}).then(function(r){return r.json()});
    if(!r.ok){showError(r.error||'Could not start listening');return}
    if(String(target.id).indexOf('browser:')===0){
      await new Promise(function(res){setTimeout(res,500)});
      startBrowserAudioStream(String(target.id).replace('browser:',''));
    }
  }finally{
    if(btn){btn.disabled=false;btn.innerHTML='Listen'}
    syncRecMonitorControls();
  }
}
function showEQPanel(){document.getElementById('eq-modal').classList.add('open');syncAdvEQ()}
function hideEQPanel(){document.getElementById('eq-modal').classList.remove('open')}
function syncAdvEQ(){document.getElementById('eq-bass-adv').value=document.getElementById('eq-bass').value;document.getElementById('eq-treble-adv').value=document.getElementById('eq-treble').value}
var _bandT=null;
function sendBands(){
  clearTimeout(_bandT);
  for(var i=0;i<5;i++){document.getElementById('eq-band-val-'+i).textContent=document.getElementById('eq-band-'+i).value}
  clearActivePreset();
  _bandT=setTimeout(function(){
    var bands=[];for(var i=0;i<5;i++)bands.push(parseInt(document.getElementById('eq-band-'+i).value));
    apiFetch('/api/eq/bands',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bands:bands})});
  },150);
}
function sendEQFromAdv(){
  var b=parseInt(document.getElementById('eq-bass-adv').value);
  var t=parseInt(document.getElementById('eq-treble-adv').value);
  document.getElementById('eq-bass').value=b;document.getElementById('eq-treble').value=t;
  clearActivePreset();sendEQ();
}
function applyPreset(name){
  apiFetch('/api/eq/preset/'+name,{method:'POST'}).then(function(r){return r.json()}).then(function(d){
    if(!d.ok)return;
    document.getElementById('eq-bass').value=d.bass;document.getElementById('eq-treble').value=d.treble;
    document.getElementById('eq-bass-adv').value=d.bass;document.getElementById('eq-treble-adv').value=d.treble;
    for(var i=0;i<5;i++){document.getElementById('eq-band-'+i).value=d.bands[i];document.getElementById('eq-band-val-'+i).textContent=d.bands[i]}
    document.querySelectorAll('.eq-preset-btn').forEach(function(b){b.classList.toggle('active',b.dataset.preset===name)});
  });
}
function clearActivePreset(){document.querySelectorAll('.eq-preset-btn').forEach(function(b){b.classList.remove('active')})}
function applyBandValues(bands,preset){
  if(!bands)return;
  for(var i=0;i<5;i++){var el=document.getElementById('eq-band-'+i);if(el){el.value=bands[i]||0;document.getElementById('eq-band-val-'+i).textContent=bands[i]||0}}
  if(preset)document.querySelectorAll('.eq-preset-btn').forEach(function(b){b.classList.toggle('active',b.dataset.preset===preset)});
}
function updateCrossfadeLabel(){var v=parseFloat(document.getElementById('crossfade-slider').value);document.getElementById('crossfade-label').textContent=v===0?'Off':v.toFixed(1)+'s'}
function saveCrossfade(){var v=parseFloat(document.getElementById('crossfade-slider').value);apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({crossfade_secs:v})})}
function updateDetectThresholdLabel(){var v=parseFloat(document.getElementById('detect-threshold-slider').value);document.getElementById('detect-threshold-label').textContent=v.toFixed(3)}
function saveDetectThreshold(){var v=parseFloat(document.getElementById('detect-threshold-slider').value);apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({audio_detect_threshold:v})})}

// --- Sleep Timer ---
var _sleepTimerInterval=null;
var _sleepTimerEnd=null;
var _sleepTimerOptions=[15,30,45,60,90];
var _sleepTimerIdx=-1;
function toggleSleepTimer(){
  _sleepTimerIdx++;
  if(_sleepTimerIdx>=_sleepTimerOptions.length){
    // Cancel timer
    clearInterval(_sleepTimerInterval);_sleepTimerInterval=null;_sleepTimerEnd=null;_sleepTimerIdx=-1;
    document.getElementById('sleep-timer-btn').style.opacity='0.4';
    document.getElementById('sleep-timer-btn').title='Sleep timer';
    showToast('Sleep timer off');
    return;
  }
  var mins=_sleepTimerOptions[_sleepTimerIdx];
  _sleepTimerEnd=Date.now()+mins*60000;
  clearInterval(_sleepTimerInterval);
  _sleepTimerInterval=setInterval(function(){
    if(!_sleepTimerEnd){clearInterval(_sleepTimerInterval);return}
    var remaining=Math.max(0,_sleepTimerEnd-Date.now());
    if(remaining<=0){
      // Time's up - stop playback
      clearInterval(_sleepTimerInterval);_sleepTimerInterval=null;_sleepTimerEnd=null;_sleepTimerIdx=-1;
      document.getElementById('sleep-timer-btn').style.opacity='0.4';
      document.getElementById('sleep-timer-btn').title='Sleep timer';
      if(_playerActive)playerStop();
      showToast('Sleep timer: playback stopped');
      return;
    }
    var m=Math.ceil(remaining/60000);
    document.getElementById('sleep-timer-btn').title='Sleep: '+m+'m remaining (tap to change)';
  },10000);
  document.getElementById('sleep-timer-btn').style.opacity='1';
  document.getElementById('sleep-timer-btn').title='Sleep: '+mins+'m (tap to change, tap past 90m to cancel)';
  showToast('Sleep timer: '+mins+' minutes');
}

async function openAlbumDetail(id){
  currentAlbumId=id;
  var plMenu=document.getElementById('modal-playlist-menu');if(plMenu)plMenu.style.display='none';
  const [cd,td]=await Promise.all([apiFetch('/api/catalog').then(r=>r.json()),apiFetch(`/api/catalog/${id}/tracks`).then(r=>r.json())]);
  const a=cd.albums.find(x=>x.id===id);if(!a)return;
  const art=a.user_artwork_path||a.artwork_path;
  const artEl=document.getElementById('modal-art');artEl.src=art?`/artwork/${art.split('/').pop()}?t=${Date.now()}`:'';artEl.style.display=art?'':'none';
  var titleEl=document.getElementById('modal-title');titleEl.textContent=a.title;
  titleEl.onclick=function(){editAlbumMetadata(id,'title')};
  var artistEl=document.getElementById('modal-artist');artistEl.textContent=a.artist;
  artistEl.onclick=function(){editAlbumMetadata(id,'artist')};
  var metaEl=document.getElementById('modal-meta');
  var metaParts=[];
  if(a.year)metaParts.push('<span class="meta-tag" data-field="year" title="Tap to edit year" onclick="editAlbumMetadata('+id+',\'year\');event.stopPropagation()">'+esc(String(a.year))+'</span>');
  if(a.genre)metaParts.push('<span class="meta-tag" data-field="genre" title="Tap to edit genre" onclick="editAlbumMetadata('+id+',\'genre\');event.stopPropagation()">'+esc(String(a.genre))+'</span>');
  if(a.label)metaParts.push('<span class="meta-tag" data-field="label" title="Tap to edit label" onclick="editAlbumMetadata('+id+',\'label\');event.stopPropagation()">'+esc(a.label)+'</span>');
  metaEl.innerHTML=metaParts.join(' <span style="opacity:0.4">\xB7</span> ');
  // Update favorite button
  const favBtn=document.getElementById('modal-favorite-btn');
  if(favBtn){favBtn.textContent=a.favorite?'\u2665':'\u2661';favBtn.classList.toggle('is-favorite',!!a.favorite)}
  // Notes
  var notesDisplay=document.getElementById('modal-notes-display');
  var notesAddBtn=document.getElementById('modal-notes-add-btn');
  var notesEdit=document.getElementById('modal-notes-edit');
  notesEdit.style.display='none';
  if(a.notes&&a.notes.trim()){notesDisplay.textContent=a.notes;notesDisplay.style.display='block';notesAddBtn.style.display='none'}else{notesDisplay.style.display='none';notesAddBtn.style.display='inline-block'}
  // Rating
  renderModalRating(a.rating||0);
  updateAlbumEqBadge(a.eq_settings);
  td.tracks.sort((a,b)=>(a.side||'').localeCompare(b.side||'')||parseInt(a.track_number||0)-parseInt(b.track_number||0));
  window._modalTracks=td.tracks.slice();
  const ad=await apiFetch('/api/album-audio/'+id).then(r=>r.json());
  window._modalAlbumHasAudio=(ad.audio&&ad.audio.length)?true:false;
  renderModalTracks(window._modalTracks,false,false);
  document.getElementById('btn-modal-play').style.display=(ad.audio&&ad.audio.length)?'':'none';
  updateResumeButton(a,!!(ad.audio&&ad.audio.length));
  document.getElementById('btn-modal-export').style.display=(ad.audio&&ad.audio.length)?'':'none';
  var expI=_exportStatus[id];
  var dlBtn=document.getElementById('btn-modal-download');
  if(expI){dlBtn.style.display='';document.getElementById('btn-modal-download-label').textContent='Download '+expI.format.toUpperCase()+' ('+expI.total_size_mb+' MB)'}else{dlBtn.style.display='none'}
  document.getElementById('btn-modal-queue').style.display=(ad.audio&&ad.audio.length&&_playerActive)?'':'none';
  loadAlbumAudio(id);
  if(_recAlbumId===id&&_recStartTime){restoreAlbumRecPanel()}else if(_recAwaitingFlipAlbumId===id&&_recAwaitingFlipNextSide){restoreAlbumRecFlipPrompt()}else{hideAlbumRecPanel()}
  const recBtn=document.getElementById('btn-modal-rec');
  if(_recAlbumId&&_recStartTime&&_recAlbumId!==id){recBtn.disabled=true;recBtn.title='Recording in progress on another album'}else{recBtn.disabled=false;recBtn.title=''}
  document.getElementById('album-modal').classList.add('open');
  document.body.classList.add('album-detail-open');
}
function closeModal(){document.getElementById('album-modal').classList.remove('open');document.body.classList.remove('album-detail-open');currentAlbumId=null}
function editAlbumMetadata(albumId, field){
  var modal=document.getElementById('album-modal');
  if(!modal.classList.contains('open'))return;
  var value=document.getElementById('modal-'+field);
  if(!value)value=modal.querySelector('.meta-tag[data-field="'+field+'"]');
  if(!value){console.log('[edit] element not found for field:',field);return}
  var currentText=value.textContent.trim();
  var input=document.createElement('input');
  input.type=field==='year'?'number':'text';
  input.value=currentText;
  input.style.cssText='width:100%;padding:0.3rem 0.4rem;border:1px solid var(--amber-dk);border-radius:4px;font-size:inherit;font-family:inherit;background:var(--cream);color:var(--ink)';
  if(field==='genre')input.placeholder='e.g. Trip-hop, Electronic, Jazz';
  var saved=false;
  input.onblur=function(){if(!saved){saved=true;saveAlbumMetadata(albumId,field,input.value)}};
  input.onkeydown=function(e){if(e.key==='Enter')this.blur();if(e.key==='Escape'){saved=true;openAlbumDetail(albumId)}};
  value.replaceWith(input);
  input.focus();
  input.select();
}
function saveAlbumMetadata(albumId,field,value){
  var data={};
  data[field]=field==='year'?(parseInt(value)||null):value;
  apiFetch('/api/catalog/'+albumId+'/metadata',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(function(r){return r.json()}).then(function(r){
    if(r.ok){showToast('Saved');loadCatalog();setTimeout(function(){openAlbumDetail(albumId)},100)}
    else{showError('Failed to save')}
  }).catch(function(e){showError('Error: '+e.message)});
}
async function toggleModalFavorite(){
  if(!currentAlbumId)return;
  try{
    const r=await apiFetch(`/api/catalog/${currentAlbumId}/favorite`,{method:'POST'}).then(r=>r.json());
    if(r.ok){
      const album=_catalogAlbums.find(a=>a.id===currentAlbumId);
      if(album){album.favorite=r.favorite?1:0}
      const favBtn=document.getElementById('modal-favorite-btn');
      if(favBtn){favBtn.textContent=r.favorite?'\u2665':'\u2661';favBtn.classList.toggle('is-favorite',r.favorite)}
    }
  }catch(e){showError('Failed to toggle favorite')}
}
function editAlbumNotes(){
  var display=document.getElementById('modal-notes-display');
  var addBtn=document.getElementById('modal-notes-add-btn');
  var edit=document.getElementById('modal-notes-edit');
  var input=document.getElementById('modal-notes-input');
  input.value=display.style.display!=='none'?display.textContent:'';
  display.style.display='none';addBtn.style.display='none';edit.style.display='block';
  input.focus();
}
function cancelAlbumNotes(){
  var display=document.getElementById('modal-notes-display');
  var addBtn=document.getElementById('modal-notes-add-btn');
  var edit=document.getElementById('modal-notes-edit');
  edit.style.display='none';
  if(display.textContent&&display.textContent.trim()){display.style.display='block'}else{addBtn.style.display='inline-block'}
}
async function saveAlbumNotes(){
  if(!currentAlbumId)return;
  var notes=document.getElementById('modal-notes-input').value;
  try{
    var r=await apiFetch('/api/catalog/'+currentAlbumId+'/notes',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({notes:notes})}).then(function(r){return r.json()});
    if(r.ok){
      var display=document.getElementById('modal-notes-display');
      var addBtn=document.getElementById('modal-notes-add-btn');
      var edit=document.getElementById('modal-notes-edit');
      edit.style.display='none';
      if(notes.trim()){display.textContent=notes;display.style.display='block';addBtn.style.display='none'}else{display.style.display='none';addBtn.style.display='inline-block'}
      var album=_catalogAlbums.find(function(a){return a.id===currentAlbumId});
      if(album)album.notes=notes.trim()||null;
      showToast('Notes saved');
    }
  }catch(e){showError('Failed to save notes')}
}
function renderModalRating(rating){
  var el=document.getElementById('modal-rating');
  var html='';
  for(var i=1;i<=5;i++){
    html+='<button class="'+(i<=rating?'filled':'')+'" onclick="setAlbumRating('+i+')" title="'+i+' star'+(i>1?'s':'')+'">'+(i<=rating?'\u2605':'\u2606')+'</button>';
  }
  el.innerHTML=html;
}
async function setAlbumRating(r){
  if(!currentAlbumId)return;
  var album=_catalogAlbums.find(function(a){return a.id===currentAlbumId});
  // Toggle off if clicking the current rating
  var newRating=(album&&album.rating===r)?0:r;
  try{
    var res=await apiFetch('/api/catalog/'+currentAlbumId+'/rating',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({rating:newRating})}).then(function(r){return r.json()});
    if(res.ok){if(album)album.rating=newRating;renderModalRating(newRating)}
  }catch(e){showToast('Failed to save rating')}
}
function playAlbumFromModal(){if(!currentAlbumId)return;var aid=currentAlbumId;_modalResume=null;if(_isStreaming||_playerActive){playAlbum(aid);closeModal()}else{_pendingAlbumId=aid;closeModal();showOutputPicker(aid)}}

// ── Resume where you left off (issue #74) ────────────────────────────────────
// The player saves the album's side and offset every 10s while playing, and
// clears it when the album plays through. A saved point only earns a Resume
// button once it is far enough in to be worth returning to.
var RESUME_MIN_SECS=30;
var _modalResume=null;
function albumResumePoint(a){
  if(!a)return null;
  var secs=parseFloat(a.last_position_secs||0);
  if(!(secs>RESUME_MIN_SECS))return null;
  var side=a.last_position_side_idx;
  // Older builds stored the playlist index ("0") rather than the side label.
  // Those cannot be mapped to a side reliably, so resume the offset only.
  if(side!==null&&side!==undefined&&/^\d+$/.test(String(side)))side=null;
  return {position_secs:secs,side:side||null};
}
function updateResumeButton(a,hasAudio){
  var btn=document.getElementById('btn-modal-resume');if(!btn)return;
  _modalResume=hasAudio?albumResumePoint(a):null;
  if(!_modalResume){btn.style.display='none';document.getElementById('btn-modal-play-label').textContent='Play';return}
  btn.style.display='';
  document.getElementById('btn-modal-resume-label').textContent=
    'Resume '+(_modalResume.side?'Side '+_modalResume.side+', ':'')+fmtTime(_modalResume.position_secs);
  // Play stays available and always starts from the top; saying so avoids
  // any doubt about which button does what.
  document.getElementById('btn-modal-play-label').textContent='Start over';
}
function resumeAlbumFromModal(){
  if(!currentAlbumId||!_modalResume)return;
  var aid=currentAlbumId,resume=_modalResume;
  if(_isStreaming||_playerActive){playAlbum(aid,null,resume);closeModal()}
  else{_pendingAlbumId=aid;_resumeInfo=resume;closeModal();showOutputPicker(aid)}
}

async function playNextFromModal(){
  if(!currentAlbumId)return;
  if(!_playerActive){showError('No active playback. Start playing first.');return}
  try{
    const r=await apiFetch('/api/player/queue/insert-next',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:currentAlbumId})}).then(r=>r.json());
    if(r.ok){showToast(`Added ${r.inserted||1} side${r.inserted!==1?'s':''} to queue`);closeModal()}else showError(r.error||'Failed to add to queue')
  }catch(e){showError('Failed to insert to queue')}
}

var _reorderExtraSides=[];
function renderModalTracks(tracks,reorder,boundaryEdit){
  const c=document.getElementById('modal-tracks');c.classList.toggle('reorder-mode',reorder);c.classList.toggle('boundary-edit-mode',boundaryEdit);
  if(!reorder)_reorderExtraSides=[];
  if(!tracks.length&&!_reorderExtraSides.length){c.innerHTML='<div style="color:var(--muted);font-size:0.82rem;padding:0.5rem 0">No tracks</div>';return}
  const sides={};tracks.forEach(t=>{var s=t.side||'A';if(!sides[s])sides[s]=[];sides[s].push(t)});
  _reorderExtraSides.forEach(function(s){if(!sides[s])sides[s]=[]});
  var allSides=Object.keys(sides).sort();
  var h='';
  allSides.forEach(function(side){
    var sideTracks=sides[side]||[];
    if(reorder){
      var removeBtn=sideTracks.length===0?'<button class="side-remove-btn" onclick="event.stopPropagation();removeSide(\''+side+'\')" title="Remove empty side">&#10005;</button>':'';
      h+='<div class="side-section" data-side="'+side+'">';
      h+='<div class="side-header"><div class="side-label">Side '+esc(side)+'</div>'+removeBtn+'</div>';
    }else{
      h+='<div class="side-label">Side '+esc(side)+'</div>';
    }
    sideTracks.forEach(function(t,i){
      var dur=t.duration_secs&&t.duration_secs>0?fmtTime(t.duration_secs):(t.start_secs!=null&&t.end_secs!=null?fmtTime(t.end_secs-t.start_secs):'');
      var learned=(t.fingerprint_count||0)>0;
      var canFpFromFlac=t.start_secs!=null&&t.end_secs!=null&&window._modalAlbumHasAudio;
      var fpBadge=learned?(canFpFromFlac?'<span style="color:var(--sage);font-size:0.68rem;cursor:pointer" onclick="event.stopPropagation();reFingerprint('+t.id+')" title="Re-fingerprint from recorded audio">\u2713 '+t.fingerprint_count+'fp</span>':'<span style="color:var(--sage);font-size:0.68rem">\u2713 '+t.fingerprint_count+'fp</span>'):(canFpFromFlac?'<span class="track-fp-learn" style="color:var(--amber-dk);font-size:0.68rem;cursor:pointer;text-decoration:underline" onclick="event.stopPropagation();reFingerprint('+t.id+')" title="Fingerprint from recorded audio">fingerprint</span>':'<span style="color:var(--muted);font-size:0.68rem">unlearned</span>');
      var fpClear=learned?'<span class="track-fp-clear" title="Clear fingerprints for this track" onclick="event.stopPropagation();clearTrackFP('+t.id+',\''+esc(t.title).replace(/'/g,"\\'")+'\')">\u2715</span>':'';
      var handle=reorder?'<span class="drag-handle">\u2807</span>':'';
      var delBtn=reorder?'<button class="track-delete-btn" onclick="event.stopPropagation();deleteTrack('+t.id+',\''+esc(t.title).replace(/'/g,"\\'")+'\')\" title="Delete track">\u2715</button>':'';
      var hasAudio=window._modalAlbumHasAudio;var isPlayable=hasAudio&&!reorder&&!boundaryEdit;
      var trackClick=isPlayable?'onclick="playTrackFromModal('+t.id+')"':'';
      var playIcon=isPlayable?'<span class="track-play-icon"><svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" stroke="none"><polygon points="6 3 20 12 6 21"/></svg></span>':'';
      if(boundaryEdit){
        var stVal=t.start_secs!=null?fmtTimePrecise(t.start_secs):'0:00.0';
        var endVal=t.end_secs!=null?fmtTimePrecise(t.end_secs):'0:00.0';
        h+='<div class="track-row" data-id="'+t.id+'" data-idx="'+i+'" data-side="'+side+'" style="gap:0.6rem"><span class="track-num">'+esc(t.track_number||'')+'</span><span style="flex:1">'+esc(t.title)+'</span><input type="text" class="boundary-time-input" id="boundary-start-'+t.id+'" value="'+stVal+'" placeholder="MM:SS.s"><input type="text" class="boundary-time-input" id="boundary-end-'+t.id+'" value="'+endVal+'" placeholder="MM:SS.s"><button class="btn boundary-save-btn" onclick="saveBoundary('+t.id+')">Save</button></div>';
      }else{
        var durDisplay=dur||'--:--';
        var durSpan='<span class="track-dur" style="color:var(--muted);font-size:0.72rem;min-width:2.5rem;text-align:right;cursor:pointer" onclick="event.stopPropagation();editTrackDuration(this,'+t.id+','+JSON.stringify(t.duration_secs||0)+')" title="Click to edit duration">'+durDisplay+'</span>';
        h+='<div class="track-row" data-id="'+t.id+'" data-idx="'+i+'" data-side="'+side+'" '+trackClick+'>'+handle+playIcon+'<span class="track-num">'+esc(t.track_number||'')+'</span><span style="flex:1">'+esc(t.title)+'</span>'+fpBadge+fpClear+durSpan+delBtn+'</div>';
      }
    });
    if(reorder)h+='</div>';
  });
  if(reorder)h+='<div style="display:flex;gap:0.5rem;margin-top:0.5rem;flex-wrap:wrap"><button class="add-side-btn" onclick="addSide()">+ Add Side</button><button class="add-side-btn" onclick="showAddTrackForm()">+ Add Track</button></div>';
  h+='<div id="add-track-form" style="display:none;margin-top:0.5rem;padding:0.6rem;background:rgba(0,0,0,0.04);border:1px solid rgba(0,0,0,0.08);border-radius:6px">';
  h+='<div style="font-size:0.78rem;font-weight:600;margin-bottom:0.4rem;color:var(--leather)">Add a track</div>';
  h+='<div style="display:flex;gap:0.4rem;flex-wrap:wrap;align-items:end">';
  h+='<div style="flex:1;min-width:120px"><label style="font-size:0.68rem;color:var(--muted)">Title</label><input type="text" id="add-track-title" style="width:100%;padding:0.3rem 0.4rem;border:1px solid rgba(0,0,0,0.15);border-radius:4px;font-size:0.78rem" placeholder="Track title"></div>';
  h+='<div style="width:50px"><label style="font-size:0.68rem;color:var(--muted)">Side</label><input type="text" id="add-track-side" style="width:100%;padding:0.3rem 0.4rem;border:1px solid rgba(0,0,0,0.15);border-radius:4px;font-size:0.78rem" value="A" maxlength="2"></div>';
  h+='<div style="width:40px"><label style="font-size:0.68rem;color:var(--muted)">#</label><input type="text" id="add-track-num" style="width:100%;padding:0.3rem 0.4rem;border:1px solid rgba(0,0,0,0.15);border-radius:4px;font-size:0.78rem" placeholder="auto"></div>';
  h+='<button class="btn btn-accent" onclick="submitAddTrack()" style="font-size:0.75rem;padding:0.3rem 0.7rem">Add</button>';
  h+='<button class="btn btn-ghost" onclick="hideAddTrackForm()" style="font-size:0.75rem;padding:0.3rem 0.5rem">Cancel</button>';
  h+='</div></div>';
  c.innerHTML=h;if(reorder)initDragReorder();
}
function seekToTrack(tid){if(!_playerActive)return;apiFetch('/api/player/seek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track_id:tid})})}
function playTrackFromModal(trackId){if(!currentAlbumId||!window._modalAlbumHasAudio)return;var aid=currentAlbumId;if(_isStreaming||_playerActive){playAlbum(aid,trackId);closeModal()}else{_pendingAlbumId=aid;_pendingTrackId=trackId;closeModal();showOutputPicker(aid)}}
async function refreshModalTracks(aid){if(currentAlbumId!==aid)return;try{const d=await apiFetch(`/api/catalog/${aid}/tracks`).then(r=>r.json());d.tracks.sort((a,b)=>(a.side||'').localeCompare(b.side||'')||parseInt(a.track_number||0)-parseInt(b.track_number||0));window._modalTracks=d.tracks.slice();renderModalTracks(window._modalTracks,false,false)}catch(e){}}

async function loadAlbumAudio(id){
  try{const d=await apiFetch('/api/album-audio/'+id).then(r=>r.json());const s=document.getElementById('modal-audio-section'),l=document.getElementById('modal-audio-list');
  if(!d.audio||!d.audio.length){s.style.display='none';return}s.style.display='block';
  l.innerHTML=d.audio.map(a=>{const dur=a.duration_secs?fmtTime(a.duration_secs):'?',sz=a.file_size?(a.file_size/(1024*1024)).toFixed(1)+' MB':'',playing=_audioPlayingId===a.id;
    return `<div class="audio-row" id="audio-row-${a.id}"><button class="audio-play-btn" data-album="${id}" data-audio="${a.id}" onclick="toggleBrowserAudio(this)">${playing?_svgPause12:_svgPlay12}</button><span style="font-weight:600">Side ${a.side||'?'}</span><span style="color:var(--muted)">${dur}</span><span style="color:var(--muted);margin-left:auto">${sz}</span><button class="audio-del-btn" data-album="${id}" data-audio="${a.id}" onclick="deleteAudioSide(this)">\u2715</button></div>`;
  }).join('')}catch(e){}}
function toggleBrowserAudio(btn){const aid=parseInt(btn.dataset.album),id=parseInt(btn.dataset.audio);if(_audioPlayer&&_audioPlayingId===id){if(_audioPlayer.paused){_audioPlayer.play();btn.innerHTML=_svgPause12}else{_audioPlayer.pause();btn.innerHTML=_svgPlay12}return}stopBrowserAudio();_audioPlayer=new Audio('/api/album-audio/'+aid+'/play/'+id);_audioPlayingId=id;_audioPlayer.play();btn.innerHTML=_svgPause12;_audioPlayer.addEventListener('ended',()=>stopBrowserAudio())}
function stopBrowserAudio(){if(_audioPlayer){_audioPlayer.pause();_audioPlayer=null}if(_audioPlayingId){const r=document.getElementById('audio-row-'+_audioPlayingId);if(r){const b=r.querySelector('.audio-play-btn');if(b)b.innerHTML=_svgPlay12}}_audioPlayingId=null}
async function deleteAudioSide(btn){const aid=parseInt(btn.dataset.album),id=parseInt(btn.dataset.audio);if(!confirm('Delete this recorded side?'))return;await apiFetch(`/api/album-audio/${aid}/${id}`,{method:'DELETE'});loadAlbumAudio(aid);loadCatalog()}
async function uploadArtwork(e){const f=e.target.files[0];if(!f||!currentAlbumId)return;const fd=new FormData();fd.append('file',f);const r=await apiFetch(`/api/catalog/${currentAlbumId}/artwork`,{method:'POST',body:fd}).then(r=>r.json());if(r.ok){openAlbumDetail(currentAlbumId);loadCatalog()}else showError(r.error||'Upload failed')}
async function clearAlbumFingerprints(){if(!currentAlbumId||!confirm('Clear all fingerprints for this album?'))return;await apiFetch(`/api/catalog/${currentAlbumId}/fingerprints`,{method:'DELETE'});showToast('Fingerprints cleared');openAlbumDetail(currentAlbumId)}
async function clearTrackFP(trackId,title){if(!confirm(`Clear fingerprints for "${title}"?`))return;await apiFetch(`/api/catalog/track/${trackId}/fingerprints`,{method:'DELETE'});showToast(`Cleared FP: ${title}`);if(currentAlbumId)openAlbumDetail(currentAlbumId)}
async function reFingerprint(trackId){
  showToast('Fingerprinting from recording...');
  var r=await apiFetch(`/api/catalog/track/${trackId}/re-fingerprint`,{method:'POST'});
  var d=await r.json();
  if(d.ok){showToast(d.message)}else{showToast(d.error||'Failed','error')}
  if(currentAlbumId)openAlbumDetail(currentAlbumId);
}
async function reFingerprintAlbum(albumId,force){
  showToast('Fingerprinting all tracks from recording...');
  var r=await apiFetch(`/api/catalog/${albumId}/re-fingerprint`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({force:!!force})});
  var d=await r.json();
  if(d.ok){showToast(d.message)}else{showToast(d.error||'Failed','error')}
  if(currentAlbumId)openAlbumDetail(currentAlbumId);
}
function editTrackDuration(span,trackId,currentSecs){
  var curVal=currentSecs>0?fmtTime(currentSecs):'';
  var inp=document.createElement('input');
  inp.type='text';inp.value=curVal;inp.placeholder='M:SS';
  inp.style.cssText='width:3.5rem;font-size:0.72rem;text-align:right;padding:0 0.2rem;border:1px solid var(--amber-dk);border-radius:3px;background:var(--paper);color:var(--ink);font-family:var(--body-font)';
  span.replaceWith(inp);inp.focus();inp.select();
  function save(){
    var v=inp.value.trim();
    if(!v){inp.replaceWith(span);return}
    // Parse M:SS or seconds
    var secs=0;
    var parts=v.split(':');
    if(parts.length===2){secs=parseInt(parts[0])*60+parseInt(parts[1])}
    else if(parts.length===3){secs=parseInt(parts[0])*3600+parseInt(parts[1])*60+parseInt(parts[2])}
    else{secs=parseInt(v)}
    if(isNaN(secs)||secs<=0){inp.replaceWith(span);return}
    apiFetch('/api/catalog/track/'+trackId,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({duration_secs:secs})}).then(function(){
      if(currentAlbumId)openAlbumDetail(currentAlbumId);
    });
  }
  inp.addEventListener('blur',save);
  inp.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();inp.blur()}if(e.key==='Escape'){inp.value='';inp.blur()}});
}
async function deleteAlbum(){if(!currentAlbumId||!confirm('Delete this album and all its data?'))return;await apiFetch(`/api/catalog/${currentAlbumId}`,{method:'DELETE'});closeModal();loadCatalog()}
function updateAlbumEqBadge(eqSettings){
  var has=!!eqSettings;
  var badge=document.getElementById('modal-eq-badge');
  var clr=document.getElementById('btn-clear-album-eq');
  if(badge)badge.style.display=has?'':'none';
  if(clr)clr.style.display=has?'':'none';
}
async function saveAlbumEq(){
  if(!currentAlbumId)return;
  try{
    var r=await apiFetch('/api/catalog/'+currentAlbumId+'/eq',{method:'POST'}).then(r=>r.json());
    if(r.ok){updateAlbumEqBadge(true);showToast('EQ saved for this album');var al=_catalogAlbums.find(a=>a.id===currentAlbumId);if(al)al.eq_settings=JSON.stringify(r.eq)}
    else showError(r.error||'Failed to save EQ');
  }catch(e){showError('Failed to save EQ')}
}
async function clearAlbumEq(){
  if(!currentAlbumId)return;
  try{
    await apiFetch('/api/catalog/'+currentAlbumId+'/eq',{method:'DELETE'});
    updateAlbumEqBadge(false);showToast('Album EQ cleared');
    var al=_catalogAlbums.find(a=>a.id===currentAlbumId);if(al)al.eq_settings=null;
  }catch(e){showError('Failed to clear EQ')}
}

function toggleReorderMode(){_reorderMode=!_reorderMode;document.getElementById('btn-reorder').classList.toggle('btn-accent',_reorderMode);renderModalTracks(window._modalTracks||[],_reorderMode)}
function initDragReorder(){
  var c=document.getElementById('modal-tracks');
  var de=null;
  // Make track rows draggable
  c.querySelectorAll('.track-row').forEach(function(r){
    r.draggable=true;
    r.addEventListener('dragstart',function(e){
      de=r;r.classList.add('dragging');
      e.dataTransfer.effectAllowed='move';
    });
    r.addEventListener('dragend',function(){
      r.classList.remove('dragging');de=null;
      c.querySelectorAll('.side-section').forEach(function(s){s.classList.remove('drag-over')});
    });
    r.addEventListener('dragover',function(e){
      e.preventDefault();e.dataTransfer.dropEffect='move';
      if(!de||de===r)return;
      var rect=r.getBoundingClientRect(),mid=rect.top+rect.height/2;
      var section=r.closest('.side-section');
      if(section){
        section.appendChild(de);
        if(e.clientY<mid)section.insertBefore(de,r);
        else section.insertBefore(de,r.nextSibling);
      }
    });
  });
  // Allow dropping on empty side sections
  c.querySelectorAll('.side-section').forEach(function(section){
    section.addEventListener('dragover',function(e){
      e.preventDefault();e.dataTransfer.dropEffect='move';
      c.querySelectorAll('.side-section').forEach(function(s){s.classList.remove('drag-over')});
      section.classList.add('drag-over');
      if(de&&!section.contains(de)){section.appendChild(de)}
    });
    section.addEventListener('dragleave',function(){section.classList.remove('drag-over')});
    section.addEventListener('drop',function(e){
      e.preventDefault();e.stopPropagation();
      section.classList.remove('drag-over');
    });
  });
  // Save on drop at container level
  c.addEventListener('drop',async function(e){
    e.preventDefault();
    var assignments=[];
    c.querySelectorAll('.side-section').forEach(function(section){
      var side=section.dataset.side;
      section.querySelectorAll('.track-row').forEach(function(row){
        assignments.push({id:parseInt(row.dataset.id),side:side});
      });
    });
    if(!assignments.length)return;
    await apiFetch('/api/catalog/'+currentAlbumId+'/reassign-sides',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({tracks:assignments})
    });
    showToast('Track sides updated');
    _reorderMode=false;
    document.getElementById('btn-reorder').classList.remove('btn-accent');
    openAlbumDetail(currentAlbumId);
  });
}
function addSide(){
  var tracks=window._modalTracks||[];
  var existing=new Set(tracks.map(function(t){return t.side||'A'}));
  _reorderExtraSides.forEach(function(s){existing.add(s)});
  var letters='ABCDEFGH';
  var next='';
  for(var i=0;i<letters.length;i++){if(!existing.has(letters[i])){next=letters[i];break}}
  if(!next){showError('Maximum sides reached');return}
  _reorderExtraSides.push(next);
  renderModalTracks(tracks,true,false);
  showToast('Added Side '+next);
}
function removeSide(side){
  var tracks=window._modalTracks||[];
  var hasTracks=tracks.some(function(t){return t.side===side});
  if(hasTracks){showError('Move all tracks off Side '+side+' first');return}
  _reorderExtraSides=_reorderExtraSides.filter(function(s){return s!==side});
  renderModalTracks(tracks,true,false);
}
function showAddTrackForm(){var f=document.getElementById('add-track-form');if(f){f.style.display='block';var ti=document.getElementById('add-track-title');if(ti)ti.focus()}}
function hideAddTrackForm(){var f=document.getElementById('add-track-form');if(f)f.style.display='none'}
async function submitAddTrack(){
  var title=(document.getElementById('add-track-title').value||'').trim();
  if(!title){showError('Enter a track title');return}
  var side=(document.getElementById('add-track-side').value||'A').trim().toUpperCase();
  var num=(document.getElementById('add-track-num').value||'').trim()||null;
  var r=await apiFetch('/api/catalog/'+currentAlbumId+'/tracks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title,side:side,track_number:num})}).then(function(d){return d.json()});
  if(r.ok){showToast('Added "'+title+'"');hideAddTrackForm();openAlbumDetail(currentAlbumId)}else{showError(r.error||'Failed to add track')}
}
async function deleteTrack(tid,title){
  if(!confirm('Delete track "'+title+'"?'))return;
  var r=await apiFetch('/api/catalog/track/'+tid,{method:'DELETE'}).then(function(d){return d.json()});
  if(r.ok){showToast('Deleted "'+title+'"');openAlbumDetail(currentAlbumId)}else{showError('Failed to delete track')}
}
function toggleBoundaryEdit(){_boundaryEditMode=!_boundaryEditMode;document.getElementById('btn-boundary-edit').classList.toggle('btn-accent',_boundaryEditMode);renderModalTracks(window._modalTracks||[],false,_boundaryEditMode)}
async function saveBoundary(trackId){const stElem=document.getElementById(`boundary-start-${trackId}`);const endElem=document.getElementById(`boundary-end-${trackId}`);if(!stElem||!endElem)return;const st=parseTimeToSecs(stElem.value);const end=parseTimeToSecs(endElem.value);if(st===null||end===null){showError('Invalid time format. Use MM:SS.s');return}if(st>=end){showError('Start time must be before end time');return}const resp=await apiFetch(`/api/catalog/track/${trackId}/boundaries`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({start_secs:st,end_secs:end})}).then(r=>r.json());if(resp.ok){showToast('Boundary saved');if(currentAlbumId)refreshModalTracks(currentAlbumId)}else showError(resp.error||'Failed to save')}

function showAlbumRecPanel(){document.getElementById('album-rec-panel').style.display='block';var setup=document.getElementById('album-rec-setup');setup.style.display='block';document.getElementById('album-rec-active').style.display='none';document.getElementById('album-rec-saving').style.display='none';document.getElementById('album-rec-flip-prompt').style.display='none';var sel=document.getElementById('album-rec-side');if(sel){var sides=[...new Set((window._modalTracks||[]).map(t=>t.side||'A'))].sort();if(!sides.length)sides=['A','B'];sel.innerHTML=sides.map(s=>'<option value="'+s+'">'+s+'</option>').join('')}updateRecSideInfo();var sw=document.getElementById('rec-play-audio-switch');if(sw){sw.style.background=_recPlayAudio?'var(--sage)':'rgba(0,0,0,0.15)';sw.querySelector('span').style.transform=_recPlayAudio?'translateX(14px)':'translateX(0)'}var picker=document.getElementById('rec-device-picker');if(picker){picker.style.display=_recPlayAudio?'block':'none';if(_recPlayAudio)loadRecDevicePicker()}}
function updateRecSideInfo(){const side=document.getElementById('album-rec-side').value;const tracks=(window._modalTracks||[]).filter(t=>t.side===side);document.getElementById('album-rec-side-info').textContent=tracks.length?`${tracks.length} track${tracks.length>1?'s':''} on Side ${side}`:''}
function hideAlbumRecPanel(){document.getElementById('album-rec-panel').style.display='none'}
function restoreAlbumRecPanel(){
  document.getElementById('album-rec-panel').style.display='block';
  document.getElementById('album-rec-setup').style.display='none';
  document.getElementById('album-rec-active').style.display='block';
  document.getElementById('album-rec-saving').style.display='none';
  document.getElementById('album-rec-flip-prompt').style.display='none';
  document.getElementById('album-rec-active-side').textContent=_recSide||'?';
  clearInterval(_recTimerInterval);
  _recTimerInterval=setInterval(()=>{const s=Math.floor((Date.now()-_recStartTime)/1000);document.getElementById('album-rec-timer').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')},1000);
  const s=Math.floor((Date.now()-_recStartTime)/1000);document.getElementById('album-rec-timer').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0');
}
function restoreAlbumRecFlipPrompt(){
  // Restore the "Side X saved — flip to Side Y" prompt on modal reopen.
  // Used when a side auto-finalized while the user had the modal closed.
  document.getElementById('album-rec-panel').style.display='block';
  document.getElementById('album-rec-setup').style.display='none';
  document.getElementById('album-rec-active').style.display='none';
  document.getElementById('album-rec-saving').style.display='none';
  document.getElementById('album-rec-flip-prompt').style.display='block';
  document.getElementById('album-rec-done-side').textContent=_recAwaitingFlipDoneSide||'?';
  document.getElementById('album-rec-next-side-label').textContent=_recAwaitingFlipNextSide||'?';
  clearInterval(_recTimerInterval);_recTimerInterval=null;
  setRecordingIndicator(false);
}
var _recOutputDevice=null;
var _recSetupHTML=null;
var _recSide=null;
var _recStartTime=null,_recAwaitingFlipAlbumId=null,_recAwaitingFlipDoneSide=null,_recAwaitingFlipNextSide=null;
var _recAlbumId=null;
var _recPlayAudio=false;
var _eqAutoLoad=false;
function setEqAutoLoadSwitch(on){
  var sw=document.getElementById('eq-auto-load-switch');
  if(sw){sw.style.background=on?'var(--sage)':'rgba(0,0,0,0.15)';sw.querySelector('span').style.transform=on?'translateX(16px)':'translateX(0)'}
}
function toggleEqAutoLoad(){
  _eqAutoLoad=!_eqAutoLoad;
  setEqAutoLoadSwitch(_eqAutoLoad);
  apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({eq_auto_load:_eqAutoLoad})}).then(()=>showToast('Auto-load album EQ '+(_eqAutoLoad?'on':'off'))).catch(()=>showToast('Failed to save'));
}
function toggleRecPlayAudio(){
  _recPlayAudio=!_recPlayAudio;
  var sw=document.getElementById('rec-play-audio-switch');
  if(sw){
    sw.style.background=_recPlayAudio?'var(--sage)':'rgba(0,0,0,0.15)';
    sw.querySelector('span').style.transform=_recPlayAudio?'translateX(14px)':'translateX(0)';
  }
  var picker=document.getElementById('rec-device-picker');
  if(_recPlayAudio){
    picker.style.display='block';
    loadRecDevicePicker();
  }else{
    picker.style.display='none';
  }
  apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rec_play_audio:_recPlayAudio})});
}
async function loadRecDevicePicker(){
  var picker=document.getElementById('rec-device-picker');
  picker.innerHTML='<div style="color:var(--muted);font-size:0.72rem;padding:0.3rem 0">Loading devices...</div>';
  var d=await apiFetch('/api/devices').then(function(r){return r.json()});
  var devices=(d.devices||[]).filter(function(dev){return !dev.hidden&&dev.paired!==false});
  var html='<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:0.4rem">';
  devices.forEach(function(dev,idx){
    var name=dev.custom_name||dev.name;
    var icon=guessDeviceIcon(dev.name,dev.type);
    var isLocal=dev.type==='local';
    html+='<div class="output-device-card" onclick="selectRecDevice('+idx+')" style="min-width:80px;padding:0.4rem 0.5rem;cursor:pointer" data-rec-dev="'+idx+'" id="rec-dev-card-'+idx+'">'
      +'<div class="output-device-icon">'+icon+'</div>'
      +'<div class="output-device-name" style="font-size:0.72rem">'+esc(name)+'</div>'
      +(isLocal?'<div class="device-type-badge local">Local</div>'
        :dev.type==='bluetooth'?'<div class="device-type-badge bt">Bluetooth</div>'
        :'<div class="device-type-badge airplay">AirPlay</div>')
      +'</div>';
  });
  html+='</div>';
  window._recDeviceList=devices;
  picker.innerHTML=html;
  // Auto-select first device
  if(devices.length>0) selectRecDevice(0);
}
var _selectedRecDevIdx=null;
function selectRecDevice(idx){
  _selectedRecDevIdx=idx;
  var cards=document.querySelectorAll('#rec-device-picker .output-device-card');
  cards.forEach(function(c,i){c.style.outline=i===idx?'2px solid var(--amber)':'none';c.style.outlineOffset=i===idx?'-2px':'0'});
}
async function startAlbumRecording(){
  _recSide=document.getElementById('album-rec-side')?.value||'A';
  // If already streaming, skip device picker and go straight to recording
  if(_isStreaming){beginRecordingWithDevice(null);return}
  // If play audio is off (default), skip straight to recording with no audio
  if(!_recPlayAudio){beginRecordingWithDevice(null);return}
  // Play audio is on: use the selected device
  beginRecordingWithDevice(_selectedRecDevIdx);
}
async function beginRecordingWithDevice(devIdx){
  var side=_recSide||document.getElementById('album-rec-side')?.value||'A';
  var dev=devIdx!==null?window._recDeviceList[devIdx]:null;
  // If a device was chosen, start streaming to it first
  if(dev){
    var target={id:dev.id,name:dev.name};
    if(dev.type==='local'){if(dev.alsa_device)target.alsa_device=dev.alsa_device}
    if(dev.type==='bluetooth'){target.address=dev.address||dev.id.replace('bt:','')}
    // "This Device": create a browser stream first so the server can attach
    // it as a live sink, then start Web Audio playback after the stream starts
    var recBrowser=false;
    if(dev.type==='browser'||dev.id==='browser'){
      stopBrowserAudioStream();
      var sr=await createBrowserStream();
      if(sr.ok){target.id='browser:'+sr.stream_id;recBrowser=true}
      else{showError(sr.error||'Failed to create browser stream');showAlbumRecPanel();return}
    }
    _recOutputDevice=target;
    await apiFetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({devices:[target],volume:parseInt(document.getElementById('volume-slider')?.value||80)})});
    await new Promise(function(r){setTimeout(r,500)});
    if(recBrowser)startBrowserAudioStream(target.id.replace('browser:',''));
  }
  // Now start recording
  var r=await apiFetch('/api/album-recording/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:currentAlbumId,side:side})}).then(function(r){return r.json()});
  if(!r.ok){showError(r.error||'Failed');showAlbumRecPanel();return}
  _recAlbumId=currentAlbumId;_recStartTime=Date.now();setRecordingIndicator(true);
  document.getElementById('album-rec-setup').style.display='none';document.getElementById('album-rec-active').style.display='block';syncRecMonitorControls();document.getElementById('album-rec-active-side').textContent=side;document.getElementById('album-rec-timer').textContent='00:00';document.getElementById('album-rec-track-log').innerHTML='';clearInterval(_recTimerInterval);_recTimerInterval=setInterval(()=>{const s=Math.floor((Date.now()-_recStartTime)/1000);document.getElementById('album-rec-timer').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')},1000)
}
async function stopAlbumRecording(keepStream){clearInterval(_recTimerInterval);_recStartTime=null;_recAlbumId=null;setRecordingIndicator(false);document.getElementById('album-rec-active').style.display='none';var r=await apiFetch('/api/album-recording/stop',{method:'POST'}).then(function(r){return r.json()});if(!r.ok){document.getElementById('album-rec-saving').style.display='none';showError(r.error||'Failed to stop and save');return}if(r.already_finalized){document.getElementById('album-rec-saving').style.display='none';hideAlbumRecPanel();if(currentAlbumId){loadAlbumAudio(currentAlbumId);loadCatalog()}}else if(r.encoding_in_background){document.getElementById('album-rec-saving').style.display='block';if(r.message)document.getElementById('album-rec-status-text').textContent=r.message}else{document.getElementById('album-rec-saving').style.display='none'}if(!keepStream&&_recOutputDevice){await apiFetch('/api/stop',{method:'POST'});_recOutputDevice=null}}
async function cancelAlbumRecording(){clearInterval(_recTimerInterval);_recTimerInterval=null;_recStartTime=null;_recAlbumId=null;_recAwaitingFlipAlbumId=null;_recAwaitingFlipDoneSide=null;_recAwaitingFlipNextSide=null;setRecordingIndicator(false);await apiFetch('/api/album-recording/cancel',{method:'POST'});hideAlbumRecPanel()}
function flipAlbumRecording(){stopAlbumRecording(true)}
function onAlbumRecStatus(d){if(d.message)document.getElementById('album-rec-status-text').textContent=d.message;if(d.recording===false){clearInterval(_recTimerInterval);setRecordingIndicator(false);if(d.error||/failed|too short/i.test((d.message||''))){document.getElementById('album-rec-saving').style.display='none'}}if(d.album_id&&currentAlbumId===d.album_id)refreshModalTracks(d.album_id);if(d.track_name){var log=document.getElementById('album-rec-track-log');log.innerHTML+='<div style="padding:0.15rem 0;color:var(--sage)">\u2713 '+esc(d.track_name)+'</div>';log.scrollTop=log.scrollHeight}}
function onAlbumRecSideSaved(d){clearInterval(_recTimerInterval);_recTimerInterval=null;setRecordingIndicator(false);_recStartTime=null;_recAlbumId=null;if(d.has_next_side&&d.next_side){_recAwaitingFlipAlbumId=d.album_id||null;_recAwaitingFlipDoneSide=d.side||null;_recAwaitingFlipNextSide=d.next_side}else{_recAwaitingFlipAlbumId=null;_recAwaitingFlipDoneSide=null;_recAwaitingFlipNextSide=null}document.getElementById('album-rec-active').style.display='none';document.getElementById('album-rec-saving').style.display='none';if(d.has_next_side&&d.next_side){document.getElementById('album-rec-flip-prompt').style.display='block';document.getElementById('album-rec-done-side').textContent=d.side||'?';document.getElementById('album-rec-next-side-label').textContent=d.next_side}else{hideAlbumRecPanel();showToast('All sides recorded!')}if(currentAlbumId){loadAlbumAudio(currentAlbumId);loadCatalog()}}
async function startNextSideRecording(){const ns=document.getElementById('album-rec-next-side-label').textContent;_recSide=ns;document.getElementById('album-rec-flip-prompt').style.display='none';document.getElementById('album-rec-saving').style.display='none';var r=await apiFetch('/api/album-recording/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:currentAlbumId,side:ns})}).then(function(r){return r.json()});if(!r.ok){showError(r.error||'Failed to start next side');showAlbumRecPanel();return}_recAlbumId=currentAlbumId;_recStartTime=Date.now();setRecordingIndicator(true);document.getElementById('album-rec-setup').style.display='none';document.getElementById('album-rec-active').style.display='block';syncRecMonitorControls();document.getElementById('album-rec-active-side').textContent=ns;document.getElementById('album-rec-timer').textContent='00:00';document.getElementById('album-rec-track-log').innerHTML='';document.getElementById('album-rec-status-text').textContent='Waiting for audio\u2026';clearInterval(_recTimerInterval);_recTimerInterval=setInterval(()=>{const s=Math.floor((Date.now()-_recStartTime)/1000);document.getElementById('album-rec-timer').textContent=String(Math.floor(s/60)).padStart(2,'0')+':'+String(s%60).padStart(2,'0')},1000)}
function finishAlbumRecSession(){clearInterval(_recTimerInterval);_recTimerInterval=null;_recStartTime=null;_recAlbumId=null;_recAwaitingFlipAlbumId=null;_recAwaitingFlipDoneSide=null;_recAwaitingFlipNextSide=null;hideAlbumRecPanel();if(currentAlbumId){loadAlbumAudio(currentAlbumId);loadCatalog()}}

function openLearnForAlbum(){if(!currentAlbumId)return;const a=_catalogAlbums.find(x=>x.id===currentAlbumId);if(!a)return;document.getElementById('learn-album-input').value=a.artist+' \u2014 '+a.title;document.getElementById('learn-album-id').value=currentAlbumId;const sa=(window._modalTracks||[]).filter(t=>t.side==='A').length;document.getElementById('learn-track-count').value=sa||5;closeModal();showLearnOverlay()}
function showLearnOverlay(){document.getElementById('learn-overlay').classList.add('open');showLearnStep('setup');populateLearnAlbumSelect()}
function hideLearnOverlay(){document.getElementById('learn-overlay').classList.remove('open');loadCatalog()}
function showLearnStep(s){['setup','active','paused','done'].forEach(x=>document.getElementById('learn-step-'+x).style.display=(x===s)?'block':'none')}
function populateLearnAlbumSelect(){document.getElementById('learn-album-list').innerHTML=_catalogAlbums.map(a=>`<option value="${esc(a.artist)} \u2014 ${esc(a.title)}" data-id="${a.id}">`).join('')}
function onLearnAlbumInput(v){const dl=document.getElementById('learn-album-list');const o=[...dl.options].find(x=>x.value===v);if(o){document.getElementById('learn-album-id').value=o.dataset.id;apiFetch(`/api/catalog/${o.dataset.id}/tracks`).then(r=>r.json()).then(d=>{const sa=(d.tracks||[]).filter(t=>t.side==='A').length;if(sa)document.getElementById('learn-track-count').value=sa})}}
function onLevel(d){const pct=Math.min(100,Math.max(0,(d.db+60)/60*100));const color=pct>90?'var(--rust)':pct>70?'#d4a017':'var(--sage)';const b=document.getElementById('learn-level-bar');if(b){b.style.width=pct+'%';b.style.background=color}const rb=document.getElementById('rec-level-bar');if(rb){rb.style.width=pct+'%';rb.style.background=color}const dbLabel=document.getElementById('rec-level-db');if(dbLabel){dbLabel.textContent=d.db>-60?d.db.toFixed(1)+' dB':'-inf dB'}}
async function startLearnSession(){const aid=parseInt(document.getElementById('learn-album-id').value),tc=parseInt(document.getElementById('learn-track-count').value)||1;if(!aid){showError('Please select an album');return}showLearnStep('active');document.getElementById('learn-status-text').textContent='\u23F3 Starting audio capture\u2026';const r=await apiFetch('/api/learn/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_id:aid,track_count:tc})}).then(r=>r.json());if(!r.ok){showLearnStep('setup');showError(r.error);return}_learnLog=[];document.getElementById('learn-status-text').textContent='\u23F3 Waiting for audio\u2026 drop the needle';document.getElementById('learn-next-track').textContent=r.first_track||'';document.getElementById('learn-progress-bar').style.width='0%';document.getElementById('learn-track-list').innerHTML=''}
async function stopLearnSession(){await apiFetch('/api/learn/stop',{method:'POST'});showLearnStep('setup')}
async function continueLearnSession(){const c=parseInt(document.getElementById('learn-continue-count').value)||1;const r=await apiFetch('/api/learn/continue',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track_count:c})}).then(r=>r.json());if(!r.ok){showError(r.error);return}showLearnStep('active')}
function finishLearnSession(){apiFetch('/api/learn/stop',{method:'POST'});showLearnStep('done');document.getElementById('learn-done-message').textContent=`${_learnLog.length} track(s) learned this session.`}
function onLearnAudioDetected(d){document.getElementById('learn-status-text').textContent=`\u23FA Track ${d.learned+1} of ${d.track_count} \u2014 recording\u2026`;document.getElementById('learn-next-track').textContent=d.next_track?`Capturing: ${d.next_track}`:''}
function onLearnUpdate(d){showLearnStep('active');document.getElementById('learn-progress-bar').style.width=Math.round((d.learned/d.track_count)*100)+'%';document.getElementById('learn-status-text').textContent=d.message||'';document.getElementById('learn-next-track').textContent=d.next_track?`Next: ${d.next_track}`:'';if(d.learned>_learnLog.length){_learnLog.push(d.learned_track||`Track ${d.learned}`);const el=document.getElementById('learn-track-list');el.innerHTML=_learnLog.map((n,i)=>`<div style="padding:0.15rem 0;color:var(--sage)">\u2713 Track ${i+1}: ${esc(n)}</div>`).join('');el.scrollTop=el.scrollHeight}if(d.album_id)refreshModalTracks(d.album_id)}
function onLearnPaused(d){showLearnStep('paused');document.getElementById('learn-pause-message').innerHTML=`<strong>${d.learned} track(s) learned</strong><br>${esc(d.message||'')}`+(d.remaining_in_album>0?`<br><span style="color:var(--muted)">${d.remaining_in_album} tracks still unlearned.</span>`:'<br><span style="color:var(--sage)">All tracks learned!</span>');if(d.remaining_in_album>0&&d.remaining_in_album<=10)document.getElementById('learn-continue-count').value=d.remaining_in_album;if(d.album_id)refreshModalTracks(d.album_id)}
function onLearnDone(d){showLearnStep('done');document.getElementById('learn-done-message').textContent=d.message||`${d.learned||0} track(s) learned.`;if(d.album_id)refreshModalTracks(d.album_id);loadCatalog()}
function onLearnEndOfSide(d){const rem=d.track_count-d.learned;if(rem>0){document.getElementById('learn-pause-message').innerHTML=`\u2713 Learned <strong>${d.learned}</strong> of <strong>${d.track_count}</strong> tracks.<br>End of side detected \u2014 flip the record and press Continue.`;document.getElementById('learn-continue-count').value=rem;showLearnStep('paused')}else{document.getElementById('learn-done-message').textContent=`All ${d.learned} tracks learned successfully.`;showLearnStep('done');loadCatalog()}}

function showAddForm(){document.getElementById('add-modal').classList.add('open');showAddStep('search');document.getElementById('s-artist').value='';document.getElementById('s-album').value='';document.getElementById('s-barcode').value='';document.getElementById('search-results').innerHTML=''}
function hideAddForm(){document.getElementById('add-modal').classList.remove('open');_pendingRelease=null}
function showAddStep(s){['search','confirm','manual'].forEach(x=>document.getElementById('add-step-'+x).style.display=(x===s)?'block':'none')}
function backToSearch(){showAddStep('search')}
function showManualStep(){showAddStep('manual')}
async function doSearch(){const a=document.getElementById('s-artist').value.trim(),al=document.getElementById('s-album').value.trim(),bc=document.getElementById('s-barcode').value.trim();if(!a&&!al&&!bc){showError('Enter an artist, album, or barcode');return}const btn=document.getElementById('btn-search');btn.disabled=true;btn.textContent='Searching\u2026';try{const r=await apiFetch(`/api/catalog/search/discogs?artist=${encodeURIComponent(a)}&album=${encodeURIComponent(al)}&barcode=${encodeURIComponent(bc)}`).then(r=>r.json());const el=document.getElementById('search-results');if(!r.releases||!r.releases.length){el.innerHTML='<div style="color:var(--muted);font-size:0.82rem;padding:0.5rem 0">No results. Try different terms or enter manually.</div>';return}el.innerHTML=r.releases.map((r,i)=>{const art=r.thumb?`<img src="${r.thumb}" style="width:44px;height:44px;border-radius:4px;object-fit:cover">`:'<div style="width:44px;height:44px;border-radius:4px;background:var(--paper-dk);display:flex;align-items:center;justify-content:center">\uD83D\uDCBF</div>';var meta=[r.artist||''];if(r.date)meta.push(r.date);if(r.label)meta.push(esc(r.label));if(r.country)meta.push(r.country);var details=[];if(r.format)details.push(esc(r.format));if(r.tracks)details.push(r.tracks+' tracks');if(r.catno)details.push(esc(r.catno));if(r.barcode)details.push(esc(r.barcode));return `<div style="display:flex;gap:0.5rem;align-items:center;padding:0.45rem 0;border-bottom:1px solid rgba(0,0,0,0.06);cursor:pointer" onclick="selectResult(${i})">${art}<div style="flex:1;min-width:0"><div style="font-size:0.82rem;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(r.title)}</div><div style="font-size:0.72rem;color:var(--muted)">${meta.join(' \xB7 ')}</div>${details.length?'<div style="font-size:0.65rem;color:var(--amber-dk)">'+details.join(' \xB7 ')+'</div>':''}</div></div>`}).join('');window._searchResults=r.releases}catch(e){showError('Search failed: '+e.message)}finally{btn.disabled=false;btn.textContent='Search Discogs'}}
async function selectResult(idx){const r=window._searchResults[idx];if(!r)return;try{const d=await apiFetch(`/api/catalog/release/discogs/${r.id}`).then(r=>r.json());if(!d.release){showError('Could not load details');return}_pendingRelease=d.release;document.getElementById('confirm-album-header').innerHTML=`<div style="font-weight:600">${esc(d.release.title)}</div><div style="font-size:0.78rem;color:var(--muted)">${esc(d.release.artist||'')}${d.release.year?' \xB7 '+d.release.year:''}${d.release.label?' \xB7 '+esc(d.release.label):''}</div>`;const sides={};(d.release.tracks||[]).forEach(t=>{if(!sides[t.side])sides[t.side]=[];sides[t.side].push(t)});Object.keys(sides).forEach(s=>sides[s].sort((a,b)=>parseInt(a.track_number||0)-parseInt(b.track_number||0)));let h='';Object.keys(sides).sort().forEach(side=>{h+=`<div class="side-label">Side ${side}</div>`;sides[side].forEach(t=>{const dur=t.duration_secs?fmtTime(t.duration_secs):'';h+=`<div class="track-row"><span class="track-num">${esc(t.track_number||'')}</span><span style="flex:1">${esc(t.title)}</span><span style="color:var(--muted);font-size:0.72rem">${dur}</span></div>`})});document.getElementById('confirm-tracks').innerHTML=h;showAddStep('confirm')}catch(e){showError('Failed: '+e.message)}}
async function saveRelease(){if(!_pendingRelease)return;const r=await apiFetch('/api/catalog/release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({release:_pendingRelease})}).then(r=>r.json());if(r.ok){hideAddForm();loadCatalog()}else showError(r.error||'Failed to save')}
async function submitManual(){const t=document.getElementById('m-album-title').value.trim(),a=document.getElementById('m-artist').value.trim();if(!t||!a){showError('Album title and artist required');return}const ta=document.getElementById('m-tracks-a').value.split('\n').map(s=>s.trim()).filter(Boolean).map((x,i)=>({title:x,side:'A',track_number:String(i+1)}));const tb=document.getElementById('m-tracks-b').value.split('\n').map(s=>s.trim()).filter(Boolean).map((x,i)=>({title:x,side:'B',track_number:String(i+1)}));const tracks=[...ta,...tb];if(!tracks.length){showError('Enter at least one track');return}const r=await apiFetch('/api/catalog/manual',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({album_title:t,album_artist:a,year:parseInt(document.getElementById('m-year').value)||null,genre:document.getElementById('m-genre').value.trim()||null,label:document.getElementById('m-label').value.trim()||null,notes:document.getElementById('m-notes').value.trim()||null,tracks})}).then(r=>r.json());if(r.ok){hideAddForm();loadCatalog()}else showError(r.error||'Failed')}

function showSettings(){document.getElementById('settings-modal').classList.add('open');document.body.classList.add('settings-open');loadBluetoothPaired();loadAdcGain();loadOskMode();document.getElementById('setting-app-name').value=document.getElementById('app-name').textContent}
function loadOskMode(){var sel=document.getElementById('osk-mode-select');if(!sel)return;var m='auto';try{m=localStorage.getItem('vinyl_osk_mode')||'auto'}catch(e){}sel.value=m}
function saveOskMode(){var m=document.getElementById('osk-mode-select').value;try{localStorage.setItem('vinyl_osk_mode',m)}catch(e){}if(m==='off'||(m==='auto'&&!(window.oskEnabled&&window.oskEnabled()))){if(window.hideOSK)window.hideOSK()}showToast('On-screen keyboard: '+({auto:'Auto',on:'Always',off:'Never'}[m]||m))}
async function loadAdcGain(){
  try{
    const g=await apiFetch('/api/audio/gain').then(r=>r.json());
    const panel=document.getElementById('adc-gain-panel');
    if(!g.detected){panel.style.display='none';return}
    panel.style.display='block';
    document.getElementById('adc-gain-card').textContent=g.label||'the ADC';
    const sl=document.getElementById('adc-gain-slider');
    sl.min=g.min_db;sl.max=g.max_db;sl.step=g.step_db||0.5;
    var val=(g.configured_db!=null?g.configured_db:(g.current_db!=null?g.current_db:6));
    sl.value=val;updateAdcGainLabel();
  }catch(e){}
}
function updateAdcGainLabel(){var v=parseFloat(document.getElementById('adc-gain-slider').value);document.getElementById('adc-gain-label').textContent=(v>=0?'+':'')+v.toFixed(1)+' dB'}
function fmtDb(v){return (v>=0?'+':'')+Number(v).toFixed(1)+' dB'}
async function calibrateAdcGain(){
  var btn=document.getElementById('adc-cal-btn'),st=document.getElementById('adc-cal-status');
  btn.disabled=true;st.textContent='Listening for ~8 seconds. Keep the loud section playing…';
  try{
    var d=await apiFetch('/api/audio/gain/calibrate',{method:'POST'}).then(function(r){return r.json()});
    if(d.ok){
      var sl=document.getElementById('adc-gain-slider');sl.value=d.new_db;updateAdcGainLabel();
      st.textContent='Peak '+d.measured_peak_db.toFixed(1)+' dBFS measured. Gain '+fmtDb(d.old_db)+' → '+fmtDb(d.new_db)+(d.clipped?'. Input was clipping: run it once more to fine-tune.':'.');
      showToast('Input gain calibrated to '+fmtDb(d.new_db));
    }else{st.textContent=d.error||'Calibration failed'}
  }catch(e){st.textContent='Calibration failed'}
  btn.disabled=false;
}
function saveAdcGain(){var v=parseFloat(document.getElementById('adc-gain-slider').value);apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({adc_gain_db:v})}).then(()=>showToast('Input gain set to '+(v>=0?'+':'')+v.toFixed(1)+' dB')).catch(()=>showToast('Failed to set gain'))}
async function loadCertInfo(){
  try{var r=await apiFetch('/api/system/cert-info').then(d=>d.json());
  var el=document.getElementById('cert-info');
  if(!el)return;
  var parts=[];
  if(r.hostname)parts.push('Host: '+r.hostname+'.local');
  if(r.ip)parts.push('IP: '+r.ip);
  if(r.expiry)parts.push('Expires: '+r.expiry);
  parts.push(r.has_ca?'CA: installed (mkcert)':'CA: not found (legacy self-signed)');
  el.textContent=parts.join(' \u00b7 ');
  var urlEl=document.getElementById('cert-https-url');
  if(urlEl&&r.ip)urlEl.textContent='https://'+r.hostname+'.local:8443';
  }catch(e){}}
async function regenerateCerts(){
  if(!confirm('Regenerate HTTPS certificates? You will need to reinstall the CA on your phone and restart the app.'))return;
  try{var r=await apiFetch('/api/system/generate-certs',{method:'POST'}).then(d=>d.json());
  if(r.ok){showToast(r.message);loadCertInfo()}else{showToast(r.error||'Failed')}}catch(e){showToast('Failed to regenerate')}
}
function saveAppName(){var n=document.getElementById('setting-app-name').value.trim()||'Vinyl Streamer';document.getElementById('app-name').textContent=n;document.title=n;apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app_name:n})});showToast('Name updated')}
async function changeRoomPassword(){
  var st=document.getElementById('setting-pw-status');
  var cur=(document.getElementById('setting-pw-current')||{}).value||'';
  var neu=(document.getElementById('setting-pw-new')||{}).value||'';
  if(st){st.style.color='var(--muted)';st.textContent='';}
  if(neu.length<8){if(st){st.style.color='var(--rust)';st.textContent='Use at least 8 characters.';}return;}
  try{
    var r=await apiFetch('/api/auth/change-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:cur,password:neu})});
    var d=await r.json().catch(function(){return {};});
    if(!r.ok||d.ok===false){if(st){st.style.color='var(--rust)';st.textContent=(d&& (d.message||d.error))||'Could not change password';}return;}
    if(document.getElementById('setting-pw-current'))document.getElementById('setting-pw-current').value='';
    if(document.getElementById('setting-pw-new'))document.getElementById('setting-pw-new').value='';
    if(st){st.style.color='var(--sage)';st.textContent='Password updated.';}
    showToast('Password updated');
  }catch(e){if(st){st.style.color='var(--rust)';st.textContent='Network error';}}
}
function hideSettings(){document.getElementById('settings-modal').classList.remove('open');document.body.classList.remove('settings-open')}
function switchSettingsGroup(group){
  document.querySelectorAll('#settings-modal .settings-group').forEach(function(g){g.classList.remove('active-group');g.removeAttribute('open')});
  var target=document.querySelector('#settings-modal .settings-group[data-group="'+group+'"]');
  if(target){target.classList.add('active-group');target.setAttribute('open','')}
  document.querySelectorAll('.settings-nav-item').forEach(function(n){n.classList.toggle('active',n.dataset.group===group)});
  var titles={audio:'Audio',library:'Library',personalization:'Personalization',system:'System'};
  document.getElementById('settings-panel-title').textContent=titles[group]||group;
}
function showStats(){document.getElementById('stats-modal').classList.add('open');loadStats()}
function hideStats(){document.getElementById('stats-modal').classList.remove('open')}
function statsJumpToAlbum(id){if(!id)return;hideStats();var dm=document.getElementById('duplicates-modal');if(dm)dm.classList.remove('open');openAlbumDetail(id)}
async function loadStats(){try{const r=await apiFetch('/api/catalog/stats').then(resp=>resp.json());document.getElementById('stat-plays').textContent=r.total_plays||0;document.getElementById('stat-albums').textContent=r.total_albums||0;document.getElementById('stat-tracks').textContent=r.total_tracks||0;document.getElementById('stat-hours').textContent=(r.total_listening_hours||0).toFixed(1);renderTopAlbums(r.top_albums||[]);renderTopTracks(r.top_tracks||[]);renderRecentPlays(r.recent_plays||[])}catch(e){showError('Failed to load stats')}}
function renderTopAlbums(albums){const c=document.getElementById('top-albums-list');if(!albums.length){c.innerHTML='<div style="color:var(--muted);font-size:0.82rem">No plays yet</div>';return}c.innerHTML=albums.map(a=>{const art=a.user_artwork_path||a.artwork_path;const artHtml=art?`<img src="/artwork/${art.split('/').pop()}" class="stats-album-art" alt="">`:'<div class="stats-album-art" style="background:var(--paper-dk);display:flex;align-items:center;justify-content:center;font-size:1.2rem">💿</div>';return`<div class="stats-album-item" onclick="statsJumpToAlbum(${a.id})"><div class="stats-album-art" style="background:var(--paper-dk);width:50px;height:50px;border-radius:4px;display:flex;align-items:center;justify-content:center;overflow:hidden;border:1px solid rgba(0,0,0,0.1)">${artHtml}</div><div class="stats-album-info"><div class="stats-album-title">${esc(a.title)}</div><div class="stats-album-artist">${esc(a.artist)}</div></div><div class="stats-play-count">${a.play_count} plays</div></div>`}).join('')}
function renderTopTracks(tracks){const c=document.getElementById('top-tracks-list');if(!tracks.length){c.innerHTML='<div style="color:var(--muted);font-size:0.82rem">No plays yet</div>';return}c.innerHTML=tracks.map(t=>`<div class="stats-track-item" onclick="statsJumpToAlbum(${t.album_id||0})"><div class="stats-track-info"><div class="stats-track-title">${esc(t.title)}</div><div class="stats-track-artist">${esc(t.artist||t.album_title)}</div></div><div class="stats-play-count">${t.play_count}</div></div>`).join('')}
function renderRecentPlays(plays){const c=document.getElementById('recent-plays-list');if(!plays.length){c.innerHTML='<div style="color:var(--muted);font-size:0.82rem">No plays yet</div>';return}c.innerHTML=plays.map(p=>{const art=p.user_artwork_path||p.artwork_path;const artHtml=art?`<img src="/artwork/${art.split('/').pop()}" class="stats-recent-art" alt="">`:'<div style="background:var(--paper-dk);width:40px;height:40px;border-radius:4px;display:flex;align-items:center;justify-content:center;font-size:0.9rem;border:1px solid rgba(0,0,0,0.1)">💿</div>';const d=new Date(p.played_at);const h=d.getHours().toString().padStart(2,'0');const m=d.getMinutes().toString().padStart(2,'0');const timeStr=h+':'+m;return`<div class="stats-recent-item" onclick="statsJumpToAlbum(${p.album_id||0})"><div class="stats-recent-art" style="background:var(--paper-dk);width:40px;height:40px;border-radius:4px;display:flex;align-items:center;justify-content:center;overflow:hidden;border:1px solid rgba(0,0,0,0.1);font-size:0.9rem">${artHtml}</div><div class="stats-recent-info"><div class="stats-recent-track">${esc(p.track_title)}</div><div class="stats-recent-album">${esc(p.album_title)}</div></div><div class="stats-recent-time">${timeStr}</div></div>`}).join('')}
async function scanDevices(){try{const d=await apiFetch('/api/scan').then(r=>r.json());renderSettingsDevices(d.devices);renderHiddenDevices(d.devices)}catch(e){showError('Scan failed')}}
function renderSettingsDevices(devs){const l=document.getElementById('settings-device-list');const v=(devs||[]).filter(d=>!d.hidden);if(!v.length){l.innerHTML='<div style="color:var(--muted);font-size:0.82rem">No devices found</div>';return}l.innerHTML=v.map(d=>{const np=d.needs_pairing&&d.paired===false;return `<label class="device-item" style="opacity:${np?'0.6':'1'}"><input type="checkbox" class="dev-check" value='${JSON.stringify({id:d.id,name:d.name}).replace(/'/g,"&apos;")}'><span style="flex:1">${esc(d.name)}</span>${np?`<button class="btn btn-ghost" onclick="pairDevice('${d.id}')">Pair</button>`:''}</label>`}).join('')}
function getSelectedDevices(){return[...document.querySelectorAll('.dev-check:checked')].map(c=>JSON.parse(c.value))}
var _streamActionPending=false;
async function startStream(){
  if(_streamActionPending)return;
  const devs=getSelectedDevices();
  const httpEnabled=document.getElementById('http-stream-enabled')&&document.getElementById('http-stream-enabled').checked;
  if(!devs.length&&!httpEnabled){showError('Select at least one device or enable HTTP live stream');return}
  _streamActionPending=true;
  const bs=document.getElementById('btn-start-stream'),bt=document.getElementById('btn-stop-stream');
  if(bs)bs.disabled=true;if(bt)bt.disabled=true;
  try{
    // "This Device" selected: create a browser stream so the server can
    // attach it as a live sink
    var bIdx=devs.findIndex(function(d){return d.id==='browser'||String(d.id).indexOf('browser:')===0});
    // A stream is already running and This Device is the only thing selected:
    // join it instead of failing with "Already streaming" (#49).
    if(bIdx>=0&&devs.length===1&&_isStreaming){await joinThisDevice();return}
    if(bIdx>=0){
      stopBrowserAudioStream();
      var sr=await createBrowserStream();
      if(sr.ok)devs[bIdx].id='browser:'+sr.stream_id;
      else{showError(sr.error||'Failed to create browser stream');return}
    }
    const r=await apiFetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({devices:devs,volume:parseInt(document.getElementById('eq-volume').value)})}).then(res=>res.json()).catch(()=>({ok:false,error:'Network error'}));
    if(r&&r.ok===false){showError(r.error||'Failed to start streaming');await refreshStatus();return}
    const ok=await waitForStreamingState(true,16,250);
    if(!ok){showError('Start requested, but stream did not become active');await refreshStatus()}
    else if(bIdx>=0&&String(devs[bIdx].id).indexOf('browser:')===0){startBrowserAudioStream(devs[bIdx].id.replace('browser:',''))}
  }finally{
    _streamActionPending=false;
    if(bs)bs.disabled=false;if(bt)bt.disabled=false;
  }
}
async function stopStream(){
  if(_streamActionPending)return;
  _streamActionPending=true;
  const bs=document.getElementById('btn-start-stream'),bt=document.getElementById('btn-stop-stream');
  if(bs)bs.disabled=true;if(bt)bt.disabled=true;
  try{
    const r=await apiFetch('/api/stop',{method:'POST'}).then(res=>res.json()).catch(()=>({ok:false,error:'Network error'}));
    if(r&&r.ok===false){showError(r.error||'Failed to stop streaming');await refreshStatus();return}
    const ok=await waitForStreamingState(false,16,250);
    if(!ok){showError('Stop requested, but stream is still active');await refreshStatus()}
  }finally{
    _streamActionPending=false;
    if(bs)bs.disabled=false;if(bt)bt.disabled=false;
  }
}
async function pairOneProtocol(did,proto){
  const enc=encodeURIComponent(did);
  const s=await apiFetch(`/api/devices/${enc}/pair/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({protocol:proto})}).then(r=>r.json());
  if(!s.ok){showError(s.error||`Pair start (${proto}) failed`);return null}
  showToast(`Check ${proto.toUpperCase()} device for PIN`);
  const pin=prompt(`Enter PIN shown on the device (${proto.toUpperCase()}):`);
  if(!pin){await apiFetch(`/api/devices/${enc}/pair/cancel`,{method:'POST'}).catch(()=>{});return null}
  const f=await apiFetch(`/api/devices/${enc}/pair/pin`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({pin})}).then(r=>r.json());
  if(!f.ok){showError(f.error||`Pair finish (${proto}) failed`);return null}
  return f;
}
async function pairDevice(did){
  // Some Apple TVs require pairing RAOP and AirPlay both. Server tells
  // us via remaining_protocols which still need it.
  let r=await pairOneProtocol(did,'raop');
  if(!r)return;
  const remaining=(r.remaining_protocols||[]);
  for(const proto of remaining){
    const r2=await pairOneProtocol(did,proto);
    if(!r2)return;
  }
  showToast('Paired!');
  scanDevices();
}
function renderHiddenDevices(devs){const l=document.getElementById('hidden-device-list');if(!devs||!devs.length){l.innerHTML='<div style="color:var(--muted);font-size:0.82rem">Scan to manage</div>';return}l.innerHTML=devs.map(d=>`<label class="device-item" style="gap:0.5rem"><input type="checkbox" ${d.hidden?'checked':''} onchange="toggleHidden('${d.id}',this.checked)" style="accent-color:var(--rust)"><span style="font-size:0.82rem">${esc(d.name)}${d.hidden?' (hidden)':''}</span></label>`).join('')}
async function toggleHidden(did,hide){await apiFetch('/api/device/hide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({device_id:did,hidden:hide})})}
async function saveAutoStreamSettings(){const en=document.getElementById('auto-stream-enabled').checked,sel=document.getElementById('auto-stream-device-select'),opt=sel.options[sel.selectedIndex],dev=opt&&opt.value?{id:opt.value,name:opt.textContent}:null;const payload={auto_stream_enabled:en};if(dev){payload.auto_stream_device=dev;window._savedAutoStreamDevice=dev}const shown=dev||window._savedAutoStreamDevice;await apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});document.getElementById('auto-stream-status').textContent=en&&shown?`Auto-streaming to: ${shown.name}`:''}
async function scanAutoStreamDevices(){const sel=document.getElementById('auto-stream-device-select');sel.innerHTML='<option value="">Scanning\u2026</option>';try{const d=await apiFetch('/api/scan').then(r=>r.json());sel.innerHTML='<option value="">\u2014 Select \u2014</option>';(d.devices||[]).filter(d=>!d.hidden).forEach(d=>{const o=document.createElement('option');o.value=d.id;o.textContent=d.name;sel.appendChild(o)});if(window._savedAutoStreamDevice)sel.value=window._savedAutoStreamDevice.id}catch(e){sel.innerHTML='<option value="">Scan failed</option>'}}
function updateHttpStreamUI(){
  const enabled=document.getElementById('http-stream-enabled').checked;
  const bitrate=document.getElementById('http-stream-bitrate').value;
  const urlEl=document.getElementById('http-stream-url');
  const statusEl=document.getElementById('http-stream-status');
  if(urlEl)urlEl.value=location.protocol+'//'+location.host+'/live.mp3';
  if(statusEl)statusEl.textContent=enabled?('Enabled at '+bitrate+' kbps'):'Disabled';
}
async function saveHttpStreamSettings(){
  const enabled=document.getElementById('http-stream-enabled').checked;
  const bitrate=parseInt(document.getElementById('http-stream-bitrate').value,10)||256;
  await apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({http_stream_enabled:enabled,http_stream_bitrate_kbps:bitrate})});
  updateHttpStreamUI();
}
async function saveMaxBrowserListeners(){
  const n=parseInt(document.getElementById('max-browser-listeners').value,10)||3;
  await apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_browser_listeners:n})});
  showToast('Max listeners: '+n+' device'+(n===1?'':'s'));
}
async function copyHttpStreamUrl(){
  const el=document.getElementById('http-stream-url');
  if(!el||!el.value)return;
  try{await navigator.clipboard.writeText(el.value);showToast('HTTP stream URL copied')}catch(e){el.select();document.execCommand('copy');showToast('HTTP stream URL copied')}
}
async function saveDiscogsToken(){const t=document.getElementById('discogs-token').value.trim();await apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({discogs_token:t})});const el=document.getElementById('discogs-saved');el.style.display='inline';setTimeout(()=>el.style.display='none',2000)}
async function saveDiscogsUsername(){const u=document.getElementById('discogs-username').value.trim();await apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({discogs_username:u})});const el=document.getElementById('discogs-user-saved');el.style.display='inline';setTimeout(()=>el.style.display='none',2000)}
async function startDiscogsSync(){const btn=document.getElementById('discogs-sync-btn');btn.disabled=true;btn.textContent='Syncing...';document.getElementById('discogs-sync-progress').style.display='block';document.getElementById('discogs-sync-result').style.display='none';try{const r=await apiFetch('/api/catalog/sync/discogs',{method:'POST'}).then(r=>r.json());if(!r.ok){showError(r.error||'Sync failed');btn.disabled=false;btn.textContent='Sync with Discogs';document.getElementById('discogs-sync-progress').style.display='none'}}catch(e){showError('Sync failed: '+e.message);btn.disabled=false;btn.textContent='Sync with Discogs';document.getElementById('discogs-sync-progress').style.display='none'}}
function onSyncProgress(d){const bar=document.getElementById('discogs-sync-bar');const label=document.getElementById('discogs-sync-label');const pct=d.total>0?Math.round((d.checked/d.total)*100):0;bar.style.width=pct+'%';if(d.phase==='pull'&&(d.state==='running'||d.state==='starting')){label.textContent='Pulling '+d.checked+' of '+d.total+(d.current_album?' \u2014 '+d.current_album:'');label.style.color='var(--cream)'}if(d.phase==='push'&&(d.state==='running'||d.state==='starting')){label.textContent='Pushing '+d.checked+' of '+d.total+(d.current_album?' \u2014 '+d.current_album:'');label.style.color='var(--cream)'}if(d.phase==='done'&&d.state==='complete'){document.getElementById('discogs-sync-progress').style.display='none';const res=document.getElementById('discogs-sync-result');res.style.display='block';res.style.color='var(--sage)';var parts=[];if(d.pulled>0)parts.push('imported <strong>'+d.pulled+'</strong> album'+(d.pulled!==1?'s':''));if(d.pushed>0)parts.push('pushed <strong>'+d.pushed+'</strong> to Discogs');if(d.pulled===0&&d.pushed===0)parts.push('everything in sync');if(d.failed>0)parts.push('<span style="color:var(--rust)">'+d.failed+' failed</span>');res.innerHTML=parts.join(', ');const btn=document.getElementById('discogs-sync-btn');btn.disabled=false;btn.textContent='Sync with Discogs';if(d.pulled>0)loadCatalog()}if(d.state==='error'){document.getElementById('discogs-sync-progress').style.display='none';const res=document.getElementById('discogs-sync-result');res.style.display='block';res.style.color='var(--rust)';res.textContent='Sync error: '+(d.errors&&d.errors[0]||'unknown');const btn=document.getElementById('discogs-sync-btn');btn.disabled=false;btn.textContent='Sync with Discogs'}}
async function startArtworkFetch(){const btn=document.getElementById('artwork-fetch-btn');btn.disabled=true;btn.textContent='Fetching...';document.getElementById('artwork-fetch-progress').style.display='block';document.getElementById('artwork-fetch-result').style.display='none';try{const r=await apiFetch('/api/catalog/artwork/fetch-missing',{method:'POST'}).then(r=>r.json());if(!r.ok){showError(r.error||'Failed');btn.disabled=false;btn.textContent='Fetch missing artwork';document.getElementById('artwork-fetch-progress').style.display='none'}}catch(e){showError('Failed: '+e.message);btn.disabled=false;btn.textContent='Fetch missing artwork';document.getElementById('artwork-fetch-progress').style.display='none'}}
function onArtworkFetchProgress(d){const bar=document.getElementById('artwork-fetch-bar');const label=document.getElementById('artwork-fetch-label');const pct=d.total>0?Math.round((d.checked/d.total)*100):0;bar.style.width=pct+'%';if(d.state==='running'||d.state==='starting'){label.textContent='Fetching '+d.checked+' of '+d.total+(d.current_album?' \u2014 '+d.current_album:'');label.style.color='var(--cream)'}if(d.state==='complete'){document.getElementById('artwork-fetch-progress').style.display='none';const res=document.getElementById('artwork-fetch-result');res.style.display='block';res.style.color='var(--sage)';var msg='Fetched <strong>'+d.fetched+'</strong> cover'+(d.fetched!==1?'s':'');if(d.failed>0)msg+=', <span style="color:var(--rust)">'+d.failed+' failed</span>';if(d.still_missing>0){msg+=' ('+d.still_missing+' album'+(d.still_missing!==1?'s':'')+' still missing artwork)';res.style.color='var(--amber)'}res.innerHTML=msg;const btn=document.getElementById('artwork-fetch-btn');btn.disabled=false;btn.textContent='Fetch missing artwork';if(d.fetched>0)loadCatalog()}if(d.state==='error'){document.getElementById('artwork-fetch-progress').style.display='none';const res=document.getElementById('artwork-fetch-result');res.style.display='block';res.style.color='var(--rust)';res.textContent='Error: '+(d.errors&&d.errors[0]||'unknown');const btn=document.getElementById('artwork-fetch-btn');btn.disabled=false;btn.textContent='Fetch missing artwork'}}
function updateStorageInfo(storage){if(!storage)return;document.getElementById('storage-path').textContent=storage.path||'—';document.getElementById('storage-free').textContent=storage.free_gb||'—';document.getElementById('storage-total').textContent=storage.total_gb||'—'}

// ── Library Maintenance: Rebuild Fingerprints ──
async function startRebuildFingerprints(){
  if(!confirm('Re-run fingerprint extraction on every track in your library? Current fingerprints are backed up first. This takes ~1-2 seconds per track on the Pi 5 and will use one CPU core for the duration.'))return;
  var btn=document.getElementById('rebuild-fp-btn');
  btn.disabled=true;btn.textContent='Starting…';
  document.getElementById('rebuild-fp-result').style.display='none';
  document.getElementById('rebuild-fp-progress').style.display='block';
  try{
    var r=await apiFetch('/api/maintenance/rebuild-fingerprints',{method:'POST'}).then(r=>r.json());
    if(!r.ok){showError(r.error||'Could not start rebuild');btn.disabled=false;btn.textContent='Rebuild all fingerprints';document.getElementById('rebuild-fp-progress').style.display='none'}
  }catch(e){
    showError('Rebuild failed to start: '+e.message);btn.disabled=false;btn.textContent='Rebuild all fingerprints';document.getElementById('rebuild-fp-progress').style.display='none';
  }
}

function onRebuildFpProgress(d){
  var bar=document.getElementById('rebuild-fp-bar');
  var label=document.getElementById('rebuild-fp-label');
  var btn=document.getElementById('rebuild-fp-btn');
  if(!bar||!label||!btn)return;
  var pct=d.total>0?Math.round((d.done/d.total)*100):0;
  bar.style.width=pct+'%';
  if(d.in_progress){
    btn.disabled=true;btn.textContent='Rebuilding…';
    var cur=d.current?(' — '+(d.current.album||'')+': '+(d.current.title||'')):'';
    label.textContent='Rebuilt '+d.done+' / '+d.total+' ('+d.ok+' ok, '+d.failed+' failed)'+cur;
    label.style.color='var(--cream)';
  }else if(d.finished_at){
    document.getElementById('rebuild-fp-progress').style.display='none';
    var res=document.getElementById('rebuild-fp-result');
    res.style.display='block';
    res.style.color=d.failed>0?'var(--amber)':'var(--sage)';
    var msg='Rebuilt <strong>'+d.ok+'</strong> track'+(d.ok!==1?'s':'');
    if(d.failed>0)msg+=', <span style="color:var(--rust)">'+d.failed+' failed</span>';
    msg+=' (backup saved to '+(d.backup_path||'?')+')';
    if(d.last_error&&d.failed>0)msg+='<br><span style="font-size:0.7rem;color:var(--muted)">Last error: '+esc(d.last_error)+'</span>';
    res.innerHTML=msg;
    btn.disabled=false;btn.textContent='Rebuild all fingerprints';
  }
}
async function changeStoragePath(){const p=document.getElementById('new-storage-path').value.trim();if(!p){alert('Enter a path');return}if(!confirm('Move all recordings to '+p+'?'))return;const st=document.getElementById('storage-status');st.style.display='block';st.style.color='var(--muted)';st.textContent='Moving files…';try{const r=await apiFetch('/api/settings/storage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})}).then(r=>r.json());if(r.ok){st.style.color='var(--sage)';st.textContent=r.message;document.getElementById('new-storage-path').value='';const d=await apiFetch('/api/status').then(r=>r.json());if(d.storage)updateStorageInfo(d.storage)}else{st.style.color='var(--rust)';st.textContent=r.error||'Failed'}}catch(e){st.style.color='var(--rust)';st.textContent='Error: '+e.message}}
var _dirPickerPath='/';
function openDirPicker(){
  const cur=document.getElementById('storage-path').textContent;
  // Start one level up from current path so user can see sibling dirs
  var start='/';
  if(cur&&cur!=='—'){var parts=cur.replace(/\/$/,'').split('/');parts.pop();start=parts.join('/')||'/'}
  document.getElementById('dir-picker-overlay').style.display='flex';
  loadDirPicker(start);
}
function closeDirPicker(){document.getElementById('dir-picker-overlay').style.display='none'}
async function loadDirPicker(path){
  _dirPickerPath=path;
  document.getElementById('dir-picker-path').textContent=path;
  document.getElementById('dir-picker-list').innerHTML='<div style="padding:0.5rem;color:var(--muted);font-size:0.82rem">Loading…</div>';
  const d=await apiFetch('/api/browse-dirs?path='+encodeURIComponent(path)).then(r=>r.json());
  if(!d.ok){document.getElementById('dir-picker-list').innerHTML='<div style="padding:0.5rem;color:var(--rust);font-size:0.82rem">'+esc(d.error||'Error')+'</div>';return}
  _dirPickerPath=d.path;
  document.getElementById('dir-picker-path').textContent=d.path;
  let h='';
  if(d.path!=='/'){var parent=d.path.replace(/\/[^/]*$/,'')||'/';h+='<div class="dir-picker-item" onclick="loadDirPicker(\''+parent+'\')">⬆ ..</div>'}
  d.dirs.forEach(name=>{var full=(d.path+'/'+name).replace(/\/\//g,'/');h+='<div class="dir-picker-item" onclick="loadDirPicker(\''+full.replace(/'/g,"\\'")+'\')">📁 '+esc(name)+'</div>'});
  if(!d.dirs.length&&d.path==='/'){h='<div style="padding:0.5rem;color:var(--muted);font-size:0.82rem">No subdirectories</div>'}
  document.getElementById('dir-picker-list').innerHTML=h||'<div style="padding:0.5rem;color:var(--muted);font-size:0.82rem">Empty folder</div>';
}
function selectDir(){
  document.getElementById('new-storage-path').value=_dirPickerPath;
  closeDirPicker();
}
async function createDirInPicker(){
  const name=document.getElementById('dir-picker-new').value.trim();
  if(!name)return;
  const newPath=(_dirPickerPath+'/'+name).replace(/\/\//g,'/');
  try{
    const r=await apiFetch('/api/browse-dirs?path='+encodeURIComponent(newPath)).then(r=>r.json());
    if(r.ok){loadDirPicker(newPath);document.getElementById('dir-picker-new').value='';return}
    // Directory doesn't exist, create via storage endpoint test
    await apiFetch('/api/settings/storage',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:newPath,create_only:true})});
    loadDirPicker(newPath);document.getElementById('dir-picker-new').value='';
  }catch(e){showError('Could not create folder')}
}
async function downloadCatalog(){
  const st=document.getElementById('export-status');
  try{
    const r=await apiFetch('/api/export/catalog');
    if(!r.ok){st.style.display='block';st.style.color='var(--rust)';st.textContent='Download failed';return}
    const blob=await r.blob();
    const url=window.URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;
    a.download='vinyl-catalog.db';
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    document.body.removeChild(a);
    st.style.display='block';
    st.style.color='var(--sage)';
    st.textContent='Catalog downloaded';
    setTimeout(()=>{st.style.display='none'},2000);
  }catch(e){
    st.style.display='block';
    st.style.color='var(--rust)';
    st.textContent='Error: '+e.message;
  }
}
async function downloadManifest(){
  const st=document.getElementById('export-status');
  try{
    st.style.display='block';
    st.style.color='var(--muted)';
    st.textContent='Generating manifest…';
    const r=await apiFetch('/api/export/manifest').then(r=>r.json());
    if(!r.ok){st.style.color='var(--rust)';st.textContent=r.error||'Failed';return}
    const blob=new Blob([JSON.stringify(r,null,2)],{type:'application/json'});
    const url=window.URL.createObjectURL(blob);
    const a=document.createElement('a');
    a.href=url;
    a.download='vinyl-manifest.json';
    document.body.appendChild(a);
    a.click();
    window.URL.revokeObjectURL(url);
    document.body.removeChild(a);
    st.style.color='var(--sage)';
    st.textContent='Manifest downloaded ('+r.total_flac_files+' files, '+Math.round(r.total_size_bytes/1024/1024/1024*10)/10+' GB)';
    setTimeout(()=>{st.style.display='none'},3000);
  }catch(e){
    st.style.display='block';
    st.style.color='var(--rust)';
    st.textContent='Error: '+e.message;
  }
}
// ── Audio Export (AAC / MP3) ──
var _exportJobId=null;
async function exportAlbum(){
  if(!currentAlbumId)return;
  var fmt=prompt('Export format:\n\n1) AAC 256kbps (.m4a) for iTunes / Apple Music\n2) MP3 320kbps (.mp3)\n\nEnter 1 or 2:','1');
  if(!fmt)return;
  fmt=fmt.trim()==='2'?'mp3':'m4a';
  var btn=document.getElementById('btn-modal-export');
  btn.disabled=true;btn.textContent='Exporting...';
  try{
    var r=await apiFetch('/api/export/album/'+currentAlbumId,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({format:fmt})}).then(r=>r.json());
    if(r.ok){
      var msg=r.tracks_exported+' track'+(r.tracks_exported!==1?'s':'')+' exported as '+fmt.toUpperCase();
      if(r.tracks_skipped)msg+=' ('+r.tracks_skipped+' already existed)';
      if(r.tracks_failed)msg+=' ('+r.tracks_failed+' failed)';
      showToast(msg);
      // Refresh export status for badges
      var es=await apiFetch('/api/export/all-status').then(r=>r.json()).catch(()=>({albums:{}}));
      var ea=es.albums||{};_exportStatus={};Object.keys(ea).forEach(k=>{_exportStatus[parseInt(k)]=ea[k]});
      renderCatalog(getFilteredAlbums());
      // Show download button
      var expI=_exportStatus[currentAlbumId];
      var dlBtn=document.getElementById('btn-modal-download');
      if(expI){dlBtn.style.display='';document.getElementById('btn-modal-download-label').textContent='Download '+expI.format.toUpperCase()+' ('+expI.total_size_mb+' MB)'}
    }else{
      showToast(r.error||'Export failed','error');
    }
  }catch(e){showToast('Export error: '+e.message,'error')}
  btn.disabled=false;btn.innerHTML='<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;margin-right:2px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Export';
}
function downloadAlbumFromModal(){
  if(!currentAlbumId)return;
  var expI=_exportStatus[currentAlbumId];
  if(!expI||!expI.relative_path)return;
  window.location='/api/export/download-album?path='+encodeURIComponent(expI.relative_path);
}
async function startBulkExport(){
  var fmt=document.getElementById('bulk-export-format').value;
  var btn=document.getElementById('bulk-export-btn');
  btn.disabled=true;btn.textContent='Starting...';
  document.getElementById('bulk-export-progress').style.display='block';
  document.getElementById('bulk-export-result').style.display='none';
  try{
    var r=await apiFetch('/api/export/bulk',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({format:fmt})}).then(r=>r.json());
    if(r.ok){
      _exportJobId=r.job_id;
      btn.textContent='Exporting...';
      document.getElementById('bulk-export-label').textContent='Starting export...';
    }else{
      showToast(r.error||'Failed to start export','error');
      btn.disabled=false;btn.textContent='Export All Albums';
    }
  }catch(e){showToast('Error: '+e.message,'error');btn.disabled=false;btn.textContent='Export All Albums'}
}
function handleExportProgress(msg){
  if(msg.status==='running'){
    var pct=msg.percent||0;
    document.getElementById('bulk-export-bar').style.width=pct+'%';
    var label=msg.current_album||'';
    if(msg.total_albums)label+=' ('+msg.album_index+'/'+msg.total_albums+')';
    if(msg.current_track)label+=' - '+msg.current_track;
    document.getElementById('bulk-export-label').textContent=label;
  }else if(msg.status==='done'){
    document.getElementById('bulk-export-bar').style.width='100%';
    document.getElementById('bulk-export-label').textContent='';
    document.getElementById('bulk-export-progress').style.display='none';
    var res=document.getElementById('bulk-export-result');
    res.style.display='block';
    var elapsed=msg.elapsed_secs?Math.round(msg.elapsed_secs/60)+'m ':'';
    res.innerHTML='<span style="color:var(--sage)">Done!</span> '+msg.tracks_exported+' tracks from '+msg.total_albums+' albums exported in '+elapsed+(msg.tracks_failed?' ('+msg.tracks_failed+' failed)':'');
    var btn=document.getElementById('bulk-export-btn');
    btn.disabled=false;btn.textContent='Export All Albums';
    _exportJobId=null;
    loadExportStats();browseExports(_exportBrowsePath);
    // Refresh export badges
    apiFetch('/api/export/all-status').then(r=>r.json()).then(es=>{var ea=es.albums||{};_exportStatus={};Object.keys(ea).forEach(k=>{_exportStatus[parseInt(k)]=ea[k]});renderCatalog(getFilteredAlbums())}).catch(()=>{});
  }else if(msg.status==='error'){
    document.getElementById('bulk-export-progress').style.display='none';
    var res=document.getElementById('bulk-export-result');
    res.style.display='block';
    res.innerHTML='<span style="color:var(--rust)">Error:</span> '+(msg.error||'Unknown error');
    var btn=document.getElementById('bulk-export-btn');
    btn.disabled=false;btn.textContent='Export All Albums';
    _exportJobId=null;
  }
}
async function loadExportStats(){
  try{
    var r=await apiFetch('/api/export/stats').then(r=>r.json());
    if(r.ok&&r.total_files>0){
      document.getElementById('bulk-export-stats').textContent=r.total_files+' files, '+r.total_size_mb+' MB exported';
      document.getElementById('export-download-all-size').textContent=r.total_files+' files, '+r.total_size_mb+' MB';
    }
  }catch(e){}
  browseExports('');
}
var _exportBrowsePath='';
async function browseExports(path){
  _exportBrowsePath=path;
  var loading=document.getElementById('export-browser-loading');
  var empty=document.getElementById('export-browser-empty');
  var content=document.getElementById('export-browser-content');
  try{
    var r=await apiFetch('/api/export/browse?path='+encodeURIComponent(path)).then(r=>r.json());
    loading.style.display='none';
    if(!r.ok||(!r.folders.length&&!r.files.length)){
      empty.style.display='block';content.style.display='none';return;
    }
    empty.style.display='none';content.style.display='block';
    // Breadcrumb
    var bc=document.getElementById('export-breadcrumb');
    var parts=path?path.split('/'):[]; var crumbs='<a href="#" onclick="browseExports(\'\');return false" style="color:var(--amber);text-decoration:none;font-weight:600">Exports</a>';
    var buildPath='';
    for(var i=0;i<parts.length;i++){
      buildPath+=(i?'/':'')+parts[i];
      crumbs+=' <span style="color:var(--muted)">/</span> <a href="#" onclick="browseExports(\''+buildPath.replace(/'/g,"\\'")+'\');return false" style="color:var(--amber);text-decoration:none">'+esc(parts[i])+'</a>';
    }
    bc.innerHTML=crumbs;
    // File list
    var list=document.getElementById('export-file-list');
    var html='';
    for(var f of r.folders){
      var fp=path?(path+'/'+f.name):f.name;
      html+='<div style="display:flex;align-items:center;padding:0.35rem 0.5rem;border-bottom:1px solid rgba(128,128,128,0.1);cursor:pointer" onclick="browseExports(\''+fp.replace(/'/g,"\\'")+'\')">'
        +'<span style="margin-right:0.4rem">&#128193;</span>'
        +'<span style="flex:1;font-size:0.8rem">'+esc(f.name)+'</span>'
        +'<span style="font-size:0.7rem;color:var(--muted);margin-right:0.5rem">'+f.track_count+' tracks</span>'
        +'<button class="btn" style="font-size:0.68rem;padding:0.15rem 0.4rem;background:var(--amber);color:var(--charcoal)" onclick="event.stopPropagation();downloadAlbumZip(\''+fp.replace(/'/g,"\\'")+'\')">ZIP</button>'
        +'</div>';
    }
    for(var f of r.files){
      var fp=path?(path+'/'+f.name):f.name;
      html+='<div style="display:flex;align-items:center;padding:0.35rem 0.5rem;border-bottom:1px solid rgba(128,128,128,0.1)">'
        +'<span style="margin-right:0.4rem">&#127925;</span>'
        +'<span style="flex:1;font-size:0.8rem">'+esc(f.name)+'</span>'
        +'<span style="font-size:0.7rem;color:var(--muted);margin-right:0.5rem">'+f.size_mb+' MB</span>'
        +'<a href="/api/export/download?path='+encodeURIComponent(fp)+'" download style="font-size:0.68rem;padding:0.15rem 0.4rem;background:var(--sage);color:var(--charcoal);border-radius:4px;text-decoration:none;font-weight:600">DL</a>'
        +'</div>';
    }
    list.innerHTML=html;
    // Show/hide download all button based on whether we have files
    document.getElementById('export-download-all-btn').style.display=r.folders.length||r.files.length?'':'none';
  }catch(e){loading.style.display='none';empty.style.display='block';content.style.display='none'}
}
function downloadAlbumZip(path){window.location='/api/export/download-album?path='+encodeURIComponent(path)}
function downloadAllExports(){window.location='/api/export/download-all'}

async function loadAudioDevices(){try{const d=await apiFetch('/api/audio-devices').then(r=>r.json());const sel=document.getElementById('audio-select');const saved=d.current_card?String(d.current_card):'';sel.innerHTML='<option value="">(none / default)</option>'+(d.devices||[]).map(dv=>`<option value="${esc(dv.card_id)}"${String(dv.card_id)===saved?' selected':''}>${esc(dv.name)}</option>`).join('')}catch(e){}}
function saveAudioDevice(){const v=document.getElementById('audio-select').value;apiFetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({audio_device_card:v===''?null:v})}).then(()=>showToast('Audio input saved')).catch(()=>showToast('Failed to save audio input'))}

// ── Bluetooth Management ──
async function scanBluetoothDevices(){
  var btn=document.getElementById('btn-bt-scan');
  var results=document.getElementById('bt-scan-results');
  btn.disabled=true;btn.textContent='Scanning…';
  results.innerHTML='<div style="color:var(--muted);font-size:0.82rem">Scanning for nearby devices (12s)…</div>';
  try{
    var d=await apiFetch('/api/bluetooth/scan').then(r=>r.json());
    if(!d.ok){results.innerHTML='<div style="color:var(--rust);font-size:0.82rem">'+(d.error||'Scan failed')+'</div>';return}
    var devs=(d.devices||[]).filter(dev=>!dev.paired);
    if(!devs.length){results.innerHTML='<div style="color:var(--muted);font-size:0.82rem">No new devices found. Make sure your speaker is in pairing mode.</div>';return}
    results.innerHTML=devs.map(dev=>'<div class="bt-device-item">'
      +'<span style="font-size:0.82rem;flex:1">'+esc(dev.name||dev.address)+'</span>'
      +'<button class="btn btn-accent" style="padding:0.2rem 0.5rem;font-size:0.72rem" onclick="pairBluetoothDevice(\''+dev.address+'\',\''+esc(dev.name||dev.address).replace(/'/g,"\\'")+'\')">Pair</button>'
      +'</div>').join('');
    loadBluetoothPaired();
  }catch(e){results.innerHTML='<div style="color:var(--rust);font-size:0.82rem">Error: '+e.message+'</div>'}
  finally{btn.disabled=false;btn.textContent='⟳ Scan for Bluetooth'}
}
async function pairBluetoothDevice(addr,name){
  showToast('Pairing with '+name+'…');
  try{
    var r=await apiFetch('/api/bluetooth/bt:'+addr+'/pair',{method:'POST'}).then(r=>r.json());
    if(r.ok){showToast('Paired: '+name);loadBluetoothPaired();document.getElementById('bt-scan-results').innerHTML=''}
    else showError(r.error||'Pairing failed');
  }catch(e){showError('Pairing error: '+e.message)}
}
async function connectBluetoothDevice(addr,name){
  showToast('Connecting to '+name+'…');
  try{
    var r=await apiFetch('/api/bluetooth/bt:'+addr+'/connect',{method:'POST'}).then(r=>r.json());
    if(r.ok){showToast('Connected: '+name);loadBluetoothPaired()}
    else showError(r.error||'Connection failed');
  }catch(e){showError('Connection error: '+e.message)}
}
async function disconnectBluetoothDevice(addr,name){
  try{
    var r=await apiFetch('/api/bluetooth/bt:'+addr+'/disconnect',{method:'POST'}).then(r=>r.json());
    if(r.ok){showToast('Disconnected: '+name);loadBluetoothPaired()}
    else showError(r.error||'Disconnect failed');
  }catch(e){showError('Error: '+e.message)}
}
async function removeBluetoothDevice(addr,name){
  if(!confirm('Remove '+name+'? You will need to pair again.'))return;
  try{
    var r=await apiFetch('/api/bluetooth/bt:'+addr+'/remove',{method:'POST'}).then(r=>r.json());
    if(r.ok){showToast('Removed: '+name);loadBluetoothPaired()}
    else showError(r.error||'Remove failed');
  }catch(e){showError('Error: '+e.message)}
}
async function loadBluetoothPaired(){
  var list=document.getElementById('bt-paired-list');
  try{
    var d=await apiFetch('/api/devices').then(r=>r.json());
    var btDevs=(d.devices||[]).filter(dev=>dev.type==='bluetooth');
    if(!btDevs.length){list.innerHTML='<div style="color:var(--muted);font-size:0.82rem">No paired Bluetooth devices</div>';return}
    var codecInfo=null;try{codecInfo=await apiFetch('/api/bluetooth/codec').then(r=>r.json())}catch(e){}
    var activeCodec=codecInfo&&codecInfo.codec?codecInfo.codec:null;
    list.innerHTML=btDevs.map(dev=>{
      var addr=dev.address||dev.id.replace('bt:','');
      var name=dev.custom_name||dev.name||addr;
      var conn=dev.connected;
      var codecBadge=conn&&activeCodec?'<span style="font-size:0.6rem;background:var(--amber);color:var(--bg);padding:0.05rem 0.3rem;border-radius:3px;font-weight:700;letter-spacing:0.02em">'+esc(activeCodec)+'</span>':'';
      return '<div class="bt-device-item">'
        +'<span style="font-size:0.82rem;flex:1">'+esc(name)+'</span>'
        +codecBadge
        +'<span class="bt-status '+(conn?'connected':'paired')+'">'+(conn?'Connected':'Paired')+'</span>'
        +(conn
          ?'<button class="btn btn-ghost" style="padding:0.15rem 0.4rem;font-size:0.68rem" onclick="disconnectBluetoothDevice(\''+addr+'\',\''+esc(name).replace(/'/g,"\\'")+'\')">Disconnect</button>'
          :'<button class="btn btn-accent" style="padding:0.15rem 0.4rem;font-size:0.68rem" onclick="connectBluetoothDevice(\''+addr+'\',\''+esc(name).replace(/'/g,"\\'")+'\')">Connect</button>')
        +'<button class="btn btn-ghost" style="padding:0.15rem 0.3rem;font-size:0.68rem;color:var(--rust)" onclick="removeBluetoothDevice(\''+addr+'\',\''+esc(name).replace(/'/g,"\\'")+'\')">✕</button>'
        +'</div>'
    }).join('');
  }catch(e){list.innerHTML='<div style="color:var(--rust);font-size:0.82rem">Failed to load</div>'}
}

async function init(){
  connectWS();await loadAudioDevices();
  const d=await apiFetch('/api/status').then(r=>r.json());
  setStatus(d);
  if(d.eq){applyEQValues(d.eq.bass,d.eq.treble,d.eq.volume);if(d.eq.bands)applyBandValues(d.eq.bands,d.eq.preset)}
  if(d.now_playing&&d.now_playing.track_title)renderNowPlaying(d.now_playing);
  if(d.player&&d.player.state!=='stopped')onPlayerStatus(d.player);
  if(d.settings){
    if(d.settings.discogs_token)document.getElementById('discogs-token').value=d.settings.discogs_token;
    if(d.settings.discogs_username)document.getElementById('discogs-username').value=d.settings.discogs_username;
    if(d.settings.saved_devices&&d.settings.saved_devices.length)_lastUsedDevice=d.settings.saved_devices[0].id;
    if(d.settings.auto_stream_enabled!==undefined){document.getElementById('auto-stream-enabled').checked=d.settings.auto_stream_enabled;if(d.settings.auto_stream_device){window._savedAutoStreamDevice=d.settings.auto_stream_device;scanAutoStreamDevices()}const st=document.getElementById('auto-stream-status');if(d.settings.auto_stream_enabled&&d.settings.auto_stream_device)st.textContent=`Auto-streaming to: ${d.settings.auto_stream_device.name}`}
    if(d.settings.http_stream_enabled!==undefined){document.getElementById('http-stream-enabled').checked=!!d.settings.http_stream_enabled}
    if(d.settings.http_stream_bitrate_kbps!==undefined){document.getElementById('http-stream-bitrate').value=String(d.settings.http_stream_bitrate_kbps)}
    if(d.settings.max_browser_listeners!==undefined){var mbl=document.getElementById('max-browser-listeners');if(mbl)mbl.value=String(d.settings.max_browser_listeners)}
    updateHttpStreamUI();
    if(d.settings.crossfade_secs!==undefined){document.getElementById('crossfade-slider').value=d.settings.crossfade_secs;updateCrossfadeLabel()}
    if(d.settings.audio_detect_threshold!==undefined){var _dt=document.getElementById('detect-threshold-slider');if(_dt){_dt.value=d.settings.audio_detect_threshold;updateDetectThresholdLabel()}}
    if(d.settings.device_volumes)window._deviceVolumes=d.settings.device_volumes;
    if(d.settings.app_name){document.getElementById('app-name').textContent=d.settings.app_name;document.title=d.settings.app_name}
    if(d.settings.rec_play_audio){_recPlayAudio=true;var sw=document.getElementById('rec-play-audio-switch');if(sw){sw.style.background='var(--sage)';sw.querySelector('span').style.transform='translateX(14px)'}}
    _eqAutoLoad=!!d.settings.eq_auto_load;setEqAutoLoadSwitch(_eqAutoLoad);
  }
  if(d.storage)updateStorageInfo(d.storage);
  if(d.album_recording){_recAlbumId=d.album_recording.album_id;_recSide=d.album_recording.side;if(!_recStartTime)_recStartTime=Date.now()}
  initSwipeGestures();
  loadCertInfo();
  loadExportStats();
  await loadCatalog();
  switchView('library');
}
/* boot via ensureAuth → startApp */

/* ── Screensaver / Screen Off ── */
var _ssActive = false, _ssLastNP = null;
var SS_IDLE_SECS = 600;
var _ssLastActivity = Date.now();
var _ssDebugLog = [];

function _ssLog(msg){
  var t = new Date().toLocaleTimeString();
  _ssDebugLog.push(t + ' ' + msg);
  if(_ssDebugLog.length > 50) _ssDebugLog.shift();
  console.log('[SS] ' + msg);
}

function _ssActivity(reason){
  _ssLastActivity = Date.now();
  _ssLog('activity: ' + reason);
  if(_ssActive) wakeSS();
}

function ssGoIdle(){
  if(_ssActive) return;
  // During an album recording, the screensaver shows the album being recorded,
  // not whatever played last (#58). Falls back to now-playing otherwise.
  var np=_ssLastNP;
  var recording=!!(_recStartTime&&_recAlbumId);
  if(recording){
    var ra=(_catalogAlbums||[]).find(function(a){return a.id===_recAlbumId});
    if(ra){
      var rart=ra.user_artwork_path||ra.artwork_path;
      np={track_title:ra.title,track_artist:ra.artist,
          album_title:'Recording Side '+(_recSide||'A'),
          artwork_url:rart?('/artwork/'+encodeURIComponent(String(rart).split('/').pop())):null,
          side:null};
    }
  }
  var playing = recording || _playerActive || _playerSource === 'vinyl';
  var ss = document.getElementById('screensaver');
  var content = document.getElementById('ss-content');
  _ssActive = true;
  _ssLog('going idle, playing=' + playing + ', recording=' + recording + ', hasNP=' + !!np);

  if(playing && np){
    ss.classList.remove('off');
    var art = document.getElementById('ss-art');
    if(np.artwork_url){
      art.outerHTML = '<img class="ss-art" id="ss-art" src="' + np.artwork_url + '?t=' + Date.now() + '" alt="" style="border-radius:50%;animation:ss-spin 8s linear infinite">';
    } else {
      art.outerHTML = '<div class="ss-art-placeholder" id="ss-art"></div>';
    }
    document.getElementById('ss-track').textContent = np.track_title || '';
    document.getElementById('ss-artist-album').textContent = [np.track_artist||np.album_artist||'', np.album_title||''].filter(Boolean).join(' - ');
    document.getElementById('ss-side').textContent = np.side ? 'Side ' + np.side : '';
    var viz = document.getElementById('ss-eq-viz');
    viz.innerHTML = '';
    for(var i = 0; i < 16; i++){
      var bar = document.createElement('div');
      bar.className = 'ss-eq-bar';
      bar.style.animationDelay = (i * 0.08) + 's';
      bar.style.animationDuration = (0.6 + Math.random() * 0.6) + 's';
      viz.appendChild(bar);
    }
    content.style.display = 'flex';
  } else {
    ss.classList.add('off');
    content.style.display = 'none';
    var _hint = document.getElementById('ss-hint-title');
    var _appName = document.getElementById('app-name');
    if(_hint && _appName && _appName.textContent.trim()) _hint.textContent = _appName.textContent.trim();
  }
  ss.classList.add('active');
}

function wakeSS(){
  if(!_ssActive) return;
  _ssLog('waking');
  _ssActive = false;
  var ss = document.getElementById('screensaver');
  ss.classList.remove('active','off');
  _ssLastActivity = Date.now();
}

function ssOnNowPlaying(d){
  if(d && d.track_title) _ssLastNP = d;
  else if(!_playerActive) _ssLastNP = null;
  if(_ssActive && _ssLastNP){
    document.getElementById('ss-track').textContent = _ssLastNP.track_title || '';
    document.getElementById('ss-artist-album').textContent = [_ssLastNP.track_artist||_ssLastNP.album_artist||'', _ssLastNP.album_title||''].filter(Boolean).join(' - ');
    document.getElementById('ss-side').textContent = _ssLastNP.side ? 'Side ' + _ssLastNP.side : '';
    var art = document.getElementById('ss-art');
    if(_ssLastNP.artwork_url && art && !art.src){
      art.outerHTML = '<img class="ss-art" id="ss-art" src="' + _ssLastNP.artwork_url + '?t=' + Date.now() + '" alt="" style="border-radius:50%;animation:ss-spin 8s linear infinite">';
    }
  }
}

function ssOnPlayerStatus(d){
  if(d.state === 'stopped' && _ssActive){
    var ss = document.getElementById('screensaver');
    ss.classList.add('off');
    document.getElementById('ss-content').style.display = 'none';
  }
  if(_ssActive && d.position_secs !== undefined){
    var pos = d.position_secs || 0, dur = d.duration_secs || 1;
    document.getElementById('ss-progress-fill').style.width = Math.min(100, (pos/dur)*100) + '%';
    document.getElementById('ss-time-cur').textContent = fmtTime(pos);
    document.getElementById('ss-time-dur').textContent = '-' + fmtTime(Math.max(0, dur - pos));
  }
}

// ACTIVITY DETECTION: Only high-level events that phantom hardware can't generate
// 'click' requires a full tap-and-release cycle — immune to phantom touchstart spam
document.addEventListener('click', function(e){
  // Don't count clicks on the screensaver itself (handled by wakeSS)
  if(!document.getElementById('screensaver').contains(e.target)){
    _ssActivity('click');
  }
}, {capture:true, passive:true});

// Input/change events — only fire on real user typing or slider moves
document.addEventListener('input', function(){ _ssActivity('input'); }, {passive:true});

// Scroll on main content
(function(){
  var mc = document.querySelector('.album-grid-wrap');
  if(mc) mc.addEventListener('scroll', function(){ _ssActivity('scroll'); }, {passive:true});
})();

// Poll every 5 seconds to check if idle threshold reached
var _ssIntervalId = setInterval(function(){
  if(_ssActive) return;
  var elapsed = Math.round((Date.now() - _ssLastActivity) / 1000);
  if(elapsed >= SS_IDLE_SECS){
    _ssLog('idle threshold reached (' + elapsed + 's)');
    ssGoIdle();
  }
}, 5000);
_ssLog('screensaver initialized, interval=' + _ssIntervalId);

/* ── Keyboard Shortcuts ── */
document.addEventListener('keydown', function(e){
  // Skip if typing in an input
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA'||e.target.tagName==='SELECT') return;
  if(e.key===' '&&_playerActive){e.preventDefault();playerToggle()}
  else if(e.key==='ArrowRight'&&_playerActive){e.preventDefault();playerNext()}
  else if(e.key==='ArrowLeft'&&_playerActive){e.preventDefault();playerPrev()}
  else if(e.key==='q'||e.key==='Q'){e.preventDefault();toggleQueue()}
  else if(e.key==='s'||e.key==='S'){e.preventDefault();toggleSortPanel()}
  else if(e.key==='p'||e.key==='P'){e.preventDefault();togglePlaylistsPanel()}
  else if(e.key==='Escape'){
    if(_playlistsPanelOpen)togglePlaylistsPanel();
    else if(_queuePanelOpen)toggleQueue();
    else if(document.getElementById('sort-panel').classList.contains('open'))closeSortPanel();
    else if(document.getElementById('album-modal').classList.contains('open'))closeModal();
    else if(document.getElementById('settings-modal').classList.contains('open'))hideSettings();
    else if(document.getElementById('stats-modal').classList.contains('open'))hideStats();
  }
});

/* ── On-Screen Keyboard ── */
(function(){
  const NUM_ROW = ['1','2','3','4','5','6','7','8','9','0'];
  const ROW1 = ['q','w','e','r','t','y','u','i','o','p'];
  const ROW2 = ['a','s','d','f','g','h','j','k','l'];
  const ROW3 = ['z','x','c','v','b','n','m'];
  let _oskTarget = null;
  let _oskShift = false;
  let _oskVisible = false;

  function makeKey(label, action, cls){
    const k = document.createElement('button');
    k.className = 'osk-key' + (cls ? ' '+cls : '');
    k.textContent = label;
    k.setAttribute('data-action', action || label);
    k.addEventListener('touchstart', function(e){ e.preventDefault(); onKey(this); }, {passive:false});
    k.addEventListener('mousedown', function(e){ e.preventDefault(); onKey(this); });
    return k;
  }

  function buildKeyboard(){
    const rn = document.getElementById('osk-row-num');
    NUM_ROW.forEach(c => rn.appendChild(makeKey(c, c)));
    rn.appendChild(makeKey('⌫', 'backspace', 'wide'));

    const r1 = document.getElementById('osk-row-1');
    ROW1.forEach(c => r1.appendChild(makeKey(c, c)));

    const r2 = document.getElementById('osk-row-2');
    r2.appendChild(makeKey('⇧', 'shift', 'wide'));
    ROW2.forEach(c => r2.appendChild(makeKey(c, c)));
    r2.appendChild(makeKey("'", "'"));

    const r3 = document.getElementById('osk-row-3');
    r3.appendChild(makeKey('123', 'symbols', 'wider'));
    ROW3.forEach(c => r3.appendChild(makeKey(c, c)));
    r3.appendChild(makeKey('.', '.'));
    r3.appendChild(makeKey(',', ','));

    const rb = document.getElementById('osk-row-bottom');
    rb.appendChild(makeKey('-', '-'));
    rb.appendChild(makeKey('space', ' ', 'space'));
    rb.appendChild(makeKey('Done', 'done', 'done'));
  }

  const SYM_NUM = ['!','@','#','$','%','^','&','*','(',')'];
  const SYM_R1  = ['-','+','=','_','/',':',';','(',')','\\'];
  const SYM_R2  = ['~','`','|','{','}','[',']','"'];
  let _symbolMode = false;

  function setSymbolMode(on){
    _symbolMode = on;
    const rn = document.getElementById('osk-row-num');
    const r1 = document.getElementById('osk-row-1');
    const r2 = document.getElementById('osk-row-2');
    const r3 = document.getElementById('osk-row-3');
    const src = on ? SYM_NUM : NUM_ROW;
    [...rn.children].forEach((k,i) => { if(i < src.length){ k.textContent = src[i]; k.setAttribute('data-action', src[i]); }});
    const src1 = on ? SYM_R1 : ROW1;
    [...r1.children].forEach((k,i) => { if(i < src1.length){ const c = on ? src1[i] : applyCase(src1[i]); k.textContent = c; k.setAttribute('data-action', on ? src1[i] : src1[i]); }});
    const keys2 = [...r2.children].filter(k => k.getAttribute('data-action') !== 'shift');
    const src2 = on ? SYM_R2 : ROW2;
    keys2.forEach((k,i) => { if(i < src2.length){ const c = on ? src2[i] : applyCase(src2[i]); k.textContent = c; k.setAttribute('data-action', on ? src2[i] : src2[i]); } else { k.textContent = on ? '' : "'"; k.setAttribute('data-action', on ? '' : "'"); }});
    const symBtn = [...r3.children].find(k => k.getAttribute('data-action') === 'symbols' || k.getAttribute('data-action') === 'abc');
    if(symBtn){ symBtn.textContent = on ? 'abc' : '123'; symBtn.setAttribute('data-action', on ? 'abc' : 'symbols'); }
    const letterKeys = [...r3.children].filter(k => { const a = k.getAttribute('data-action'); return a && a !== 'symbols' && a !== 'abc' && a !== 'Backspace' && a !== 'shift'; });
    const src3 = on ? ['?','!','.',','] : ROW3.concat(['.', ',']);
    letterKeys.forEach((k,i) => { if(i < src3.length){ const c = on ? src3[i] : applyCase(src3[i]); k.textContent = c; k.setAttribute('data-action', src3[i]); k.style.display = ''; } else { k.style.display = 'none'; }});
  }

  function applyCase(c){
    return _oskShift ? c.toUpperCase() : c.toLowerCase();
  }

  function updateDisplay(){
    const rows = [document.getElementById('osk-row-1'), document.getElementById('osk-row-2'), document.getElementById('osk-row-3')];
    if(_symbolMode) return;
    rows.forEach(row => {
      [...row.children].forEach(k => {
        const a = k.getAttribute('data-action');
        if(a && a.length === 1 && a.match(/[a-z]/i)){
          k.textContent = _oskShift ? a.toUpperCase() : a.toLowerCase();
        }
      });
    });
    const shiftBtn = document.querySelector('.osk-key[data-action="shift"]');
    if(shiftBtn) shiftBtn.classList.toggle('shift-active', _oskShift);
  }

  function updatePreview(){
    if(!_oskTarget) return;
    document.getElementById('osk-value').textContent = _oskTarget.value || '';
  }

  function onKey(btn){
    const action = btn.getAttribute('data-action');
    if(!action || !_oskTarget) return;
    btn.classList.add('active');
    setTimeout(() => btn.classList.remove('active'), 120);

    if(action === 'done'){
      hideOSK(); return;
    }
    if(action === 'shift'){
      _oskShift = !_oskShift; updateDisplay(); return;
    }
    if(action === 'symbols'){
      setSymbolMode(true); return;
    }
    if(action === 'abc'){
      setSymbolMode(false); updateDisplay(); return;
    }
    if(action === 'backspace'){
      const s = _oskTarget.selectionStart, e = _oskTarget.selectionEnd;
      if(s !== e){
        _oskTarget.value = _oskTarget.value.slice(0, s) + _oskTarget.value.slice(e);
        _oskTarget.selectionStart = _oskTarget.selectionEnd = s;
      } else if(s > 0){
        _oskTarget.value = _oskTarget.value.slice(0, s-1) + _oskTarget.value.slice(s);
        _oskTarget.selectionStart = _oskTarget.selectionEnd = s-1;
      }
      fireInput(); updatePreview(); return;
    }
    // Regular character
    const ch = (action.length === 1 && action.match(/[a-z]/i)) ? (_oskShift ? action.toUpperCase() : action) : action;
    const s = _oskTarget.selectionStart, e = _oskTarget.selectionEnd;
    _oskTarget.value = _oskTarget.value.slice(0, s) + ch + _oskTarget.value.slice(e);
    _oskTarget.selectionStart = _oskTarget.selectionEnd = s + ch.length;
    if(_oskShift && action.match(/[a-z]/i)){ _oskShift = false; updateDisplay(); }
    fireInput(); updatePreview();
  }

  // Mobile UX Features Implementation

  // Pull-to-Refresh
  let _pullStartY=0, _pullOffset=0;
  const albumGridWrap=document.getElementById('album-grid-wrap');
  if(albumGridWrap) {
    albumGridWrap.addEventListener('touchstart', e => {
      if(albumGridWrap.scrollTop===0) {
        _pullStartY=e.touches[0].clientY;
      }
    }, {passive: true});

    albumGridWrap.addEventListener('touchmove', e => {
      if(albumGridWrap.scrollTop===0 && _pullStartY > 0) {
        _pullOffset=e.touches[0].clientY-_pullStartY;
        if(_pullOffset > 0) {
          const indicator=document.getElementById('pull-indicator');
          indicator.style.opacity=Math.min(_pullOffset/100, 1);
          indicator.style.transform=`translateX(-50%) translateY(${_pullOffset-50}px)`;
          if(_pullOffset > 100) {
            indicator.classList.add('show');
          }
        }
      }
    }, {passive: true});

    albumGridWrap.addEventListener('touchend', e => {
      if(_pullOffset > 80) {
        const indicator=document.getElementById('pull-indicator');
        indicator.classList.add('refreshing');
        loadCatalog().then(() => {
          indicator.classList.remove('refreshing');
          indicator.classList.remove('show');
          indicator.style.opacity='0';
        });
      }
      _pullStartY=0;
      _pullOffset=0;
    }, {passive: true});
  }

  // Long-Press Context Menu
  let _longPressTimer=null, _longPressTarget=null;
  document.addEventListener('pointerdown', e => {
    const card=e.target.closest('.album-card');
    if(!card) return;
    _longPressTarget=card;
    _longPressTimer=setTimeout(() => {
      showContextMenu(card, e);
    }, 500);
  }, {passive: true});

  document.addEventListener('pointerup', () => {
    if(_longPressTimer) {
      clearTimeout(_longPressTimer);
      _longPressTimer=null;
    }
    _longPressTarget=null;
  }, {passive: true});

  function showContextMenu(card, event) {
    const albumId=parseInt(card.dataset.albumId);
    const album=_catalogAlbums.find(a => a.id===albumId);
    if(!album) return;

    const hasAudio=album.has_audio||false;
    const isFav=album.favorite||false;

    const menu=document.getElementById('context-menu');
    let html='';

    if(hasAudio) {
      html+=`<button class="context-menu-item" onclick="playAlbumFromContext(${albumId});closeContextMenu()">Play</button>`;
      html+=`<button class="context-menu-item" onclick="queueAlbumFromContext(${albumId});closeContextMenu()">Queue</button>`;
    }

    html+=`<button class="context-menu-item has-submenu">Add to Playlist
      <div class="context-submenu">`;
    if(window._playlists && window._playlists.length > 0) {
      window._playlists.forEach(pl => {
        html+=`<button class="context-menu-item" onclick="addAlbumToPlaylist(${albumId}, ${pl.id});closeContextMenu()"> ${esc(pl.name)}</button>`;
      });
    }
    html+=`<button class="context-menu-item" onclick="createPlaylistForAlbum(${albumId})">+ New Playlist</button>
      </div>
    </button>`;

    html+=`<button class="context-menu-item" onclick="toggleFavoriteFromContext(${albumId}, ${!isFav});closeContextMenu()">${isFav?'Unfavorite':'Favorite'}</button>`;
    html+=`<button class="context-menu-item" onclick="openAlbumDetail(${albumId});closeContextMenu()">Album Details</button>`;

    menu.innerHTML=html;
    menu.style.display='block';
    menu.style.left=event.clientX+'px';
    menu.style.top=event.clientY+'px';
  }

  window.closeContextMenu=function() {
    const menu=document.getElementById('context-menu');
    menu.style.display='none';
  };

  window.playAlbumFromContext=function(albumId) {
    if(_playerActive||_isStreaming) {
      playAlbum(albumId);
    } else {
      _pendingAlbumId=albumId;
      showOutputPicker(albumId);
    }
  };

  window.queueAlbumFromContext=function(albumId) {
    if(!_playerActive) {
      showError('No active playback');
      return;
    }
    apiFetch('/api/player/queue/insert-next',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({album_id:albumId})
    }).then(r=>r.json()).then(d=>{
      if(d.ok) showToast(`Added to queue`);
      else showError(d.error||'Failed');
    });
  };

  window.toggleFavoriteFromContext=function(albumId, fav) {
    const album=_catalogAlbums.find(a=>a.id===albumId);
    if(!album) return;
    album.favorite=fav;
    apiFetch('/api/catalog/'+albumId+'/favorite',{
      method:'PUT',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({favorite:fav})
    });
    renderCatalog(_catalogAlbums);
  };

  document.addEventListener('click', e => {
    const menu=document.getElementById('context-menu');
    if(menu.style.display==='block' && !menu.contains(e.target)) {
      closeContextMenu();
    }
  });

  // Stubs for removed fullscreen now-playing (referenced elsewhere)
  window.openNowPlayingFullscreen=function(){};
  window.closeNowPlayingFullscreen=function(){};

  // Barcode Scanner (using html5-qrcode library for broad mobile support)
  var _html5QrCode=null;

  window.openBarcodeScanner=function() {
    if(typeof Html5Qrcode === 'undefined') {
      showError('Barcode scanner library failed to load. Check your connection.');
      return;
    }

    var scanner=document.getElementById('barcode-scanner');
    scanner.classList.add('open');
    document.getElementById('barcode-status').textContent='Starting camera...';

    _html5QrCode=new Html5Qrcode('barcode-reader');
    _html5QrCode.start(
      {facingMode: 'environment'},
      {fps: 10, qrbox: {width: 250, height: 150}, formatsToSupport: [
        Html5QrcodeSupportedFormats.EAN_13,
        Html5QrcodeSupportedFormats.EAN_8,
        Html5QrcodeSupportedFormats.UPC_A,
        Html5QrcodeSupportedFormats.UPC_E,
        Html5QrcodeSupportedFormats.CODE_128,
        Html5QrcodeSupportedFormats.CODE_39
      ]},
      function onScanSuccess(decodedText) {
        handleBarcodeDetected(decodedText);
      },
      function onScanFailure() {
        // Scan miss, keep trying
      }
    ).then(function() {
      document.getElementById('barcode-status').textContent='Point camera at barcode...';
    }).catch(function(err) {
      console.log('Camera error:', err);
      showError('Camera permission denied or not available');
      closeBarcodeScanner();
    });
  };

  function handleBarcodeDetected(barcode) {
    showToast('Barcode detected: '+barcode);
    document.getElementById('s-barcode').value=barcode;
    closeBarcodeScanner();
    doSearch();
  }

  window.closeBarcodeScanner=function() {
    var scanner=document.getElementById('barcode-scanner');
    scanner.classList.remove('open');
    if(_html5QrCode) {
      _html5QrCode.stop().then(function() {
        _html5QrCode.clear();
        _html5QrCode=null;
      }).catch(function() {
        _html5QrCode=null;
      });
    }
  };

  function fireInput(){
    if(!_oskTarget) return;
    _oskTarget.dispatchEvent(new Event('input', {bubbles:true}));
    _oskTarget.dispatchEvent(new Event('change', {bubbles:true}));
  }

  function getLabel(el){
    const p = el.closest('.form-row');
    if(p){ const lbl = p.querySelector('label'); if(lbl) return lbl.textContent.replace(/[*:]/g,'').trim(); }
    if(el.placeholder) return el.placeholder;
    if(el.id === 'catalog-search') return 'Search';
    return 'Input';
  }

  function showOSK(target){
    if(_oskVisible && _oskTarget === target) return;
    _oskTarget = target;
    _oskShift = false; _symbolMode = false;
    setSymbolMode(false); updateDisplay();
    document.getElementById('osk-label').textContent = getLabel(target);
    updatePreview();
    const osk = document.getElementById('osk');
    osk.classList.add('opening');
    requestAnimationFrame(() => { osk.classList.add('open'); });
    document.body.classList.add('osk-active');
    _oskVisible = true;
    // Shrink frame to make room
    const oskH = osk.offsetHeight || 240;
    document.querySelector('.frame').style.height = `calc(100dvh - 12px - ${oskH}px)`;
    // Scroll input into view after a tick
    setTimeout(() => {
      target.scrollIntoView({behavior:'smooth', block:'center'});
    }, 100);
  }

  function hideOSK(){
    if(!_oskVisible) return;
    const osk = document.getElementById('osk');
    osk.classList.remove('open');
    setTimeout(() => { osk.classList.remove('opening'); }, 200);
    document.body.classList.remove('osk-active');
    document.querySelector('.frame').style.height = '';
    _oskVisible = false;
    if(_oskTarget){ _oskTarget.blur(); }
    _oskTarget = null;
  }
  window.hideOSK = hideOSK;

  // Intercept focus on text inputs
  // The built-in OSK is meant for the dedicated Pi touchscreen (a short
  // ~1024x600 display with no physical keyboard). On tablets and phones it
  // just gets in the way of the native keyboard, so auto mode only enables it
  // on that kiosk profile. Users can force on/off in Settings.
  //
  // The kiosk browser is the only client that loads the app from localhost,
  // which makes that the reliable signal. The two things tried before are not:
  // screen size alone also matches a phone held in landscape, and
  // `pointer: coarse` silently flips to fine the moment a wireless keyboard
  // or mouse dongle is plugged into the Pi, which turned the on-screen
  // keyboard off on the kiosk itself. `any-pointer: coarse` still confirms a
  // touchscreen is present without caring what else is attached.
  function isKioskDisplay(){
    const h = location.hostname;
    const onKioskHost = (h === 'localhost' || h === '127.0.0.1' || h === '[::1]');
    if(!onKioskHost) return false;
    return window.matchMedia('(max-width: 1024px) and (max-height: 640px)').matches
        && window.matchMedia('(any-pointer: coarse)').matches;
  }
  function oskMode(){
    try{ return localStorage.getItem('vinyl_osk_mode') || 'auto'; }catch(_e){ return 'auto'; }
  }
  function oskEnabled(){
    const m = oskMode();
    if(m === 'on') return true;
    if(m === 'off') return false;
    return isKioskDisplay();
  }
  window.oskEnabled = oskEnabled;

  function shouldShowOSK(el){
    if(!oskEnabled()) return false;
    if(!el || !el.tagName) return false;
    const tag = el.tagName.toLowerCase();
    const type = (el.type||'').toLowerCase();
    if(tag === 'textarea') return true;
    if(tag === 'input' && ['text','search','password','email','url','tel','number'].includes(type)) return true;
    return false;
  }

  document.addEventListener('focusin', function(e){
    if(shouldShowOSK(e.target)){
      // Prevent native keyboard on touch devices
      e.target.setAttribute('readonly','');
      setTimeout(() => {
        e.target.removeAttribute('readonly');
        showOSK(e.target);
      }, 50);
    }
  });

  // Hide on outside interaction (touch, pen, mouse).
  // Consume the event so it does not activate the tapped element underneath.
  let _oskDismissTapUntil = 0;
  document.addEventListener('pointerdown', function(e){
    if(!_oskVisible) return;
    const osk = document.getElementById('osk');
    if(osk && osk.contains(e.target)) return;
    if(shouldShowOSK(e.target)) return;
    _oskDismissTapUntil = Date.now() + 450;
    e.preventDefault();
    e.stopPropagation();
    hideOSK();
  }, {capture:true});

  // Some browsers still emit a synthetic click after touchstart;
  // swallow it briefly after dismissing the OSK.
  document.addEventListener('click', function(e){
    if(Date.now() > _oskDismissTapUntil) return;
    _oskDismissTapUntil = 0;
    if(shouldShowOSK(e.target)) return;
    const osk = document.getElementById('osk');
    if(osk && osk.contains(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
  }, true);

  buildKeyboard();
})();



/* ── Header overflow / Manage menus ── */
function toggleHeaderMenu(e){
  if(e){e.stopPropagation()}
  var m=document.getElementById('header-menu');
  if(!m)return;
  var open=m.classList.toggle('open');
  var btn=document.getElementById('btn-header-more');
  if(btn)btn.setAttribute('aria-expanded', open?'true':'false');
  closeManageMenu();
}
function closeHeaderMenu(){
  var m=document.getElementById('header-menu');
  if(m)m.classList.remove('open');
  var btn=document.getElementById('btn-header-more');
  if(btn)btn.setAttribute('aria-expanded','false');
}
function toggleManageMenu(e){
  if(e){e.stopPropagation()}
  var m=document.getElementById('modal-manage-menu');
  if(!m)return;
  var open=m.classList.toggle('open');
  var btn=document.getElementById('btn-modal-manage');
  if(btn)btn.setAttribute('aria-expanded', open?'true':'false');
  closeHeaderMenu();
}
function closeManageMenu(){
  var m=document.getElementById('modal-manage-menu');
  if(m)m.classList.remove('open');
  var btn=document.getElementById('btn-modal-manage');
  if(btn)btn.setAttribute('aria-expanded','false');
}
document.addEventListener('click', function(){
  closeHeaderMenu();
  closeManageMenu();
});
document.addEventListener('keydown', function(ev){
  if(ev.key==='Escape'){ closeHeaderMenu(); closeManageMenu(); }
});

function startApp(){
  if(window.__vsAppStarted)return;
  window.__vsAppStarted=true;
  init();
}

/* ── Additional features (was post-</html> script) ── */
// Additional features for Stats QoL

// Feature 2: Duplicate Detection
window.showDuplicates = async function(){
  try{
    const r = await apiFetch('/api/catalog/duplicates').then(r => r.json());
    const groups = r.duplicate_groups || [];
    const container = document.getElementById('duplicates-list');
    if(!groups.length){
      container.innerHTML = '<div style="color:var(--muted);font-size:0.82rem;padding:1rem">No duplicates found</div>';
    } else {
      container.innerHTML = groups.map(group => `
        <div style="border:1px solid rgba(0,0,0,0.1);border-radius:8px;padding:1rem">
          <div style="font-weight:600;margin-bottom:0.5rem;color:var(--amber-dk)">Duplicate: ${group[0].title} by ${group[0].artist}</div>
          <div style="display:flex;gap:0.5rem;flex-wrap:wrap">
            ${group.map(a => `<div style="flex:1;min-width:120px;text-align:center;padding:0.5rem;background:var(--paper);border-radius:4px;cursor:pointer" onclick="statsJumpToAlbum(${a.id})"><div style="font-size:0.82rem;font-weight:600">${esc(a.title)}</div><div style="font-size:0.72rem;color:var(--muted)">${esc(a.artist)}</div></div>`).join('')}
          </div>
        </div>
      `).join('');
    }
    document.getElementById('duplicates-modal').classList.add('open');
  } catch(e){
    showError('Failed to load duplicates: ' + e.message);
  }
};

window.hideDuplicates = function(){
  document.getElementById('duplicates-modal').classList.remove('open');
};

// Feature 3: Enhanced Stats Rendering

window.renderHeatmap = function(heatmap){
  const container = document.getElementById('heatmap-container');
  const today = new Date();
  const startDate = new Date(today);
  startDate.setDate(startDate.getDate() - 180);
  const maxCount = Math.max(...Object.values(heatmap || {}), 1);
  let html = '';
  for(let d = new Date(startDate); d <= today; d.setDate(d.getDate() + 1)){
    const dateStr = d.toISOString().split('T')[0];
    const count = heatmap[dateStr] || 0;
    const intensity = count > 0 ? count / maxCount : 0;
    const color = `rgba(212,162,78,${0.2 + intensity * 0.8})`;
    html += `<div style="width:100%;height:20px;background:${color};border-radius:3px;border:1px solid rgba(212,162,78,0.3)" title="${dateStr}: ${count} plays"></div>`;
  }
  container.innerHTML = html;
};

window.renderGenreChart = function(genres){
  const container = document.getElementById('genre-bars-container');
  if(!genres.length){
    container.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">No genres</div>';
    return;
  }
  const maxCount = Math.max(...genres.map(g => g.count), 1);
  const html = genres.map(g => {
    const percent = (g.count / maxCount) * 100;
    return `<div style="margin-bottom:0.6rem"><div style="display:flex;justify-content:space-between;margin-bottom:0.2rem"><span style="font-size:0.82rem">${esc(g.genre || 'Unknown')}</span><span style="font-size:0.75rem;color:var(--amber-dk)">${g.count}</span></div><div style="height:24px;background:var(--paper-dk);border-radius:4px;overflow:hidden"><div style="height:100%;width:${percent}%;background:linear-gradient(90deg,var(--amber-dk),var(--amber));transition:width 0.3s"></div></div></div>`;
  }).join('');
  container.innerHTML = html;
};

window.renderArtistChart = function(artists){
  const container = document.getElementById('artist-bars-container');
  if(!artists.length){
    container.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">No artists</div>';
    return;
  }
  const maxCount = Math.max(...artists.map(a => a.album_count), 1);
  const html = artists.map(a => {
    const percent = (a.album_count / maxCount) * 100;
    return `<div style="margin-bottom:0.6rem"><div style="display:flex;justify-content:space-between;margin-bottom:0.2rem"><span style="font-size:0.82rem">${esc(a.artist || 'Unknown')}</span><span style="font-size:0.75rem;color:var(--amber-dk)">${a.album_count} albums</span></div><div style="height:24px;background:var(--paper-dk);border-radius:4px;overflow:hidden"><div style="height:100%;width:${percent}%;background:linear-gradient(90deg,var(--leather),var(--amber-dk));transition:width 0.3s"></div></div></div>`;
  }).join('');
  container.innerHTML = html;
};

window.renderDecadeChart = function(decades){
  const container = document.getElementById('decade-bars-container');
  if(!decades.length){
    container.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">No decades</div>';
    return;
  }
  const maxCount = Math.max(...decades.map(d => d.count), 1);
  const html = decades.map(d => {
    const percent = (d.count / maxCount) * 100;
    return `<div style="margin-bottom:0.6rem"><div style="display:flex;justify-content:space-between;margin-bottom:0.2rem"><span style="font-size:0.82rem">${esc(d.decade)}</span><span style="font-size:0.75rem;color:var(--amber-dk)">${d.count}</span></div><div style="height:24px;background:var(--paper-dk);border-radius:4px;overflow:hidden"><div style="height:100%;width:${percent}%;background:linear-gradient(90deg,var(--sage-dk),var(--sage));transition:width 0.3s"></div></div></div>`;
  }).join('');
  container.innerHTML = html;
};

window.renderWeeklyChart = function(weeks){
  const container = document.getElementById('weekly-bars-container');
  if(!weeks.length){
    container.innerHTML = '<div style="color:var(--muted);font-size:0.82rem">No data</div>';
    return;
  }
  const maxCount = Math.max(...weeks.map(w => w.count), 1);
  const html = weeks.map(w => {
    const percent = (w.count / maxCount) * 100;
    const weekDate = new Date(w.week_start);
    const weekStr = weekDate.toLocaleDateString('en-US', {month:'short', day:'numeric'});
    return `<div style="margin-bottom:0.6rem"><div style="display:flex;justify-content:space-between;margin-bottom:0.2rem"><span style="font-size:0.82rem">${weekStr}</span><span style="font-size:0.75rem;color:var(--amber-dk)">${w.count}</span></div><div style="height:24px;background:var(--paper-dk);border-radius:4px;overflow:hidden"><div style="height:100%;width:${percent}%;background:linear-gradient(90deg,var(--teal),var(--sage));transition:width 0.3s"></div></div></div>`;
  }).join('');
  container.innerHTML = html;
};

window.renderOnThisDay = function(albums){
  const container = document.getElementById('on-this-day-container');
  if(!albums.length){
    container.innerHTML = '<div style="color:var(--muted);font-size:0.82rem;padding:0.5rem 0">Nothing played on this date in previous years</div>';
    return;
  }
  const html = albums.map(a => {
    const art = a.user_artwork_path || a.artwork_path;
    const artHtml = art ? `<img src="/artwork/${art.split('/').pop()}" style="width:50px;height:50px;border-radius:4px;object-fit:cover;border:1px solid rgba(0,0,0,0.1)">` : `<div style="width:50px;height:50px;border-radius:4px;background:var(--paper-dk);display:flex;align-items:center;justify-content:center;font-size:1.2rem">💿</div>`;
    const playedDate = new Date(a.played_at);
    const year = playedDate.getFullYear();
    const today = new Date();
    const yearsAgo = today.getFullYear() - year;
    return `<div style="display:flex;align-items:center;gap:0.5rem;padding:0.5rem;background:var(--paper);border-radius:6px;cursor:pointer;border:1px solid rgba(0,0,0,0.06)" onclick="statsJumpToAlbum(${a.id})"><div style="overflow:hidden;border-radius:4px">${artHtml}</div><div style="flex:1;min-width:0"><div style="font-size:0.85rem;font-weight:600">${esc(a.title)}</div><div style="font-size:0.75rem;color:var(--muted)">${esc(a.artist)}</div><div style="font-size:0.72rem;color:var(--amber-dk)">${yearsAgo} year${yearsAgo !== 1 ? 's' : ''} ago</div></div></div>`;
  }).join('');
  container.innerHTML = html;
};

// Feature 4: Player Status Restoration
window.restorePlayerStatus = async function(){
  try{
    const r = await apiFetch('/api/player/status').then(r => r.json());
    if(r.state === 'playing' || r.state === 'paused'){
      console.log('Player status restored:', r.state);
    }
  } catch(e){}
};

// Feature 5: Keyboard Shortcuts
window.showShortcuts = function(){
  const shortcuts = [
    ['Space', 'Play/Pause'],
    ['N', 'Next track'],
    ['P', 'Playlists'],
    ['Q', 'Queue'],
    ['S', 'Settings'],
    ['F', 'Search'],
    ['Escape', 'Close modal'],
    ['?', 'Show shortcuts'],
    ['Up/Down', 'Volume'],
  ];
  const grid = document.getElementById('shortcuts-grid');
  grid.innerHTML = shortcuts.map(([key, desc]) => `
    <div style="padding:0.5rem;background:var(--paper);border-radius:4px;border:1px solid rgba(0,0,0,0.06)">
      <div style="font-weight:700;color:var(--amber-dk);font-size:0.85rem;margin-bottom:0.2rem">${key}</div>
      <div style="font-size:0.75rem;color:var(--muted)">${desc}</div>
    </div>
  `).join('');
  document.getElementById('shortcuts-modal').classList.add('open');
};

window.hideShortcuts = function(){
  document.getElementById('shortcuts-modal').classList.remove('open');
};

// Feature 6: Undo for Destructive Actions
let _undoTimeout = null;
let _lastDeletedAlbumId = null;

window.showUndoToast = function(albumId){
  clearTimeout(_undoTimeout);
  _lastDeletedAlbumId = albumId;
  const toast = document.getElementById('toast-msg');
  toast.innerHTML = `Album deleted <button style="margin-left:0.5rem;padding:0.2rem 0.6rem;background:var(--amber-dk);color:var(--walnut);border:none;border-radius:3px;cursor:pointer;font-size:0.78rem" onclick="undoDelete()">Undo</button>`;
  toast.style.display = 'block';
  _undoTimeout = setTimeout(() => {
    toast.style.display = 'none';
    _lastDeletedAlbumId = null;
  }, 5000);
};

window.undoDelete = async function(){
  if(!_lastDeletedAlbumId) return;
  clearTimeout(_undoTimeout);
  try{
    const r = await apiFetch(`/api/catalog/${_lastDeletedAlbumId}/restore`, {
      method: 'POST'
    }).then(r => r.json());
    if(r.ok){
      showToast('Restored');
      loadCatalog();
    }
  } catch(e){}
  _lastDeletedAlbumId = null;
};

// Update the loadStats function to include new charts
const _originalLoadStats = window.loadStats;
window.loadStats = async function(){
  try{
    const r = await apiFetch('/api/catalog/stats').then(r => r.json());
    document.getElementById('stat-plays').textContent = r.total_plays || 0;
    document.getElementById('stat-albums').textContent = r.total_albums || 0;
    document.getElementById('stat-tracks').textContent = r.total_tracks || 0;
    document.getElementById('stat-hours').textContent = (r.total_listening_hours || 0).toFixed(1);
    renderTopAlbums(r.top_albums || []);
    renderTopTracks(r.top_tracks || []);
    renderRecentPlays(r.recent_plays || []);

    try{ const hm = await apiFetch('/api/catalog/heatmap').then(r => r.json()); renderHeatmap(hm.heatmap || {}); } catch(e){}
    try{ const gen = await apiFetch('/api/catalog/genre-stats').then(r => r.json()); renderGenreChart(gen.genres || []); } catch(e){}
    try{ const art = await apiFetch('/api/catalog/artist-stats').then(r => r.json()); renderArtistChart(art.artists || []); } catch(e){}
    try{ const dec = await apiFetch('/api/catalog/decade-stats').then(r => r.json()); renderDecadeChart(dec.decades || []); } catch(e){}
    try{ const week = await apiFetch('/api/catalog/weekly-trend').then(r => r.json()); renderWeeklyChart(week.weeks || []); } catch(e){}
    try{ const otd = await apiFetch('/api/catalog/on-this-day').then(r => r.json()); renderOnThisDay(otd.albums || []); } catch(e){}
  } catch(e){
    showError('Failed to load stats');
  }
};

// Keyboard shortcut handler
document.addEventListener('keydown', function(e){
  if((e.key === '?' || e.key === '/') && !document.getElementById('catalog-search').matches(':focus')){
    e.preventDefault();
    showShortcuts();
  }
});

// Restore player status on load
window.addEventListener('load', function(){
  setTimeout(restorePlayerStatus, 500);
});

// Register service worker for PWA support
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/service-worker.js').catch(err => {
    console.log('Service worker registration failed:', err);
  });
}

// Feature 2: Album Art Color Extraction
const _colorCache = {};

function extractDominantColor(imageUrl, callback) {
  if (_colorCache[imageUrl]) {
    callback(_colorCache[imageUrl]);
    return;
  }

  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = function() {
    const canvas = document.createElement('canvas');
    canvas.width = 100;
    canvas.height = 100;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0, 100, 100);

    const imageData = ctx.getImageData(0, 0, 100, 100);
    const data = imageData.data;

    let r = 0, g = 0, b = 0;
    let count = 0;

    for (let i = 0; i < data.length; i += 4) {
      r += data[i];
      g += data[i + 1];
      b += data[i + 2];
      count++;
    }

    r = Math.floor(r / count);
    g = Math.floor(g / count);
    b = Math.floor(b / count);

    const color = 'rgb(' + r + ',' + g + ',' + b + ')';
    _colorCache[imageUrl] = color;
    callback(color);
  };
  img.onerror = function() {
    callback('rgba(62, 39, 35, 0.5)');
  };
  img.src = imageUrl;
}

function applyColorTint(elementId, color) {
  const el = document.getElementById(elementId);
  if (el) {
    const rgb = color.match(/\d+/g);
    if (rgb) {
      const r = parseInt(rgb[0]);
      const g = parseInt(rgb[1]);
      const b = parseInt(rgb[2]);
      const tintColor = 'rgba(' + r + ',' + g + ',' + b + ',0.08)';
      el.style.background = 'linear-gradient(135deg, ' + tintColor + ' 0%, rgba(62,39,35,0.05) 100%)';
    }
  }
}

function applyNpBarTint(color) {
  const npBar = document.querySelector('.np-bar');
  if (npBar && color) {
    const rgb = color.match(/\d+/g);
    if (rgb) {
      const r = parseInt(rgb[0]);
      const g = parseInt(rgb[1]);
      const b = parseInt(rgb[2]);
      const tintColor = 'rgba(' + r + ',' + g + ',' + b + ',0.12)';
      npBar.style.background = 'linear-gradient(180deg, ' + tintColor + ' 0%, rgba(44, 24, 16, 0.8) 100%)';
    }
  }
}

// Feature 3: Smooth Transitions - CSS transitions support
function transitionView(fromEl, toEl, duration = 200) {
  if (!fromEl || !toEl) return;

  fromEl.style.transition = 'opacity ' + duration + 'ms ease-out, transform ' + duration + 'ms ease-out';
  toEl.style.transition = 'opacity ' + duration + 'ms ease-in, transform ' + duration + 'ms ease-in';

  fromEl.style.opacity = '0';
  fromEl.style.transform = 'scale(0.98)';
  fromEl.style.pointerEvents = 'none';

  toEl.style.opacity = '1';
  toEl.style.transform = 'scale(1)';
  toEl.style.pointerEvents = 'auto';
}

function smoothShowModal(modalEl) {
  if (!modalEl) return;
  modalEl.classList.add('open');
  modalEl.style.transition = 'opacity 200ms ease-out, transform 200ms ease-out';
  modalEl.style.opacity = '0';
  modalEl.style.transform = 'scale(0.95)';

  setTimeout(() => {
    modalEl.style.opacity = '1';
    modalEl.style.transform = 'scale(1)';
  }, 10);
}

function smoothHideModal(modalEl) {
  if (!modalEl) return;
  modalEl.style.transition = 'opacity 200ms ease-in, transform 200ms ease-in';
  modalEl.style.opacity = '0';
  modalEl.style.transform = 'scale(0.95)';

  setTimeout(() => {
    modalEl.classList.remove('open');
    modalEl.style.opacity = '';
    modalEl.style.transform = '';
  }, 200);
}

// Feature 6: Settings Backup and Restore
async function downloadSettingsBackup() {
  try {
    // Save backup to server and also offer download
    var r = await apiFetch('/api/settings/backup', { method: 'POST' }).then(function(d){return d.json()});
    if(r.backup_version) {
      showToast('Settings backed up. Timestamp: ' + new Date(r.backup_timestamp * 1000).toLocaleString());
      // Try opening download in new tab (works on mobile, may be blocked in kiosk)
      window.open('/api/settings/backup/download', '_blank');
    } else {
      showError('Backup failed');
    }
  } catch (err) {
    console.error('Backup failed:', err);
    showError('Failed to backup settings');
  }
}

function selectBackupFile() {
  document.getElementById('backup-file-input').click();
}

async function restoreSettingsFromFile() {
  const fileInput = document.getElementById('backup-file-input');
  const file = fileInput.files[0];
  if (!file) return;

  try {
    const text = await file.text();
    const backupData = JSON.parse(text);

    const response = await apiFetch('/api/settings/restore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(backupData),
    }).then(r => r.json());

    if (response.ok) {
      showToast('Settings restored successfully');
      setTimeout(() => window.location.reload(), 500);
    } else {
      showError(response.error || 'Failed to restore settings');
    }
  } catch (err) {
    console.error('Restore failed:', err);
    showError('Invalid backup file format');
  } finally {
    fileInput.value = '';
  }
}

async function generateCollage() {
  var btn = document.getElementById('collage-gen-btn');
  var status = document.getElementById('collage-status');
  var preview = document.getElementById('collage-preview');
  btn.disabled = true;
  btn.textContent = 'Generating...';
  status.style.display = 'block';
  status.textContent = 'Building collage from album artwork...';
  status.style.color = 'var(--muted)';
  try {
    var r = await apiFetch('/api/catalog/collage', {method:'POST'}).then(function(d){return d.json()});
    if (r.ok) {
      status.textContent = r.albums + ' albums, ' + r.size;
      status.style.color = 'var(--sage)';
      var img = document.getElementById('collage-img');
      img.src = r.url + '?t=' + Date.now();
      preview.style.display = 'block';
    } else {
      status.textContent = r.error || 'Failed to generate collage';
      status.style.color = 'var(--rust)';
      preview.style.display = 'none';
    }
  } catch (err) {
    status.textContent = 'Error: ' + err.message;
    status.style.color = 'var(--rust)';
    preview.style.display = 'none';
  }
  btn.disabled = false;
  btn.textContent = 'Generate Collage';
}

async function checkForUpdates() {
  const btn = document.getElementById('btn-check-update');
  const status = document.getElementById('update-status');
  const updateBtn = document.getElementById('btn-update-now');

  btn.disabled = true;
  btn.textContent = 'Checking...';
  status.textContent = '';
  status.style.color = 'var(--muted)';

  try {
    const response = await apiFetch('/api/system/check-update');
    const data = await response.json();
    const short = function(h){ return (h && h !== 'unknown') ? h.slice(0, 7) : 'unknown'; };

    if (data.fetch_error || data.ok === false) {
      status.style.color = 'var(--rust)';
      status.textContent = 'Could not reach GitHub: ' + (data.fetch_error || 'fetch failed');
      // Still offer Update Now — pull may work even if the check path lied before.
      updateBtn.style.display = 'block';
      showError('Update check failed — you can still try Update Now');
    } else if (data.available) {
      status.style.color = 'var(--amber-dk)';
      status.textContent = 'Update available (' + (data.commits_behind || '?') +
        ' commits): ' + short(data.current_commit) + ' → ' + short(data.latest_commit);
      updateBtn.style.display = 'block';
      showToast('Update available!');
    } else {
      status.style.color = 'var(--sage)';
      status.textContent = 'You are up to date (' + short(data.current_commit) + ')';
      updateBtn.style.display = 'none';
      showToast('Already up to date');
    }
  } catch (err) {
    console.error('Check failed:', err);
    status.style.color = 'var(--rust)';
    status.textContent = 'Failed to check for updates';
    updateBtn.style.display = 'block';
    showError('Could not reach update server');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check for Updates';
  }
}

async function performUpdate() {
  const progress = document.getElementById('update-progress');
  const progressBar = document.getElementById('update-progress-bar');
  const message = document.getElementById('update-message');
  const btn = document.getElementById('btn-update-now');

  btn.disabled = true;
  progress.style.display = 'block';
  message.textContent = 'Starting update...';
  progressBar.style.width = '10%';

  try {
    const response = await apiFetch('/api/system/update', { method: 'POST' });
    const data = await response.json();

    if (data.status === 'success') {
      message.textContent = 'Update complete. Reloading...';
      progressBar.style.width = '100%';

      await new Promise(r => setTimeout(r, 1500));

      for (let i = 0; i < 30; i++) {
        try {
          const pingResponse = await apiFetch('/api/system/check-update');
          if (pingResponse.ok) {
            window.location.reload();
            return;
          }
        } catch (e) {}
        await new Promise(r => setTimeout(r, 2000));
      }
      window.location.reload();
    } else {
      message.textContent = 'Update error: ' + (data.message || 'Unknown error');
      progressBar.style.width = '0%';
    }
  } catch (err) {
    console.error('Update failed:', err);
    message.textContent = 'Update failed: ' + err.message;
    progressBar.style.width = '0%';
  } finally {
    btn.disabled = false;
  }
}

async function reconfigureWiFi() {
  var btn = event.target.closest('button');
  var status = document.getElementById('wifi-status');
  status.style.display = 'none';
  if(btn){btn.disabled=true;btn.textContent='Starting portal...';}

  try {
    var response = await apiFetch('/api/wifi/reconfigure', { method: 'POST' });
    var data = await response.json();

    if (data.status === 'portal_started') {
      status.style.display = 'block';
      status.textContent = 'Portal started. Connect to "VinylStreamer-Setup" WiFi network to configure.';
      showToast('WiFi portal started');
    } else {
      showError(data.message || 'Failed to start WiFi portal');
    }
  } catch (err) {
    console.error('WiFi reconfigure failed:', err);
    showError('Failed to reconfigure WiFi');
  } finally {
    if(btn){btn.disabled=false;btn.textContent='Reconfigure WiFi';}
  }
}
