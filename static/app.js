// Hacker Environment - client
const UOFA = [53.5232, -113.5263];
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let me = storeGet('he_user');
let spots = [];
let map, markersLayer, meMarker;
let pending = []; // compressed photo blobs for a new report

// ---------- tiny utils ----------
function storeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function storeSet(k, v) { try { localStorage.setItem(k, v); } catch {} }

function toast(msg, kind = 'ok') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'fixed left-1/2 -translate-x-1/2 top-24 z-[1100] max-w-[90vw] px-4 py-3 rounded-2xl shadow-xl text-sm font-semibold ' +
    (kind === 'err' ? 'bg-rose-600 text-white' : 'bg-forest-950 text-white');
  t.classList.remove('hidden');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add('hidden'), 3800);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch {}
  if (!res.ok) throw new Error((body && body.detail) || `Something went wrong (${res.status})`);
  return body;
}

function showLoading(text, imgUrl) {
  $('loadingText').textContent = text;
  $('loadingImg').style.backgroundImage = imgUrl ? `url(${imgUrl})` : '';
  $('loading').classList.remove('hidden');
}
const hideLoading = () => $('loading').classList.add('hidden');

function getLocation() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error('Location is not available on this device.'));
    navigator.geolocation.getCurrentPosition(
      (p) => resolve({ lat: p.coords.latitude, lng: p.coords.longitude, acc: p.coords.accuracy }),
      () => reject(new Error('Allow location access so we can pin the spot.')),
      { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 });
  });
}

// Shrink photos before upload: faster on venue Wi-Fi and cheaper for the AI.
function compress(file, max = 1280, quality = 0.82) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const scale = Math.min(1, max / Math.max(img.width, img.height));
      const c = document.createElement('canvas');
      c.width = Math.round(img.width * scale);
      c.height = Math.round(img.height * scale);
      c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      c.toBlob((b) => (b ? resolve(b) : reject(new Error('Could not read photo'))), 'image/jpeg', quality);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('Could not read that photo. Try a JPG or PNG.')); };
    img.src = url;
  });
}

function timeAgo(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

const DIFF = ['', 'Easy', 'Light', 'Medium', 'Hard', 'Extreme'];
const DIFF_COLOR = ['', '#f59e0b', '#f97316', '#ef4444', '#e11d48', '#9f1239'];

// ---------- navigation ----------
function go(tab) {
  document.querySelectorAll('.view').forEach((v) => v.classList.remove('active'));
  $('view-' + tab).classList.add('active');
  document.querySelectorAll('.tab').forEach((b) => {
    const on = b.dataset.tab === tab;
    b.classList.toggle('text-forest-700', on);
    b.classList.toggle('text-slate-400', !on);
  });
  closeSheet();
  if (tab === 'map') setTimeout(() => map.invalidateSize(), 50);
  if (tab === 'leaders') loadLeaders();
  if (tab === 'me') loadMe();
}

// ---------- user ----------
function askName() { $('nameModal').classList.remove('hidden'); setTimeout(() => $('nameInput').focus(), 100); }
function saveName() {
  const n = $('nameInput').value.trim();
  if (!n) return toast('Pick a username', 'err');
  me = n.slice(0, 24);
  storeSet('he_user', me);
  $('nameModal').classList.add('hidden');
  refreshPoints();
}
function changeName() { $('nameInput').value = me || ''; askName(); }

async function refreshPoints() {
  if (!me) return;
  try { const p = await api('/api/users/' + encodeURIComponent(me)); $('myPoints').textContent = p.points; } catch {}
}

// ---------- map ----------
function initMap() {
  map = L.map('map', { zoomControl: false, attributionControl: true }).setView(UOFA, 15);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors',
  }).addTo(map);
  markersLayer = L.layerGroup().addTo(map);
}

function pinIcon(s) {
  let color, label;
  if (s.status === 'cleaned') { color = '#16b364'; label = '✓'; }
  else if (s.status === 'claimed') { color = '#f59e0b'; label = '⏳'; }
  else { color = DIFF_COLOR[s.difficulty] || '#ef4444'; label = s.difficulty; }
  return L.divIcon({ className: '', iconSize: [34, 34], iconAnchor: [17, 34],
    html: `<div class="pin" style="background:${color}"><span>${label}</span></div>` });
}

async function loadSpots() {
  try { spots = await api('/api/spots'); } catch (e) { return toast(e.message, 'err'); }
  markersLayer.clearLayers();
  for (const s of spots) {
    L.marker([s.lat, s.lng], { icon: pinIcon(s) }).on('click', () => openSpot(s.id)).addTo(markersLayer);
  }
  $('cntOpen').textContent = spots.filter((s) => s.status === 'open').length;
  $('cntClaimed').textContent = spots.filter((s) => s.status === 'claimed').length;
  $('cntClean').textContent = spots.filter((s) => s.status === 'cleaned').length;
}

async function locateMe() {
  try {
    const p = await getLocation();
    map.setView([p.lat, p.lng], 17);
    if (meMarker) meMarker.remove();
    meMarker = L.circleMarker([p.lat, p.lng], { radius: 8, color: '#fff', weight: 3, fillColor: '#2563eb', fillOpacity: 1 }).addTo(map);
  } catch (e) { toast(e.message, 'err'); }
}

// ---------- spot sheet ----------
function openSheet(html) {
  $('sheetBody').innerHTML = html;
  $('sheetBackdrop').classList.remove('hidden');
  requestAnimationFrame(() => $('sheet').classList.add('open'));
}
function closeSheet() {
  $('sheet').classList.remove('open');
  $('sheetBackdrop').classList.add('hidden');
}

function openSpot(id) {
  const s = spots.find((x) => x.id === id);
  if (!s) return;
  map.panTo([s.lat, s.lng]);
  const dots = [1, 2, 3, 4, 5].map((i) =>
    `<i class="inline-block w-5 h-2 rounded-full" style="background:${i <= s.difficulty ? DIFF_COLOR[s.difficulty] : '#e2e8f0'}"></i>`).join('');
  const photos = s.photos.map((p) => `<img src="${esc(p)}" class="h-44 w-auto rounded-2xl object-cover shrink-0" alt="">`).join('');
  const hazards = s.hazards.length
    ? `<div class="mt-3 flex flex-wrap gap-1.5">${s.hazards.map((h) => `<span class="text-xs font-semibold bg-rose-50 text-rose-700 rounded-full px-2.5 py-1">⚠️ ${esc(h)}</span>`).join('')}</div>` : '';

  let action = '';
  if (s.status === 'cleaned') {
    action = `
      <div class="mt-5 rounded-2xl bg-forest-50 p-4">
        <div class="font-extrabold text-forest-800">✨ Cleaned by ${esc(s.cleaner)}</div>
        <div class="text-sm text-forest-800/80 mt-0.5">${esc(s.verify_comment || '')}</div>
        <div class="grid grid-cols-2 gap-2 mt-3">
          <div><div class="text-[11px] font-bold text-slate-500 mb-1">BEFORE</div><img src="${esc(s.photos[0])}" class="rounded-xl aspect-square object-cover w-full" alt=""></div>
          <div><div class="text-[11px] font-bold text-slate-500 mb-1">AFTER</div><img src="${esc(s.after_photo)}" class="rounded-xl aspect-square object-cover w-full" alt=""></div>
        </div>
        <div class="text-sm font-bold text-forest-700 mt-3">+${s.awarded} pts · ${s.percent_cleaned}% clean</div>
      </div>`;
  } else if (s.reporter === me) {
    action = `<div class="mt-5 rounded-2xl bg-slate-100 p-4 text-sm text-slate-600 font-medium">You reported this spot. ${s.status === 'claimed' ? esc(s.claimed_by) + ' is cleaning it now 🙌' : 'Waiting for a volunteer to pick it up.'}</div>`;
  } else if (s.status === 'claimed' && s.claimed_by === me) {
    action = `
      <a href="https://www.google.com/maps/dir/?api=1&destination=${s.lat},${s.lng}" target="_blank" rel="noopener"
         class="mt-5 block text-center w-full rounded-2xl bg-slate-100 font-bold py-3.5">🧭 Directions</a>
      <label class="mt-2 block text-center w-full rounded-2xl bg-forest-500 text-white font-extrabold py-4 text-lg shadow-lg cursor-pointer active:scale-[.98] transition">
        📸 I cleaned it: take after photo
        <input type="file" accept="image/*" capture="environment" class="hidden" onchange="submitClean('${s.id}', this)">
      </label>
      <p class="text-xs text-center text-slate-500 mt-2">Take it from the same spot. AI compares it to the before photos.</p>`;
  } else if (s.status === 'claimed') {
    action = `<div class="mt-5 rounded-2xl bg-amber-50 p-4 text-sm text-amber-800 font-semibold">⏳ ${esc(s.claimed_by)} is cleaning this spot.</div>`;
  } else {
    action = `<button onclick="claim('${s.id}')" class="mt-5 w-full rounded-2xl bg-forest-500 text-white font-extrabold py-4 text-lg shadow-lg active:scale-[.98] transition">I'll clean it · +${s.points} pts</button>`;
  }

  openSheet(`
    <div class="mx-auto w-10 h-1.5 rounded-full bg-slate-200 mb-4"></div>
    <div class="flex gap-2 overflow-x-auto no-scrollbar -mx-5 px-5">${photos}</div>
    <div class="flex items-start justify-between mt-4">
      <div>
        <div class="text-xs font-bold uppercase tracking-wide" style="color:${DIFF_COLOR[s.difficulty]}">${DIFF[s.difficulty]} cleanup</div>
        <div class="flex gap-1 mt-1.5">${dots}</div>
      </div>
      <div class="text-right">
        <div class="text-3xl font-extrabold text-forest-700 leading-none">${s.points}</div>
        <div class="text-xs text-slate-500 font-semibold">points</div>
      </div>
    </div>
    <p class="mt-3 text-slate-700">${esc(s.summary)}</p>
    <div class="mt-3 grid grid-cols-2 gap-2 text-sm">
      <div class="rounded-xl bg-slate-50 px-3 py-2">🗑️ <b>${s.bags}</b> bag${s.bags === 1 ? '' : 's'}</div>
      <div class="rounded-xl bg-slate-50 px-3 py-2">⏱️ ~<b>${s.minutes}</b> min</div>
    </div>
    ${hazards}
    <div class="text-xs text-slate-400 mt-3">Reported by ${esc(s.reporter)} · ${timeAgo(s.created_at)}${s.ai_source === 'demo' ? ' · demo score' : ''}</div>
    ${action}`);
}

async function claim(id) {
  const fd = new FormData();
  fd.append('user', me);
  try {
    await api(`/api/spots/${id}/claim`, { method: 'POST', body: fd });
    toast("It's yours! Head there and clean it up 💪");
    await loadSpots();
    openSpot(id);
  } catch (e) { toast(e.message, 'err'); }
}

async function submitClean(id, input) {
  const file = input.files[0];
  input.value = '';
  if (!file) return;
  let blob, pos;
  try {
    blob = await compress(file);
    showLoading('Checking your location…', URL.createObjectURL(blob));
    pos = await getLocation();
  } catch (e) { hideLoading(); return toast(e.message, 'err'); }
  showLoading('AI is verifying your cleanup…', URL.createObjectURL(blob));
  const fd = new FormData();
  fd.append('user', me);
  fd.append('lat', pos.lat);
  fd.append('lng', pos.lng);
  fd.append('photo', blob, 'after.jpg');
  try {
    const r = await api(`/api/spots/${id}/clean`, { method: 'POST', body: fd });
    hideLoading();
    await loadSpots();
    refreshPoints();
    celebrate(r);
  } catch (e) { hideLoading(); toast(e.message, 'err'); }
}

function celebrate(r) {
  openSheet(`
    <div class="mx-auto w-10 h-1.5 rounded-full bg-slate-200 mb-6"></div>
    <div class="text-center">
      <div class="text-6xl">🎉</div>
      <div class="text-4xl font-extrabold text-forest-700 mt-3">+${r.awarded} pts</div>
      <div class="text-slate-600 mt-2">${esc(r.verification.comment)}</div>
      <div class="text-sm text-slate-500 mt-1">${r.verification.percent_cleaned}% cleaned${r.capped ? ' · daily cap reached' : ''}</div>
      <div class="grid grid-cols-2 gap-2 mt-5">
        <img src="${esc(r.spot.photos[0])}" class="rounded-2xl aspect-square object-cover w-full" alt="">
        <img src="${esc(r.spot.after_photo)}" class="rounded-2xl aspect-square object-cover w-full" alt="">
      </div>
      <button onclick="go('me')" class="mt-5 w-full rounded-2xl bg-forest-900 text-white font-bold py-4">See my Generation Score</button>
    </div>`);
}

// ---------- report ----------
function renderPending() {
  $('photoGrid').innerHTML = pending.map((b, i) => `
    <div class="relative aspect-square rounded-2xl overflow-hidden bg-forest-800">
      <img src="${b.url}" class="w-full h-full object-cover" alt="">
      <button onclick="removePending(${i})" class="absolute top-2 right-2 w-8 h-8 rounded-full bg-black/60 text-white font-bold">×</button>
    </div>`).join('');
  $('submitReport').disabled = pending.length === 0;
  $('addPhotoBtn').classList.toggle('hidden', pending.length >= 4);
  $('addPhotoLabel').textContent = pending.length ? `Add another angle (${pending.length}/4)` : 'Take a photo';
}
function removePending(i) { URL.revokeObjectURL(pending[i].url); pending.splice(i, 1); renderPending(); }

async function addFiles(files) {
  for (const f of files) {
    if (pending.length >= 4) break;
    try { const b = await compress(f); b.url = URL.createObjectURL(b); pending.push(b); }
    catch (e) { toast(e.message, 'err'); }
  }
  renderPending();
}

async function submitReport() {
  if (!pending.length) return;
  let pos;
  showLoading('Getting your location…', pending[0].url);
  try {
    pos = await getLocation();
  } catch (e) {
    hideLoading();
    if (!confirm(e.message + '\n\nUse the map center instead?')) return;
    const c = map.getCenter();
    pos = { lat: c.lat, lng: c.lng };
  }
  showLoading('AI is analyzing the mess…', pending[0].url);
  const fd = new FormData();
  fd.append('user', me);
  fd.append('lat', pos.lat);
  fd.append('lng', pos.lng);
  pending.forEach((b, i) => fd.append('photos', b, `photo${i}.jpg`));
  try {
    const r = await api('/api/spots', { method: 'POST', body: fd });
    hideLoading();
    pending.forEach((b) => URL.revokeObjectURL(b.url));
    pending = [];
    renderPending();
    await loadSpots();
    refreshPoints();
    go('map');
    map.setView([r.spot.lat, r.spot.lng], 17);
    openSpot(r.spot.id);
    toast(`Posted! Worth ${r.spot.points} pts to a volunteer (+${r.report_bonus} for you)`);
  } catch (e) { hideLoading(); toast(e.message, 'err'); }
}

// ---------- leaderboard ----------
async function loadLeaders() {
  let rows;
  try { rows = await api('/api/leaderboard'); } catch (e) { return toast(e.message, 'err'); }
  const medal = ['🥇', '🥈', '🥉'];
  const order = [1, 0, 2];
  $('podium').innerHTML = order.map((i) => {
    const r = rows[i];
    if (!r) return '<div></div>';
    const h = ['h-36', 'h-28', 'h-24'][i];
    return `<div class="bg-white rounded-3xl shadow-sm ${h} flex flex-col items-center justify-center px-1 ${r.user === me ? 'ring-2 ring-forest-500' : ''}">
      <div class="text-3xl">${medal[i]}</div>
      <div class="font-extrabold text-sm truncate max-w-full">${esc(r.user)}</div>
      <div class="text-forest-700 font-bold text-sm">${r.points} pts</div></div>`;
  }).join('');
  $('leaderList').innerHTML = rows.length ? rows.map((r, i) => `
    <li class="flex items-center gap-3 px-4 py-3 ${r.user === me ? 'bg-forest-50' : ''}">
      <div class="w-7 text-center font-extrabold text-slate-400">${i + 1}</div>
      <div class="flex-1 min-w-0">
        <div class="font-bold truncate">${esc(r.user)}${r.user === me ? ' (you)' : ''}</div>
        <div class="text-xs text-slate-500">${esc(r.level)} · ${r.cleanups} cleanup${r.cleanups === 1 ? '' : 's'}</div>
      </div>
      <div class="font-extrabold text-forest-700">${r.points}</div>
    </li>`).join('')
    : '<li class="p-6 text-center text-slate-500">No volunteers yet. Be the first!</li>';
}

// ---------- me ----------
let lastProfile = null;
async function loadMe() {
  if (!me) return;
  let p;
  try { p = await api('/api/users/' + encodeURIComponent(me)); } catch (e) { return toast(e.message, 'err'); }
  lastProfile = p;
  $('meScore').textContent = p.points;
  $('myPoints').textContent = p.points;
  $('meName').textContent = p.user;
  $('meLevel').textContent = p.level.title;
  $('meCleanups').textContent = p.cleanups;
  $('meBags').textContent = p.bags;
  $('meRank').textContent = p.rank ? '#' + p.rank : '—';
  const { floor, next } = p.level;
  $('meBar').style.width = next ? `${Math.round(((p.points - floor) / (next - floor)) * 100)}%` : '100%';
  $('meNext').textContent = next ? `${next - p.points} pts to next level` : 'Max level reached 🌍';

  const mine = spots.filter((s) => s.reporter === me || s.cleaner === me || s.claimed_by === me);
  $('myActivity').innerHTML = mine.length ? mine.map((s) => {
    const role = s.cleaner === me ? `Cleaned · +${s.awarded} pts` : s.reporter === me ? 'Reported' : 'Claimed: go clean it!';
    return `<button onclick="go('map'); openSpot('${s.id}')" class="w-full flex items-center gap-3 bg-white rounded-2xl p-2.5 shadow-sm text-left">
      <img src="${esc(s.photos[0])}" class="w-14 h-14 rounded-xl object-cover" alt="">
      <div class="flex-1 min-w-0"><div class="font-bold text-sm truncate">${esc(s.summary)}</div>
      <div class="text-xs text-slate-500">${role} · ${timeAgo(s.created_at)}</div></div></button>`;
  }).join('') : '<div class="text-sm text-slate-500 bg-white rounded-2xl p-4">Nothing yet. Report a spot or claim one on the map.</div>';
}

// Draws the score card to an image so it can be shared to Instagram/Messages.
async function shareCard() {
  const p = lastProfile;
  if (!p) return;
  const W = 1080, H = 1350;
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const g = c.getContext('2d');
  const grad = g.createLinearGradient(0, 0, W, H);
  grad.addColorStop(0, '#16b364'); grad.addColorStop(0.5, '#0d7343'); grad.addColorStop(1, '#07261c');
  g.fillStyle = grad; g.fillRect(0, 0, W, H);
  g.fillStyle = 'rgba(255,255,255,.08)';
  g.beginPath(); g.arc(W - 80, 120, 300, 0, Math.PI * 2); g.fill();
  g.beginPath(); g.arc(80, H - 60, 260, 0, Math.PI * 2); g.fill();

  g.fillStyle = 'rgba(255,255,255,.8)';
  g.font = '600 40px Inter, sans-serif';
  g.fillText('MY GENERATION SCORE', 90, 200);
  g.fillStyle = '#fff';
  g.font = '800 260px Inter, sans-serif';
  g.fillText(String(p.points), 80, 470);
  g.font = '700 64px Inter, sans-serif';
  g.fillText(p.user, 90, 590);
  g.font = '600 46px Inter, sans-serif';
  g.fillStyle = 'rgba(255,255,255,.9)';
  g.fillText('🏅 ' + p.level.title, 90, 670);

  const stats = [[p.cleanups, 'cleanups'], [p.bags, 'bags removed'], [p.rank ? '#' + p.rank : '—', 'rank']];
  stats.forEach(([v, l], i) => {
    const x = 90 + i * 310;
    g.fillStyle = 'rgba(255,255,255,.12)';
    g.beginPath(); g.roundRect(x, 780, 280, 220, 36); g.fill();
    g.fillStyle = '#fff'; g.font = '800 90px Inter, sans-serif'; g.fillText(String(v), x + 36, 900);
    g.fillStyle = 'rgba(255,255,255,.8)'; g.font = '600 34px Inter, sans-serif'; g.fillText(l, x + 36, 960);
  });
  g.fillStyle = '#fff';
  g.font = '700 44px Inter, sans-serif';
  g.fillText('I clean up my city. 🌍', 90, 1150);
  g.fillStyle = 'rgba(255,255,255,.7)';
  g.font = '500 34px Inter, sans-serif';
  g.fillText('Hacker Environment · ' + location.host, 90, 1220);

  const blob = await new Promise((r) => c.toBlob(r, 'image/png'));
  const file = new File([blob], 'generation-score.png', { type: 'image/png' });
  try {
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      await navigator.share({ files: [file], text: `My Generation Score is ${p.points} 🌱` });
      return;
    }
  } catch (e) { if (e.name === 'AbortError') return; }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'generation-score.png';
  a.click();
}

// ---------- boot ----------
async function boot() {
  initMap();
  $('photoInput').addEventListener('change', (e) => { addFiles([...e.target.files]); e.target.value = ''; });
  $('galleryInput').addEventListener('change', (e) => { addFiles([...e.target.files]); e.target.value = ''; });
  renderPending();
  go('map');
  if (!me) askName(); else refreshPoints();
  $('nameInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveName(); });
  api('/api/status').then((s) => {
    $('aiBadge').textContent = s.ai === 'gemini' ? 'AI online · Gemini' : 'Demo mode (no AI key)';
  }).catch(() => {});
  await loadSpots();
  setInterval(loadSpots, 20000);
}
boot();
