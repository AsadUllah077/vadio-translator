const $ = (id) => document.getElementById(id);
let selectedFile = null, sourceURL = null, configured = false, busy = false;
const supported = /\.(mp4|mov|webm|mkv|avi|m4v)$/i;
let languages = {};
const nativeNames = {ur:'اردو', hi:'हिन्दी', ar:'العربية', zh:'中文', ja:'日本語', ko:'한국어', ta:'தமிழ்'};
function error(message) { $('error').textContent = message; $('error').hidden = !message; }
function refresh() {
  $('submit').disabled = !selectedFile || !configured || busy;
  for (const id of ['video-file','target','voice','remove-file']) $(id).disabled = busy;
}
function selectFile(file) {
  if (busy || !file) return;
  error('');
  if (!supported.test(file.name)) return error('Please choose an MP4, MOV, WebM, MKV, AVI, or M4V video.');
  if (!file.size || file.size > 200 * 1024 * 1024) return error('Choose a nonempty video smaller than 200 MB.');
  selectedFile = file;
  if (sourceURL) URL.revokeObjectURL(sourceURL);
  sourceURL = URL.createObjectURL(file);
  $('source-video').src = sourceURL;
  $('filename').textContent = file.name;
  $('file-size').textContent = `${(file.size / 1024 / 1024).toFixed(1)} MB`;
  $('dropzone').hidden = true;
  $('file-preview').hidden = false;
  $('preview-warning').hidden = true;
  $('result').hidden = true;
  refresh();
}
$('video-file').addEventListener('change', e => selectFile(e.target.files[0]));
$('source-video').addEventListener('error', () => { $('preview-warning').hidden = false; });
$('source-video').addEventListener('loadedmetadata', () => {
  if ($('source-video').duration > 600) {
    removeFile(); error('This video is longer than 10 minutes. Please choose a shorter clip.');
  }
});
function removeFile() {
  if (busy) return;
  selectedFile = null;
  $('source-video').removeAttribute('src'); $('source-video').load();
  if (sourceURL) URL.revokeObjectURL(sourceURL);
  sourceURL = null;
  $('video-file').value = '';
  $('file-preview').hidden = true; $('dropzone').hidden = false; refresh();
}
$('remove-file').addEventListener('click', removeFile);
for (const event of ['dragenter', 'dragover']) $('dropzone').addEventListener(event, e => {
  e.preventDefault(); if (!busy) $('dropzone').classList.add('dragging');
});
for (const event of ['dragleave','drop']) $('dropzone').addEventListener(event, e => {
  e.preventDefault(); $('dropzone').classList.remove('dragging');
});
$('dropzone').addEventListener('drop', e => selectFile(e.dataTransfer.files[0]));
async function request(url, options) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error('The server returned an unexpected response. Please try again.'); }
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The request failed. Please try again.');
  return data;
}
function progress(value, stage) {
  $('progress-panel').hidden = false;
  $('progress-fill').style.width = `${value}%`;
  $('progress-percent').textContent = `${value}%`;
  $('stage').textContent = stage;
}
function showResult(job) {
  $('progress-panel').hidden = true; $('result').hidden = false;
  const base = `/api/jobs/${job.id}/files/`;
  $('result-video').src = base + 'video';
  for (const kind of ['video','audio','subtitles']) $('download-' + kind).href = base + kind;
  $('language-pair').textContent = `${job.source_language || 'Detected'} → ${languages[job.target] || job.target}`;
  $('transcript').replaceChildren();
  for (const segment of job.segments || []) {
    const row = document.createElement('div'); row.className = 'transcript-row';
    const time = document.createElement('time'); time.textContent = `${Math.floor(segment.start / 60)}:${String(Math.floor(segment.start % 60)).padStart(2,'0')}`;
    const original = document.createElement('p'); original.dir = 'auto'; original.textContent = segment.text;
    const translated = document.createElement('p'); translated.dir = 'auto'; translated.textContent = segment.translation;
    row.append(time, original, translated); $('transcript').append(row);
  }
}
async function poll(id) {
  let failures = 0;
  while (true) {
    let job;
    try { job = await request(`/api/jobs/${id}`); failures = 0; }
    catch (e) {
      if (++failures >= 5) throw e;
      progress(0, 'Reconnecting to your translation…');
      await new Promise(resolve => setTimeout(resolve, 3000)); continue;
    }
    if (job.status === 'failed') {
      localStorage.removeItem('vadio-job'); throw new Error(job.error);
    }
    if (job.status === 'complete') { showResult(job); return; }
    progress(job.progress, job.stage);
    await new Promise(resolve => setTimeout(resolve, 1800));
  }
}
$('translate-form').addEventListener('submit', async e => {
  e.preventDefault(); if (!selectedFile || busy || !configured) return;
  busy = true; refresh(); error(''); $('result').hidden = true;
  progress(1, 'Uploading your video…');
  try {
    const form = new FormData(); form.append('file',selectedFile); form.append('target',$('target').value); form.append('voice',$('voice').value);
    const job = await request('/api/jobs', {method:'POST',body:form});
    localStorage.setItem('vadio-job',job.id);
    await poll(job.id);
  } catch(e) { error(e.message); $('progress-panel').hidden = true; }
  finally { busy = false; refresh(); }
});
async function init() {
  try {
    const config = await request('/api/config');
    configured = config.configured; languages = config.languages;
    $('setup').hidden = configured;
    $('target').replaceChildren(...Object.entries(languages).map(([code,name]) => {
      const option = new Option(nativeNames[code] ? `${name} — ${nativeNames[code]}` : name,code);
      option.selected = code === 'ur'; return option;
    }));
    $('voice').replaceChildren(...config.voices.map(voice => new Option(voice[0].toUpperCase() + voice.slice(1),voice)));
    refresh();
    const saved = localStorage.getItem('vadio-job');
    if (saved) { busy = true; refresh(); await poll(saved); }
  } catch(e) { error(e.message || 'Could not connect to the server.'); }
  finally { busy = false; refresh(); }
}
init();
