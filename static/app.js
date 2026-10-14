/*
 * Seconded — local meeting transcription & summarization
 * Copyright (C) 2026 Seconded contributors
 *
 * This program is free software: you can redistribute it and/or modify it under
 * the terms of the GNU Affero General Public License as published by the Free
 * Software Foundation, either version 3 of the License, or (at your option) any
 * later version.
 *
 * This program is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
 * FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License for more
 * details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program. If not, see <https://www.gnu.org/licenses/>.
 */

const $ = s => document.querySelector(s);
const T = (k, v) => I18N.T(k, v);

let es, curStatus='idle', currentSession=null;

// ---------- event source ----------
function connect(){
  es = new EventSource('/events');
  es.addEventListener('status', e => { const d=JSON.parse(e.data); setStatus(I18N.Tstatus(d.msg||''));
    if (/regenerated/i.test(d.msg||'')){ onRegenDone(); $('#regen-note').textContent=T('regen_done'); } });
  es.addEventListener('live',   e => { $('#livetext').textContent = JSON.parse(e.data).text; });
  es.addEventListener('final',  e => renderFinal(JSON.parse(e.data)));
  es.onerror = () => {};
}

function api(path, body){
  const opt = {method:'POST'}; if(body) opt.body = JSON.stringify(body);
  return fetch(path,opt).then(r=>r.json()).catch(()=>({}));
}

// ---------- status banner ----------
const PHASE_KEYS = { idle:'phase_idle', loading:'phase_loading', live:'phase_live',
                     finalizing:'phase_finalizing', done:'phase_done', error:'phase_error' };
function setStatus(msg){ $('#status').textContent = msg; }
function paintBanner(){
  const b = $('#banner');
  b.className = (curStatus==='live'?'recording': curStatus==='finalizing'||curStatus==='loading'?'processing':
                 curStatus==='done'?'done': curStatus==='error'?'error':'');
  $('#phase').textContent = T(PHASE_KEYS[curStatus] || 'phase_idle');
  $('#cancelbtn').style.display = (curStatus==='loading'||curStatus==='finalizing') ? '' : 'none';
  // uploads can't share a session with a live recording / running pipeline
  const busy = ['live','loading','finalizing'].includes(curStatus);
  const up = $('#uplabel');
  if (up){ up.style.opacity = busy ? '.45' : ''; up.style.pointerEvents = busy ? 'none' : ''; }
  document.title = (curStatus==='live' ? '🔴 ' : curStatus==='finalizing'||curStatus==='loading' ? '⏳ ' : '')
                 + 'Seconded';
}

async function cancelProcessing(){
  if(!confirm(T('confirm_cancel'))) return;
  stopMic();
  const r = await fetch('/cancel', {method:'POST'});
  const d = await r.json().catch(()=>({}));
  if(!r.ok){ setStatus(T('cancel_failed') + (d.error || r.status)); return; }
  clearTranscriptUI();
  setStatus(T('stopping'));
}

// ---------- summary template/length options ----------
let TPLS = [], LENS = [];

async function loadSettings(){
  try {
    const d = await (await fetch('/api/templates')).json();
    TPLS = d.templates || []; LENS = d.lengths || [];
    fillSel('#tplsel', TPLS);
    $('#tplsel').onchange = saveSettings;
    // restore the user's last template choice: localStorage survives refreshes
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem('ma_settings') || '{}'); } catch(e){}
    if (TPLS.some(t => t.id === saved.template)) $('#tplsel').value = saved.template;
    // a non-default template already on the server (another device) wins
    try {
      const s = await (await fetch('/api/state')).json();
      if (s.template && s.template !== (d.defaults||{}).template && TPLS.some(t=>t.id===s.template))
        $('#tplsel').value = s.template;
    } catch(e){}
    saveSettings();   // push the restored selection to the server
  } catch(e){}
}
function fillSel(sel, items){
  const el = $(sel); el.innerHTML='';
  for(const it of items){
    const o = document.createElement('option');
    o.value = it.id; o.textContent = I18N.Ttpl(it.id, it.label); el.appendChild(o);
  }
}
async function saveSettings(){
  try { localStorage.setItem('ma_settings',
    JSON.stringify({template: $('#tplsel').value})); } catch(e){}
  await api('/api/settings', {template: $('#tplsel').value});
}

// ---------- sidebar: session list ----------
let SESSIONS = [];

async function loadSidebar(){
  try {
    const d = await (await fetch('/api/history')).json();
    SESSIONS = d.sessions || [];
    renderSidebar();
  } catch(e){}
}

function renderSidebar(){
  const list = $('#sidebar-list');
  const q = ($('#sess-filter').value || '').trim().toLowerCase();
  const shown = q ? SESSIONS.filter(s => (s.name || s.dir).toLowerCase().includes(q)) : SESSIONS;
  list.innerHTML = '';
  if(!SESSIONS.length){
    list.innerHTML = `<div class="empty" style="padding:20px 0;font-size:13px">${escapeHtml(T('no_sessions'))}</div>`;
    return;
  }
  if(!shown.length){
    list.innerHTML = `<div class="empty" style="padding:20px 0;font-size:13px">${escapeHtml(T('no_match', {q}))}</div>`;
    return;
  }
  for(const s of shown){
    const item = document.createElement('div');
    item.className = 'session-item' + (s.dir === currentSession ? ' active' : '');
    const date = s.created_at ? new Date(s.created_at).toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '';
    item.innerHTML = `
      <div class="name">${escapeHtml(s.name || s.dir)}${s.current?`<span class="badge current">${escapeHtml(T('badge_current'))}</span>`:''}</div>
      <div class="meta"><span>${date}</span><span>${fmtDur(s.duration_s)}</span>${s.has_summary?`<span class="badge">${escapeHtml(T('badge_done'))}</span>`:''}</div>
      <div class="sess-actions">
        <button title="${escapeHtml(T('rename'))}" onclick="event.stopPropagation();renameSession('${jsq(s.dir)}','${jsq(s.name || s.dir)}')">✎</button>
        <button title="${escapeHtml(T('del'))}" onclick="event.stopPropagation();deleteSession('${jsq(s.dir)}','${jsq(s.name || s.dir)}')">🗑</button>
      </div>`;
    item.onclick = () => selectSession(s.dir, s.name);
    list.appendChild(item);
  }
}
// safe inline-JS string escaping (values are user-typed session names)
function jsq(s){ return String(s).replace(/\\/g,'\\\\').replace(/'/g,"\\'").replace(/"/g,'&quot;').replace(/\n/g,'\\n'); }

async function renameSession(dir, oldName){
  const n = prompt(T('rename_prompt'), oldName);
  if (n === null) return;
  const r = await fetch(`/history/${dir}/rename`, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: n})});
  if (!r.ok){ const d = await r.json().catch(()=>({})); setStatus(T('rename_failed') + (d.error || r.status)); return; }
  loadSidebar();
}

async function deleteSession(dir, name){
  if (!confirm(T('confirm_delete', {name}))) return;
  const r = await fetch(`/history/${dir}`, {method:'DELETE'});
  if (!r.ok){ const d = await r.json().catch(()=>({})); setStatus(T('delete_failed') + (d.error || r.status)); return; }
  if (currentSession === dir) currentSession = null;
  loadSidebar();
}

// ---------- select a session ----------
function selectSession(dir, name){
  currentSession = dir;
  loadSidebar();          // re-render to move the active highlight
  viewSession(dir, name);
}

async function viewSession(dir, name){
  const d = await (await fetch(`/history/${dir}/view`)).json();
  if (d.error){ setStatus(T('open_failed') + d.error); return; }
  $('#m-title').textContent = d.name || dir;

  if (TPLS.length === 0) await loadSettings();

  buildDlBar('#m-dlbar', `/history/${dir}/download/`);
  // legacy/hidden template ids (e.g. old 'default' sessions) aren't in the
  // dropdown — align the stored setting with what the select actually shows
  if (!TPLS.some(t => t.id === d.template)) d.template = (TPLS[0]||{}).id;
  window.viewSessionSettings = {template: d.template, length: d.length};
  $('#m-dlbar').insertAdjacentHTML('beforeend',
    `<span class="dl-group" style="gap:6px">
       <select id="m-tplsel" onchange="window.viewSessionSettings.template=this.value"
         style="padding:8px;border-radius:8px;border:1px solid var(--line);background:var(--panel2);color:var(--fg);font-size:12.5px">
         ${TPLS.map(t=>`<option value="${t.id}" ${d.template===t.id?'selected':''}>${escapeHtml(I18N.Ttpl(t.id, t.label))}</option>`).join('')}
       </select>
       <button onclick="regenerateHistory('${jsq(dir)}')">${escapeHtml(T('regenerate_short'))}</button>
       <button onclick="rediarizeHistory('${jsq(dir)}')" title="${escapeHtml(T('rediarize_title'))}">${escapeHtml(T('rediarize_short'))}</button>
     </span>`);

  const t = d.timings || {}, mods = d.models || {};
  const bits = [];
  if(t.transcription_s) bits.push(T('t_transcription', {d: fmtDur(t.transcription_s)}));
  if(t.diarization_s)   bits.push(T('t_diarization',   {d: fmtDur(t.diarization_s)}));
  if(t.summary_s)       bits.push(T('t_summary',       {d: fmtDur(t.summary_s)}));
  let notes = '';
  if(bits.length || mods.asr || mods.summary){
    notes = `<div style="font-size:12.5px;color:var(--dim);margin-bottom:14px">` +
      (bits.length ? `⏱ ${bits.join(' · ')}` : '') +
      ((bits.length && (mods.asr||mods.summary)) ? ' &nbsp;·&nbsp; ' : '') +
      [mods.asr, mods.summary].filter(Boolean).map(escapeHtml).join(' · ') + `</div>`;
  }
  let html = notes;
  for(const sg of d.segments){
    html += `<div class="line"><div class="who" style="color:${colorFor(sg.speaker)}">${escapeHtml(T('speaker', {n: sg.speaker+1}))}</div>
      <div class="txt"><span class="ts">${fmt(sg.start)} → ${fmt(sg.end)}</span>${escapeHtml(sg.text)}</div></div>`;
  }
  if(d.summary){
    html += `<h2 style="margin-top:24px;font-size:13px;text-transform:uppercase;letter-spacing:1.2px;color:var(--dim)">${escapeHtml(T('summary_heading'))}</h2>
      <div class="md">${mdToHtml(d.summary)}</div>`;
  }
  $('#m-body').innerHTML = html || `<div class="empty">${escapeHtml(T('no_transcript_data'))}</div>`;
  $('#modal').classList.add('open');
}

// ---------- tiny markdown renderer (headings, bold, lists, checkboxes) ----------
function mdToHtml(md){
  const out = []; let inUl = false;
  const inline = t => escapeHtml(t)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
  for (const raw of md.split('\n')){
    const ln = raw.replace(/\s+$/, '');
    let m;
    if ((m = ln.match(/^(#{1,4})\s+(.*)/))) {
      if (inUl){ out.push('</ul>'); inUl = false; }
      const lvl = m[1].length;
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`);
    } else if ((m = ln.match(/^\s*[-*]\s+\[([ xX])\]\s+(.*)/))) {
      if (!inUl){ out.push('<ul>'); inUl = true; }
      const done = m[1].toLowerCase() === 'x';
      out.push(`<li class="task" style="${done?'opacity:.55;text-decoration:line-through':''}">${done?'✅':'⬜'} ${inline(m[2])}</li>`);
    } else if ((m = ln.match(/^\s*[-*]\s+(.*)/))) {
      if (!inUl){ out.push('<ul>'); inUl = true; }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if (/^\s*(---|———)\s*$/.test(ln)) {
      if (inUl){ out.push('</ul>'); inUl = false; }
      out.push('<hr>');
    } else if (ln.trim() === '') {
      if (inUl){ out.push('</ul>'); inUl = false; }
    } else {
      if (inUl){ out.push('</ul>'); inUl = false; }
      out.push(`<p>${inline(ln)}</p>`);
    }
  }
  if (inUl) out.push('</ul>');
  return out.join('\n');
}

let regenWatch = null;
async function regenerateHistory(dir){
  const settings = window.viewSessionSettings || {template:'default', length:'medium'};
  const r = await fetch(`/history/${dir}/regenerate`, {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(settings)});
  const d = await r.json().catch(()=>({}));
  if(!r.ok){ setStatus(T('regen_failed') + (d.error || r.status)); return; }
  regenWatch = {dir};
  setStatus(T('regen_started'));
}
async function rediarizeHistory(dir){
  const r = await fetch(`/history/${dir}/rediarize`, {method:'POST'});
  const d = await r.json().catch(()=>({}));
  if(!r.ok){ setStatus(T('regen_failed') + (d.error || r.status)); return; }
  regenWatch = {dir};
  setStatus(T('rediarize_started'));
}
function onRegenDone(){
  if (!regenWatch) return;
  const dir = regenWatch.dir; regenWatch = null;
  viewSession(dir, '');
}
function closeModal(){ $('#modal').classList.remove('open'); }

// ---------- keyboard shortcuts ----------
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && $('#modal').classList.contains('open')) closeModal();
  if (ev.key === '/' && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)){
    ev.preventDefault(); $('#sess-filter').focus();
  }
});

// ---------- mic devices ----------
async function listMicDevices(){
  const sel = $('#micdev');
  try {
    let devs = await navigator.mediaDevices.enumerateDevices();
    let inputs = devs.filter(d => d.kind === 'audioinput');
    if (!inputs.length && !mediaStream){
      const tmp = await navigator.mediaDevices.getUserMedia({audio:true});
      tmp.getTracks().forEach(t=>t.stop());
      devs = await navigator.mediaDevices.enumerateDevices();
      inputs = devs.filter(d => d.kind === 'audioinput');
    }
    const prev = sel.value;
    sel.innerHTML = '';
    if (!inputs.length){ sel.innerHTML = `<option value="">${escapeHtml(T('no_mic'))}</option>`; return; }
    for (const d of inputs){
      const o = document.createElement('option');
      o.value = d.deviceId;
      o.textContent = d.label || T('mic_fallback', {id: d.deviceId.slice(0,6)});
      sel.appendChild(o);
    }
    if (prev) sel.value = prev;   // keep the user's pick across devicechange
  } catch(e){ sel.innerHTML = `<option value="">${escapeHtml(T('mic_list_unavailable'))}</option>`; }
}

async function refreshPermBanner(){
  const b = $('#perm-banner');
  try {
    if (!navigator.permissions || !window.isSecureContext){ b.style.display='none'; return; }
    const st = await navigator.permissions.query({name:'microphone'});
    b.style.display = (st.state === 'granted') ? 'none' : '';
    st.onchange = () => { b.style.display = (st.state === 'granted') ? 'none' : ''; listMicDevices(); };
  } catch(e){ b.style.display='none'; }
}
async function grantMic(){
  try {
    const s = await navigator.mediaDevices.getUserMedia({audio:true});
    s.getTracks().forEach(t=>t.stop());
    listMicDevices(); refreshPermBanner();
    setStatus(T('mic_allowed'));
  } catch(e){ setStatus(T('mic_denied')+e.message); }
}

// ---------- mic capture ----------
let audioCtx=null, procNode=null, mediaStream=null, mixGain=null;
let sysStream=null, sysSrc=null;

function makePipeline(){
  audioCtx = new AudioContext({sampleRate:16000});
  mixGain = audioCtx.createGain();
  procNode = audioCtx.createScriptProcessor(4096, 1, 1);
  let meterTick = 0;
  procNode.onaudioprocess = (ev) => {
    const f32 = ev.inputBuffer.getChannelData(0);
    if ((meterTick++ & 7) === 0){
      let sum=0; for(let i=0;i<f32.length;i+=16) sum += f32[i]*f32[i];
      const rms = Math.sqrt(sum/(f32.length/16));
      const bars='▁▂▃▄▅▆▇█';
      $('#meter').textContent = '🎚 '+bars[Math.min(7,Math.floor(rms*40))]+(rms<0.005?'  '+T('silent'):'');
    }
    const i16 = new Int16Array(f32.length);
    for(let i=0;i<f32.length;i++){ let v=Math.max(-1,Math.min(1,f32[i])); i16[i]=v<0?v*32768:v*32767; }
    fetch('/ingest',{method:'POST', body:i16.buffer}).catch(()=>{});
  };
  mixGain.connect(procNode); procNode.connect(audioCtx.destination);
}

function attachMic(){
  const src = audioCtx.createMediaStreamSource(mediaStream);
  src.connect(mixGain);
}

async function attachSystemAudio(){
  try {
    const stream = await navigator.mediaDevices.getDisplayMedia({video:false, audio:true});
    if (stream.getAudioTracks().length) return setupSystemAudio(stream);
    stream.getTracks().forEach(t=>t.stop());
  } catch(e){}

  const stream = await navigator.mediaDevices.getDisplayMedia({video:true, audio:true});
  if (!stream.getAudioTracks().length){
    stream.getTracks().forEach(t=>t.stop());
    throw new Error(T('no_sys_audio'));
  }
  return setupSystemAudio(stream);
}

function setupSystemAudio(stream){
  sysSrc = audioCtx.createMediaStreamSource(stream);
  sysSrc.connect(mixGain);
  sysStream = stream;
  const track = stream.getAudioTracks()[0];
  track.onended = () => {
    try{ sysSrc.disconnect(); }catch(e){}
    sysStream=null; sysSrc=null; $('#addsysbtn').style.display='';
    setStatus(T('meeting_audio_stopped'));
  };
  return track.label || T('sys_audio_label');
}

async function startWithMic(){ await startCapture(false); }
async function startWithBoth(){ await startCapture(true); }

async function startCapture(withSystem){
  if (!navigator.mediaDevices || !window.isSecureContext){
    setStatus(T('mic_blocked_https'));
    return;
  }
  setStatus(T('requesting_mic'));
  await api('/start');
  const constraints = {audio:{echoCancellation:true, noiseSuppression:true}};
  const devId = $('#micdev').value;
  if (devId) constraints.audio.deviceId = {exact: devId};
  try { mediaStream = await navigator.mediaDevices.getUserMedia(constraints); }
  catch(err){
    try { mediaStream = await navigator.mediaDevices.getUserMedia({audio:true}); }
    catch(e2){ setStatus(T('mic_denied')+e2.message); return; }
  }
  makePipeline(); attachMic();
  const devName = $('#micdev').selectedOptions[0];
  let label = T('recording_from', {dev: devName ? devName.textContent : T('from_mic')});
  if (withSystem){
    try {
      const sysLabel = await attachSystemAudio();
      label += ` + ${sysLabel}`;
      $('#addsysbtn').style.display='none';
    } catch(e){
      setStatus(label + ' — ' + T('sys_audio_not_added') + e.message);
      return;
    }
  } else {
    $('#addsysbtn').style.display='';
  }
  setStatus(label);
}

async function addMeetingAudio(){
  if (!audioCtx){ setStatus(T('start_mic_first')); return; }
  try {
    const sysLabel = await attachSystemAudio();
    $('#addsysbtn').style.display='none';
    setStatus(T('added_both', {label: sysLabel}));
  } catch(e){ setStatus(T('add_audio_failed')+e.message); }
}

function stopMic(){
  if(mediaStream) mediaStream.getTracks().forEach(t=>t.stop());
  if(sysStream) sysStream.getTracks().forEach(t=>t.stop());
  if(procNode){ try{procNode.disconnect();}catch(e){} }
  if(audioCtx){ audioCtx.close().catch(()=>{}); }
  mediaStream=null; procNode=null; audioCtx=null; mixGain=null; sysStream=null; sysSrc=null;
  $('#meter').textContent=''; $('#addsysbtn').style.display='none';
}

function endMeeting(){ stopMic(); api('/end'); }
async function resetSession(){
  if(!confirm(T('confirm_discard'))) return;
  stopMic(); await api('/reset'); clearTranscriptUI();
}

// ---------- new session ----------
function newSession(){
  stopMic();
  api('/reset');
  clearTranscriptUI();
  $('#sessname').value = '';
  currentSession = null;
  setStatus(T('new_session_ready'));
  loadSidebar();
}

// ---------- upload (own session; drag & drop + picker) ----------
function uploadFile(f){
  if (!f) return;
  if (curStatus === 'live' || curStatus === 'loading'){
    setStatus(T('upload_busy_rec'));
    return;
  }
  if (curStatus === 'finalizing'){
    setStatus(T('upload_busy_proc'));
    return;
  }
  // fresh session: clear whatever the view was showing
  clearTranscriptUI();
  currentSession = null;
  $('#sessname').value = '';

  const bar = $('#upprog'), fill = bar.firstElementChild;
  bar.style.display = ''; fill.style.width = '0';
  setStatus(T('uploading', {name: f.name, mb: (f.size/1e6).toFixed(1)}));
  const xhr = new XMLHttpRequest();
  const fd = new FormData(); fd.append('file', f);
  xhr.open('POST', '/upload');
  xhr.upload.onprogress = (e) => { if (e.lengthComputable) fill.style.width = (e.loaded/e.total*100) + '%'; };
  xhr.onload = () => {
    let msg;
    try { const d = JSON.parse(xhr.responseText);
      msg = xhr.ok ? T('processing_file', {name: f.name}) : T('upload_failed') + (d.error || xhr.status);
    } catch(e){ msg = T('upload_failed_http', {code: xhr.status}); }
    setStatus(msg);
    setTimeout(() => { bar.style.display='none'; fill.style.width='0'; }, 1200);
  };
  xhr.onerror = () => { setStatus(T('upload_network_error')); bar.style.display='none'; };
  xhr.send(fd);
}

$('#upfile').addEventListener('change', (ev) => { uploadFile(ev.target.files[0]); ev.target.value=''; });

// drag & drop onto the dropzone
const dz = $('#dropzone');
for (const evt of ['dragenter','dragover'])
  dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.add('drag'); });
for (const evt of ['dragleave','dragend','drop'])
  dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.remove('drag'); });
dz.addEventListener('drop', e => {
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) uploadFile(f);
});
// don't let a dropped file elsewhere in the page navigate away from the app
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => e.preventDefault());

// ---------- regenerate ----------
async function regenerateCurrent(){
  const note = $('#regen-note');
  note.textContent = T('regenerating');
  const r = await fetch('/regenerate', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({template:$('#tplsel').value})});
  const d = await r.json().catch(()=>({}));
  if(!r.ok){ note.textContent = T('regen_failed') + (d.error || r.status); return; }
  note.textContent = T('regenerating_banner');
}

// ---------- session name ----------
function saveName(){ const n=$('#sessname').value.trim(); if(n) api('/api/session/name',{name:n}); }

// ---------- transcript rendering ----------
const COLORS = ['#5b8cff','#ff7a5c','#3ddc84','#e8c15a','#c792ea','#f07dc4'];
function colorFor(k){ return COLORS[k % COLORS.length]; }
function fmt(sec){ const h=Math.floor(sec/3600), m=Math.floor(sec%3600/60), s=sec%60;
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${s.toFixed(2).padStart(5,'0')}`; }
function fmtDur(s){ if(!s) return ''; const h=Math.floor(s/3600), m=Math.floor(s%3600/60);
  return (h?h+'h ':'')+m+'m '+(Math.round(s%60))+'s'; }
function escapeHtml(t){ return String(t ?? '').replace(/[<>"&]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function clearTranscriptUI(){
  $('#lines').innerHTML = `<div class="empty" id="empty">${escapeHtml(T('empty_transcript'))}</div>`;
  $('#lines').classList.remove('collapsed');
  $('#livetext').textContent='—';
  $('#dl-card').style.display='none';
  $('#collapse-btn').style.display='none';
}

function toggleCollapse(){
  const lines = $('#lines');
  const btn = $('#collapse-btn');
  lines.classList.toggle('collapsed');
  btn.textContent = lines.classList.contains('collapsed') ? T('expand') : T('collapse');
}

function renderFinal(d){
  clearTranscriptUI();
  const box = $('#lines');
  d.segments.forEach(sg => {
    const div = document.createElement('div'); div.className='line';
    div.innerHTML = `<div class="who" style="color:${colorFor(sg.speaker)}">${escapeHtml(T('speaker', {n: sg.speaker+1}))}</div>
      <div class="txt"><span class="ts">${fmt(sg.start)} → ${fmt(sg.end)}</span>${escapeHtml(sg.text)}</div>`;
    box.appendChild(div);
  });
  buildDlBar('#dlbar', '/download/');
  $('#dl-card').style.display='';
  $('#collapse-btn').style.display='';
  setStatus(T('final_ready'));
}

function buildDlBar(sel, base){
  $(sel).innerHTML = `
    <div class="dl-group"><span class="lbl">${escapeHtml(T('dl_notes'))}</span>
      <a class="btn" href="${base}combined?format=md">${escapeHtml(T('dl_markdown'))}</a>
      <a class="btn" href="${base}combined?format=pdf">${escapeHtml(T('dl_pdf'))}</a></div>
    <div class="dl-group"><span class="lbl">${escapeHtml(T('dl_transcript'))}</span>
      <a class="btn" href="${base}transcript?format=md">${escapeHtml(T('dl_markdown'))}</a>
      <a class="btn" href="${base}transcript?format=pdf">${escapeHtml(T('dl_pdf'))}</a></div>
    <div class="dl-group"><span class="lbl">${escapeHtml(T('dl_summary'))}</span>
      <a class="btn" href="${base}summary?format=md">${escapeHtml(T('dl_markdown'))}</a>
      <a class="btn" href="${base}summary?format=pdf">${escapeHtml(T('dl_pdf'))}</a></div>
    <div class="dl-group"><span class="lbl">${escapeHtml(T('dl_other'))}</span>
      <a class="btn" href="${base}vtt">${escapeHtml(T('dl_vtt'))}</a>
      <a class="btn" href="${base}audio.wav">${escapeHtml(T('dl_wav'))}</a></div>`;
}

// ---------- polling state ----------
setInterval(async ()=>{ try {
    const s = await (await fetch('/api/state')).json();
    if(s.status !== curStatus){
      if(s.status === 'idle' && (curStatus==='finalizing'||curStatus==='loading')) clearTranscriptUI();
      curStatus = s.status; paintBanner();
      if(curStatus === 'done') loadSidebar();
    }
    $('#dur').textContent = s.duration ? fmt(s.duration) : '';
    if (s.models && $('#models-sub')){
      const sub = `${s.models.asr} + ${s.models.summary}`;
      const el = $('#models-sub');
      if (el.textContent !== sub){ el.textContent = sub; el.title = sub; }
    }
  } catch(e){} }, 2000);

// ---------- boot ----------
I18N.applyI18n();
I18N.initLangSelect();
connect();
listMicDevices();
refreshPermBanner();
loadSettings();
loadSidebar();
if (navigator.mediaDevices && navigator.mediaDevices.addEventListener)
  navigator.mediaDevices.addEventListener('devicechange', listMicDevices);
