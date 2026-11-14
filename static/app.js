// Hacker Environment - client
const UOFA = [53.5232, -113.5263];
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let me = storeGet('he_user');
let spots = [];
let map, markersLayer, meMarker;
let myLocation = null;
let aiStatus = null;
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
  if (!res.ok) {
    const detail = body && body.detail;
    const e = new Error(typeof detail === 'string' ? detail : (detail && detail.message) || `Something went wrong (${res.status})`);
    if (detail && typeof detail === 'object') e.assessment = detail.analysis;
    throw e;
  }
  return body;
}

function assessmentCard(d) {
  if (!d) return '';
  const labels = { litter_detected: 'Litter detected', no_litter_detected: 'No litter detected', unable_to_determine: 'Unable to determine' };
  const title = labels[d.outcome] || labels.unable_to_determine;
  const color = d.outcome === 'litter_detected' ? 'text-forest-800 bg-forest-50'
    : d.outcome === 'no_litter_detected' ? 'text-slate-700 bg-slate-100' : 'text-amber-900 bg-amber-50';
  const items = (d.objects || []).map((o) => `<li><b>${esc(o.object_type || 'Unidentified object')} × ${Math.max(1, Number(o.count) || 1)}</b> · object ${Number(o.object_confidence) || 0}% · ${esc((o.discard_status || 'uncertain').replaceAll('_', ' '))}${o.disposal_category && o.disposal_category.toLowerCase() !== 'unknown' ? ` · disposal: ${esc(o.disposal_category)}` : ''}</li>`).join('');
  const unavailable = d.source === 'demo' ? '<div class="mt-2 text-xs font-semibold">AI service unavailable; no detection result was produced.</div>' : '';
  return `<section class="mt-4 rounded-2xl p-4 ${color}"><div class="font-extrabold">Photo assessment · ${esc(title)}</div><div class="mt-1 text-sm">Litter decision confidence: ${Number(d.litter_confidence) || 0}%</div>${items ? `<ul class="mt-2 space-y-1 text-sm list-disc pl-5">${items}</ul>` : '<div class="mt-2 text-sm">No waste objects were recognized.</div>'}<p class="mt-2 text-sm">${esc(d.rationale || d.summary || '')}</p>${unavailable}</section>`;
}

function showLoading(text, imgUrl) {
  $('loadingText').textContent = text;
  $('loadingImg').style.backgroundImage = imgUrl ? `url(${imgUrl})` : '';
  $('loading').classList.remove('hidden');
}
const hideLoading = () => $('loading').classList.add('hidden');

// Phones first return a rough Wi-Fi/cell fix (often 200-500 m off), then real GPS.
// So collect fixes for a few seconds and keep the most accurate one.
// Updated from the server's /api/status (MAX_ACTION_LOCATION_ACCURACY_M on Render), so the
// app and server always agree and it can be loosened for an indoor demo without a code change.
let PHOTO_LOCATION_ACCURACY_M = 75;
const MAX_REPORT_LOCATION_ACCURACY_M = 75;
function locError(kind, accuracy) {
  const e = new Error(kind === 'denied' ? 'Location access is turned off for this site.'
    : kind === 'unsupported' ? 'This browser has no location support.'
    : kind === 'inaccurate' ? `GPS is only accurate to about ${Math.round(accuracy)} m.`
    : "Couldn't get a GPS signal.");
  e.kind = kind;  // 'denied' | 'unavailable' | 'unsupported' | 'inaccurate'
  e.accuracy = accuracy;
  return e;
}

function getLocation({ maxWaitMs = 12000, maxAccuracyM = 1000 } = {}) {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(locError('unsupported'));
    let best = null, id = null, done = false, denied = false;
    const readFix = (p) => {
      const accuracy = p.coords.accuracy;
      if (!Number.isFinite(accuracy) || accuracy <= 0 || !Number.isFinite(p.coords.latitude) || !Number.isFinite(p.coords.longitude)) return null;
      return { lat: p.coords.latitude, lng: p.coords.longitude, acc: Math.ceil(accuracy), timestamp: p.timestamp };
    };
    const finishWith = (fix) => {
      if (done || !fix || fix.acc > maxAccuracyM) return false;
      done = true;
      navigator.geolocation.clearWatch(id);
      setMyLocation(fix, true);
      resolve(fix);
      return true;
    };
    const finish = () => {
      if (done) return;
      navigator.geolocation.clearWatch(id);
      if (denied) { done = true; return reject(locError('denied')); }
      // Ask for one final fresh high-accuracy reading; never substitute a two-minute-old cached fix.
      navigator.geolocation.getCurrentPosition(
        (p) => {
          const candidate = readFix(p);
          if (candidate && (!best || candidate.acc < best.acc)) best = candidate;
          if (!finishWith(best)) {
            done = true;
            reject(locError(best ? 'inaccurate' : 'unavailable', best?.acc));
          }
        },
        (err) => {
          if (best && finishWith(best)) return;
          done = true;
          reject(locError(err.code === 1 ? 'denied' : best ? 'inaccurate' : 'unavailable', best?.acc));
        },
        { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 });
    };
    id = navigator.geolocation.watchPosition(
      (p) => {
        const fix = readFix(p);
        if (!fix) return;
        if (!best || fix.acc <= best.acc) best = fix;
        if (best.acc <= maxAccuracyM) finishWith(best);
      },
      (err) => { if (err.code === 1) { denied = true; finish(); } },  // keep waiting on timeouts
      { enableHighAccuracy: true, timeout: maxWaitMs, maximumAge: 0 });
    setTimeout(finish, maxWaitMs);
  });
}

// Like getLocation, but if it fails we explain how to turn location on and let the
// user retry, so a photo they just took isn't thrown away. Resolves null on Cancel.
async function getLocationOrAsk(loadingImg, requirePhotoAccuracy = false) {
  for (;;) {
    try {
      return await getLocation({ maxWaitMs: requirePhotoAccuracy ? 18000 : 12000,
        maxAccuracyM: requirePhotoAccuracy ? PHOTO_LOCATION_ACCURACY_M : 500 });
    } catch (e) {
      e.photoAccuracy = requirePhotoAccuracy;
      hideLoading();
      if (!(await askForLocation(e))) return null;
      showLoading('Getting a precise GPS fix…', loadingImg);
    }
  }
}

// Apps like Instagram, WhatsApp and Discord open links in their own browser,
// which often blocks location without ever asking.
const IN_APP_BROWSER = /FBAN|FBAV|Instagram|WhatsApp|Discord|Snapchat|LinkedInApp|TikTok|musical_ly|Line\/|; wv\)/i.test(navigator.userAgent);

async function locPermission() {
  try { return (await navigator.permissions.query({ name: 'geolocation' })).state; }  // granted | prompt | denied
  catch { return 'unknown'; }
}

// Browsers only show their "Allow location?" pop-up reliably when the request comes
// straight from a tap, so this must be called first thing inside a click handler.
function requestLocationFromTap() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve('unsupported');
    navigator.geolocation.getCurrentPosition(
      (p) => { setMyLocation({ lat: p.coords.latitude, lng: p.coords.longitude, acc: Math.ceil(p.coords.accuracy) }, true); resolve('granted'); },
      (err) => resolve(err.code === 1 ? 'denied' : 'granted'),  // timeout = permission is fine, GPS just slow
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
}

function fixSteps() {
  const ua = navigator.userAgent;
  if (IN_APP_BROWSER) return [
    "You opened this link inside another app (like Instagram, WhatsApp or Discord), and those block location",
    'Tap <b>⋯</b> or the share icon → <b>Open in Safari</b> / <b>Open in Chrome</b>',
    'Allow location when the browser asks'];
  if (/iPhone|iPad|iPod/.test(ua)) return [
    'Open the <b>Settings</b> app → <b>Privacy &amp; Security</b> → <b>Location Services</b>, and make sure it is <b>on</b>',
    'In that list tap <b>Safari Websites</b> (using Chrome? tap <b>Chrome</b>) → <b>While Using the App</b>, and turn on <b>Precise Location</b>',
    'Still blocked? In Safari tap <b>aA</b> in the address bar → <b>Website Settings</b> → <b>Location</b> → <b>Ask</b>',
    'Come back here and tap the green button'];
  if (/Android/.test(ua)) return [
    'Swipe down from the top of the screen and make sure <b>Location</b> is on',
    'Tap the <b>🔒 / settings icon</b> left of the web address → <b>Permissions</b> → <b>Location</b> → <b>Allow</b>',
    'Come back here and tap the green button'];
  return ['Click the <b>🔒 icon</b> left of the web address', 'Set <b>Location</b> to <b>Allow</b>', 'Tap the green button'];
}

function showLocModal({ title, msg, steps = [], primary, secondary = 'Cancel' }) {
  $('locTitle').textContent = title;
  $('locMsg').innerHTML = msg;
  $('locSteps').innerHTML = steps.map((s, i) => `<li class="flex gap-2"><span class="font-extrabold text-forest-600">${i + 1}.</span><span>${s}</span></li>`).join('');
  $('locRetry').textContent = primary;
  $('locCancel').textContent = secondary;
  $('locModal').classList.remove('hidden');
}
const hideLocModal = () => $('locModal').classList.add('hidden');

function askForLocation(err) {
  const denied = err.kind === 'denied' || err.kind === 'unsupported';
  const inaccurate = err.kind === 'inaccurate';
  showLocModal(denied
    ? { title: 'Location access needed', primary: 'Try again', steps: fixSteps(),
        msg: 'We use your location to prove the photo was taken at the spot. Your photo is saved: turn location on, then try again.' }
    : { title: inaccurate ? 'GPS needs a clearer fix' : 'No GPS signal yet', primary: 'Try again',
        msg: `${inaccurate ? `The current reading is only accurate to about ±${Math.round(err.accuracy)} m; ${err.photoAccuracy ? `photos need ±${PHOTO_LOCATION_ACCURACY_M} m or better` : 'distances may be off by that much'}. ` : "Your phone hasn't found you yet. "}Turn on Precise Location, then step outside or near a window and try again. ${err.photoAccuracy ? 'Your photo is saved.' : 'Try locating yourself again.'}` });
  return new Promise((resolve) => {
    $('locRetry').onclick = () => {
      // Ask again from inside the tap so the browser can show its pop-up.
      requestLocationFromTap();
      hideLocModal(); resolve(true);
    };
    $('locCancel').onclick = () => { hideLocModal(); resolve(false); };
  });
}

// First-run step (and the map banner): explain why, then ask from a tap.
async function checkLocationAccess() {
  const state = await locPermission();
  $('locBanner').classList.toggle('hidden', state === 'granted');
  if (state === 'granted') { ping(); return; }
  if (storeGet('he_loc_later')) return;  // they said "Not now" before: just keep the banner
  showLocationGate(state);
}

function showLocationGate(state) {
  const blocked = state === 'denied' || IN_APP_BROWSER;
  showLocModal(blocked
    ? { title: 'Location is blocked', primary: "I've turned it on", secondary: 'Not now', steps: fixSteps(),
        msg: 'Your browser is blocking location for this site, so it can\'t ask you. Turn it on like this:' }
    : { title: 'Turn on location', primary: '📍 Allow location', secondary: 'Not now',
        msg: 'Hacker Environment uses your location to show cleanups near you and to prove photos are taken at the spot. Tap below, then choose <b>Allow</b>.' });
  $('locRetry').onclick = async () => {
    const result = await requestLocationFromTap();  // first thing in the tap -> browser pop-up
    if (result === 'granted') {
      hideLocModal(); storeSet('he_loc_later', ''); $('locBanner').classList.add('hidden');
      toast('📍 Location on. Thanks!'); ping();
    } else {
      showLocationGate('denied');
    }
  };
  $('locCancel').onclick = () => { hideLocModal(); storeSet('he_loc_later', '1'); ping(); };
}

// Live position: keeps distances and the blue "you" dot up to date while the app is open.
let watchId = null;
function startTracking() {
  if (watchId !== null || !navigator.geolocation) return;
  watchId = navigator.geolocation.watchPosition(
    (p) => setMyLocation({ lat: p.coords.latitude, lng: p.coords.longitude, acc: Math.round(p.coords.accuracy) }),
    () => {},
    { enableHighAccuracy: true, maximumAge: 5000 });
}

let accCircle = null;
function setMyLocation(fix, freshRead = false) {
  if (!Number.isFinite(fix.lat) || !Number.isFinite(fix.lng) || !Number.isFinite(fix.acc) || fix.acc <= 0) return;
  // Ignore a rougher fix if we got a precise one in the last minute.
  if (!freshRead && myLocation && fix.acc > Math.max(100, myLocation.acc * 2) && Date.now() - myLocation.t < 60000) return;
  myLocation = { ...fix, t: Date.now() };
  if (!map) return;
  if (meMarker) meMarker.setLatLng([fix.lat, fix.lng]);
  else meMarker = L.circleMarker([fix.lat, fix.lng], { radius: 8, color: '#fff', weight: 3, fillColor: '#2563eb', fillOpacity: 1 }).addTo(map);
  if (accCircle) accCircle.setLatLng([fix.lat, fix.lng]).setRadius(fix.acc);
  else accCircle = L.circle([fix.lat, fix.lng], { radius: fix.acc, color: '#2563eb', weight: 1, fillOpacity: 0.08, interactive: false }).addTo(map);
  renderNearest();
  if ($('view-tasks').classList.contains('active')) loadTasks();
}

// Distance from me to a spot, live (null if we don't know where I am).
function distTo(s) {
  return myLocation ? Math.round(map.distance([myLocation.lat, myLocation.lng], [s.lat, s.lng])) : null;
}
function fmtDist(d) {
  if (d == null) return '';
  if (d <= 15 && myLocation.acc <= 30) return "📍 You're here";
  if (d <= myLocation.acc) return `Near this spot (GPS ±${myLocation.acc} m)`;
  const step = myLocation.acc >= 500 ? 500 : myLocation.acc >= 150 ? 100 : myLocation.acc >= 50 ? 25 : 10;
  const rounded = Math.round(d / step) * step;
  const txt = rounded < 1000 ? `${rounded} m away` : `${(rounded / 1000).toFixed(1)} km away`;
  return myLocation.acc > 30 ? `≈${txt} (GPS ±${myLocation.acc} m)` : txt;
}

// ---------- photo origin checks (before compressing, which strips metadata) ----------
// Not proof on their own (a determined cheater can work around client checks), but they stop
// the easy routes: old gallery photos, screenshots, and files that carry an AI-generator or
// editing-app signature. The server adds duplicate detection (photo fingerprints), and anyone
// passing by can flag a "cleaned" spot that's still dirty.
const MAX_PHOTO_AGE_MS = 10 * 60 * 1000;
// Note: many phones (Google Pixel, newer Samsung Galaxy and iPhone models) attach Content
// Credentials (C2PA) to EVERY normal camera photo, so a C2PA label alone is not suspicious.
// What matters is the "digital source type" inside it (and in IPTC/XMP): AI generators and
// AI edits (e.g. Galaxy AI, Magic Editor) write "trainedAlgorithmicMedia"; a real camera writes
// "digitalCapture".
const PROVENANCE_MARKERS = [
  ['trainedAlgorithmicMedia', 'AI-generated or AI-edited image tag'],
  ['compositeSynthetic', 'AI-generated image tag'],
  ['Midjourney', 'Midjourney'], ['DALL-E', 'DALL-E'], ['Stable Diffusion', 'Stable Diffusion'],
  ['stable-diffusion', 'Stable Diffusion'], ['NovelAI', 'NovelAI'], ['Adobe Firefly', 'Adobe Firefly'],
  // editing apps
  // Note: NOT "Photoshop 3.0": that's only the name of the IPTC block iPhones write into normal photos.
  ['Adobe Photoshop', 'Photoshop'], ['GIMP', 'GIMP'], ['Snapseed', 'Snapseed'],
  ['Lightroom', 'Lightroom'], ['Picsart', 'Picsart'], ['Canva', 'Canva'], ['Facetune', 'Facetune'], ['Meitu', 'Meitu'],
];

// Tell the server why a photo was refused in the app, so false alarms show up on /api/status.
function reportRejection(reason, file) {
  const fd = new FormData();
  fd.append('user', me || '');
  fd.append('reason', reason);
  fd.append('detail', `${file.type || '?'} ${Math.round(file.size / 1024)} KB, age ${Math.round((Date.now() - (file.lastModified || Date.now())) / 60000)} min`);
  fetch('/api/client-reject', { method: 'POST', body: fd }).catch(() => {});
}

async function checkPhotoOrigin(file) {
  try { await checkPhotoOriginInner(file); } catch (e) { reportRejection(e.message, file); throw e; }
}

async function checkPhotoOriginInner(file) {
  if (file.lastModified && Date.now() - file.lastModified > MAX_PHOTO_AGE_MS) {
    throw new Error('This photo is not new. Take a fresh photo with the camera right now.');
  }
  if (file.type === 'image/png') {
    throw new Error("Screenshots aren't accepted. Take a photo with the camera.");
  }
  // Metadata lives at the start of the file (EXIF/XMP/C2PA). For JPEGs, only scan up to the
  // start of the image data (SOS marker FF DA), so random pixel bytes can't cause a false alarm.
  let head = new Uint8Array(await file.slice(0, 512 * 1024).arrayBuffer());
  let text;
  if (head[0] === 0xff && head[1] === 0xd8) {
    // Walk the JPEG segments (APP1 EXIF/XMP, APP11 C2PA, ...) until the image data starts.
    let i = 2;
    while (i + 3 < head.length && head[i] === 0xff && head[i + 1] !== 0xda) {
      i += 2 + ((head[i + 2] << 8) | head[i + 3]);
    }
    text = new TextDecoder('latin1').decode(head.subarray(0, Math.min(i, head.length)));
  } else {
    const tail = new Uint8Array(await file.slice(Math.max(0, file.size - 128 * 1024)).arrayBuffer());
    text = new TextDecoder('latin1').decode(head) + new TextDecoder('latin1').decode(tail);
  }
  const lower = text.toLowerCase();  // e.g. "compositeWithTrainedAlgorithmicMedia" has a capital T
  const hit = PROVENANCE_MARKERS.find(([m]) => lower.includes(m.toLowerCase()));
  if (hit) throw new Error(`This photo looks edited or AI-made (${hit[1]}). Take a new photo with the camera.`);
}

// Keep enough detail to recognize small or distant litter, but small enough to upload on venue Wi-Fi
// and to fit many photos in the free database.
async function compress(file, max = 1600, quality = 0.85) {
  await checkPhotoOrigin(file);
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
  if (tab === 'tasks') loadTasks();
}

// Clickable username that opens their profile.
const userLink = (n) => `<a data-user="${esc(n)}" onclick="openUser(this.dataset.user)" class="font-semibold underline decoration-dotted cursor-pointer">${esc(n)}</a>`;
const money = (cents) => '$' + (cents / 100).toFixed(cents % 100 ? 2 : 0);

// ---------- tasks: nearest / most points ----------
let sortMode = storeGet('he_sort') || 'near';
function setSort(m) { sortMode = m; storeSet('he_sort', m); loadTasks(); }

async function loadTasks() {
  $('sortNear').className = 'rounded-xl py-2 ' + (sortMode === 'near' ? 'bg-white text-forest-900' : 'text-white/80');
  $('sortPoints').className = 'rounded-xl py-2 ' + (sortMode === 'points' ? 'bg-white text-forest-900' : 'text-white/80');
  if (!myLocation) { try { await getLocation(); } catch {} }
  const c = map.getCenter();
  const from = myLocation || { lat: c.lat, lng: c.lng };
  const dist = (s) => map.distance([from.lat, from.lng], [s.lat, s.lng]);
  const fmt = (m) => (myLocation ? fmtDist(Math.round(m)) : `${(m / 1000).toFixed(1)} km`).replace(' away', '');
  const list = spots
    .filter((s) => s.status === 'open' || (s.status === 'claimed' && s.claimed_by === me))
    .map((s) => ({ ...s, d: dist(s) }))
    .sort((a, b) => (sortMode === 'near' ? a.d - b.d : b.points - a.points || a.d - b.d));
  $('tasksSub').textContent = !myLocation ? 'Location off: distances are from the map center.'
    : `${list.length} spot${list.length === 1 ? '' : 's'} waiting for you${myLocation.acc > 100 ? ` · weak GPS (±${myLocation.acc} m)` : ''}.`;
  $('taskList').innerHTML = list.length ? list.map((s, i) => {
    const tag = s.claimed_by === me ? '<span class="text-amber-600">⏳ Yours: go clean</span>'
      : s.reporter === me ? '<span class="text-forest-700 font-semibold">Your report · tap to clean</span>' : '';
    const best = i === 0 ? (sortMode === 'near' ? 'Closest' : 'Top points') : '';
    return `<button onclick="go('map'); map.setView([${s.lat}, ${s.lng}], 17); openSpot('${s.id}')"
      class="w-full flex items-center gap-3 bg-white rounded-2xl p-2.5 shadow-sm text-left ${i === 0 ? 'ring-2 ring-forest-400' : ''}">
      <img src="${esc(s.photos[0])}" class="w-16 h-16 rounded-xl object-cover" alt="">
      <div class="flex-1 min-w-0">
        ${best ? `<div class="text-[10px] font-extrabold uppercase tracking-wide text-forest-600">${best}</div>` : ''}
        <div class="font-bold text-sm truncate">${esc(s.summary)}</div>
        <div class="text-xs text-slate-500 mt-0.5">${fmt(s.d).startsWith('📍') ? '' : '📍 '}${fmt(s.d)} · <span style="color:${DIFF_COLOR[s.difficulty]}">${DIFF[s.difficulty]}</span> · ~${s.minutes} min ${tag ? '· ' + tag : ''}</div>
      </div>
      <div class="text-right shrink-0"><div class="text-xl font-extrabold text-forest-700 leading-none">${s.points}</div><div class="text-[10px] text-slate-500 font-semibold">pts</div></div>
    </button>`;
  }).join('') : '<div class="text-sm text-slate-500 bg-white rounded-2xl p-6 text-center">No open spots right now. Report one with the + button!</div>';
}

// ---------- user profile: donate / report ----------
function donationRules(d) {
  return d.rules.map((r) => `
    <div class="flex items-center gap-2 text-sm py-1">
      <span>${r.ok ? '✅' : r.permanent ? '⛔' : '⬜'}</span>
      <span class="flex-1 ${r.ok ? 'text-slate-700' : 'text-slate-500'}">${esc(r.label)}</span>
      ${r.permanent ? '' : `<span class="text-xs font-bold text-slate-400">${Math.min(r.have, r.need)}/${r.need}</span>`}
    </div>`).join('');
}

function donationCard(d, mine) {
  // Only "Verified · donations on" is shown publicly. Everyone else's status stays private.
  if (!mine && !d.eligible) return '';
  if (d.locked) {
    return `<div class="rounded-2xl bg-slate-100 p-4 text-sm text-slate-600">
      <div class="font-extrabold">Donations aren't available for this account.</div>
    </div>`;
  }
  if (d.eligible) {
    return `<div class="rounded-2xl bg-amber-50 p-4">
      <div class="font-extrabold text-amber-900">💛 Verified Cleaner: donations on</div>
      <div class="text-sm text-amber-900/80 mt-1">${money(d.released_cents)} received · ${money(d.held_cents)} on 7-day hold · ${money(d.room_cents)} left this month</div>
    </div>`;
  }
  return `<div class="rounded-2xl bg-white p-4 shadow-sm">
    <div class="font-extrabold text-slate-800">💛 Unlock donations</div>
    <div class="text-xs text-slate-500 mt-0.5 mb-2">Only for proven volunteers. One problem locks it forever.</div>
    ${donationRules(d)}
  </div>`;
}

// ---------- donation goals ("I'll remove X kg of trash if I get $Y") ----------
function bar(pct, cls) {
  return `<div class="h-2 rounded-full bg-black/10 overflow-hidden"><div class="h-full rounded-full ${cls}" style="width:${Math.min(100, Math.round(pct))}%"></div></div>`;
}

function goalCard(g, name, mine) {
  if (!g) return '';
  const money_pct = g.target_cents ? (g.raised_cents / g.target_cents) * 100 : 0;
  const kg_pct = g.promise_kg ? (g.kg_done / g.promise_kg) * 100 : 0;
  return `<div class="rounded-2xl bg-forest-50 p-4">
    <div class="font-extrabold text-forest-900">🎯 ${mine ? 'My promise' : `${esc(name)}'s promise`}</div>
    <div class="text-sm text-forest-900/80 mt-0.5">"I'll remove <b>${g.promise_kg} kg</b> of trash if I get <b>${money(g.target_cents)}</b> in donations."</div>
    <div class="mt-3 text-xs font-bold text-slate-600 flex justify-between"><span>💛 Donations</span><span>${money(g.raised_cents)} / ${money(g.target_cents)}</span></div>
    ${bar(money_pct, 'bg-amber-400')}
    <div class="mt-2 text-xs font-bold text-slate-600 flex justify-between"><span>🗑️ Trash removed</span><span>~${g.kg_done} / ${g.promise_kg} kg</span></div>
    ${bar(kg_pct, 'bg-forest-500')}
    <div class="text-[11px] text-slate-500 mt-2">Counted since the promise was made. ~5 kg per full bag.</div>
  </div>`;
}

function goalForm(g) {
  return `<details class="mt-3 rounded-2xl bg-white p-4 shadow-sm" ${g ? '' : 'open'}>
    <summary class="font-extrabold text-slate-800 cursor-pointer">🎯 ${g ? 'Change my promise' : 'Make a promise to donors'}</summary>
    <div class="text-xs text-slate-500 mt-1">Tell people what their donations will get done.</div>
    <div class="grid grid-cols-2 gap-2 mt-3 text-sm">
      <label class="block"><span class="text-xs font-bold text-slate-500">I'll remove (kg)</span>
        <input id="goalKg" type="number" min="10" max="1000" value="${g ? g.promise_kg : 100}" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 font-bold"></label>
      <label class="block"><span class="text-xs font-bold text-slate-500">If I get ($)</span>
        <input id="goalUsd" type="number" min="5" max="50" value="${g ? g.target_cents / 100 : 50}" class="mt-1 w-full rounded-xl border border-slate-200 px-3 py-2 font-bold"></label>
    </div>
    <button onclick="saveGoal()" class="mt-3 w-full rounded-2xl bg-forest-500 text-white font-extrabold py-3 active:scale-[.98] transition">Save promise</button>
  </details>`;
}

async function saveGoal() {
  const fd = new FormData();
  fd.append('user', me);
  fd.append('promise_kg', Math.round(Number($('goalKg').value)));
  fd.append('target_dollars', Math.round(Number($('goalUsd').value)));
  try {
    await api(`/api/users/${encodeURIComponent(me)}/goal`, { method: 'POST', body: fd });
    toast('🎯 Promise saved. Donors can see it on your profile.');
    loadMe();
  } catch (e) { toast(e.message, 'err'); }
}

async function openUser(name) {
  let p;
  try { p = await api('/api/users/' + encodeURIComponent(name)); } catch (e) { return toast(e.message, 'err'); }
  const d = p.donations;
  const isMe = name === me;
  const donate = !isMe && d.eligible ? `
    <div class="mt-4 font-extrabold text-slate-800">Support ${esc(name)}</div>
    <div class="grid grid-cols-3 gap-2 mt-2">
      ${[2, 5, 10].map((a) => `<button data-user="${esc(name)}" onclick="donateTo(this.dataset.user, ${a})" class="rounded-2xl bg-amber-400 text-amber-950 font-extrabold py-3 active:scale-95 transition">$${a}</button>`).join('')}
    </div>
    <p class="text-[11px] text-slate-500 mt-2 text-center">Held 7 days and refunded if any problem shows up. Demo: no real money moves yet.</p>` : '';
  openSheet(`
    <div class="mx-auto w-10 h-1.5 rounded-full bg-slate-200 mb-4"></div>
    <div class="flex items-center gap-3">
      <div class="w-14 h-14 rounded-full bg-forest-100 grid place-items-center text-2xl">${d.eligible ? '💛' : '🌱'}</div>
      <div class="flex-1 min-w-0">
        <div class="text-xl font-extrabold truncate">${esc(name)}</div>
        <div class="text-sm text-slate-500">${esc(p.level.title)} · ${p.points} pts${p.rank ? ' · #' + p.rank : ''}</div>
      </div>
    </div>
    <div class="grid grid-cols-3 gap-2 mt-4 text-center">
      <div class="bg-slate-50 rounded-2xl py-2"><div class="text-lg font-extrabold">${p.cleanups}</div><div class="text-[11px] text-slate-500">cleanups</div></div>
      <div class="bg-slate-50 rounded-2xl py-2"><div class="text-lg font-extrabold">${p.reports}</div><div class="text-[11px] text-slate-500">reports</div></div>
      <div class="bg-slate-50 rounded-2xl py-2"><div class="text-lg font-extrabold">${p.bags}</div><div class="text-[11px] text-slate-500">bags</div></div>
    </div>
    <div class="mt-4">${donationCard(d, isMe)}</div>
    ${p.goal ? `<div class="mt-3">${goalCard(p.goal, name, isMe)}</div>` : ''}
    ${donate}`);
}

async function donateTo(name, amount) {
  const fd = new FormData();
  fd.append('user', me);
  fd.append('amount', amount);
  try {
    const r = await api(`/api/users/${encodeURIComponent(name)}/donate`, { method: 'POST', body: fd });
    toast(`💛 ${money(r.amount_cents)} to ${name}, released in ${r.held_days} days`);
    openUser(name);
  } catch (e) { toast(e.message, 'err'); }
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
  checkLocationAccess();
  loadSpots();
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
  else if (s.status === 'pending') { color = '#6366f1'; label = '🔍'; }
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
  renderNearest();
}

function nearestOpen() {
  if (!myLocation) return null;
  return spots.filter((s) => s.status === 'open' && s.reporter !== me)
    .sort((a, b) => distTo(a) - distTo(b))[0] || null;
}

function renderNearest() {
  const el = $('nearestTask');
  if (!el) return;
  const nearest = nearestOpen();
  el.classList.toggle('hidden', !nearest);
  if (nearest) el.innerHTML = `<div class="text-[10px] uppercase tracking-wide font-extrabold text-forest-700">Closest open cleanup</div><div class="font-bold text-sm truncate mt-0.5">${esc(nearest.summary)}</div><div class="text-xs text-slate-500">${fmtDist(distTo(nearest))} · ${nearest.points} pts</div>`;
}

function openNearestTask() {
  const nearest = nearestOpen();
  if (nearest) openSpot(nearest.id);
}

// Tell the server we're around (and adopt its spelling of our name).
async function ping() {
  if (!me) return;
  const fd = new FormData();
  fd.append('user', me);
  // Only read location in the background once it's allowed; asking is done from a tap.
  if ((await locPermission()) !== 'denied') {
    try {
      const p = await getLocation({ maxWaitMs: 12000, maxAccuracyM: MAX_REPORT_LOCATION_ACCURACY_M });
      fd.append('lat', p.lat); fd.append('lng', p.lng); fd.append('accuracy_m', p.acc);
    } catch {}
  }
  try {
    // The server matches names ignoring case ("p" = "P"); use its spelling so the app agrees.
    const r = await api('/api/ping', { method: 'POST', body: fd });
    if (r && r.user && r.user !== me) { me = r.user; storeSet('he_user', me); }
  } catch {}
  if ((await locPermission()) !== 'denied') startTracking();
  $('locBanner').classList.toggle('hidden', !!myLocation);
}

async function locateMe() {
  const p = await getLocationOrAsk();
  if (!p) return;
  map.setView([p.lat, p.lng], 17);
  if (p.acc > 100) toast(`GPS is weak (±${p.acc} m). Step outside or turn on precise location.`, 'err');
}

// ---------- spot sheet ----------
function openSheet(html) {
  $('sheetBody').innerHTML = html;
  $('sheetBackdrop').classList.remove('hidden');
  requestAnimationFrame(() => $('sheet').classList.add('open'));
  startAfterCountdown();
}

// Solo cleanups (cleaning your own spot) need SOLO_MIN_SECONDS between starting and the after
// photo. Show a countdown on the button instead of letting people hit an error.
const SOLO_MIN_SECONDS = 60;
let afterTimer = null;
function startAfterCountdown() {
  clearInterval(afterTimer);
  const tick = () => {
    const btn = document.querySelector('.after-btn');
    if (!btn) return clearInterval(afterTimer);
    const left = Math.ceil((Number(btn.dataset.unlock) - Date.now()) / 1000);
    const locked = left > 0;
    btn.classList.toggle('opacity-50', locked);
    btn.classList.toggle('pointer-events-none', locked);
    btn.querySelector('.after-label').textContent = locked
      ? `⏳ Keep cleaning: after photo in ${left} s` : '📸 Upload after photo';
    if (!locked) clearInterval(afterTimer);
  };
  tick();
  afterTimer = setInterval(tick, 1000);
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

  const beforeAfter = (extra = '') => `
        <div class="grid grid-cols-${extra ? 3 : 2} gap-2 mt-3">
          <div><div class="text-[11px] font-bold text-slate-500 mb-1">BEFORE</div><img src="${esc(s.task_before_photo || s.photos[0])}" class="rounded-xl aspect-square object-cover w-full" alt=""></div>
          <div><div class="text-[11px] font-bold text-slate-500 mb-1">AFTER</div><img src="${esc(s.after_photo)}" class="rounded-xl aspect-square object-cover w-full" alt=""></div>
          ${extra ? `<div><div class="text-[11px] font-bold text-slate-500 mb-1">CHECK</div><img src="${esc(extra)}" class="rounded-xl aspect-square object-cover w-full" alt=""></div>` : ''}
        </div>`;
  const camBtn = (label, cls, handler) => `
      <label class="block text-center w-full rounded-2xl ${cls} font-extrabold py-4 shadow cursor-pointer active:scale-[.98] transition">
        ${label}
        <input type="file" accept="image/*" capture="environment" class="hidden" onchange="${handler}">
      </label>`;
  const note = s.verify_note ? `<div class="mt-4 rounded-2xl bg-slate-100 p-3 text-sm text-slate-600">${esc(s.verify_note)}</div>` : '';
  const detection = assessmentCard(s.detection);

  let action = '';
  if (s.status === 'cleaned') {
    const by = s.verified_by ? `✅ Verified in person by ${esc(s.verified_by)}` : esc(s.verify_note || 'Approved by the AI check.');
    action = `
      <div class="mt-5 rounded-2xl bg-forest-50 p-4">
        <div class="font-extrabold text-forest-800">✨ Cleaned by ${userLink(s.cleaner)}</div>
        <div class="text-sm text-forest-800/80 mt-0.5">${by}</div>
        ${beforeAfter(s.verify_photo)}
        <div class="text-sm font-bold text-forest-700 mt-3">+${s.awarded} pts · ${s.percent_cleaned}% clean</div>
      </div>
      ${s.cleaner !== me ? `<div class="mt-3">${camBtn('🚩 Still dirty? Report it', 'bg-white border-2 border-rose-200 text-rose-600 text-base', `submitFlag('${s.id}', this)`)}</div>
      <p class="text-xs text-center text-slate-500 mt-2">Be at the spot. If the AI sees trash, the cleaner loses the points and you get +10.</p>` : ''}`;
  } else if (s.reporter === me && s.status !== 'open' && !(s.status === 'claimed' && s.claimed_by === me)) {
    const message = s.status === 'cleaned' ? `Your reported cleanup is complete.`
        : `${esc(s.claimed_by)} is cleaning it now 🙌`;
    action = `<div class="mt-5 rounded-2xl bg-slate-100 p-4 text-sm text-slate-600 font-medium">You reported this spot. ${message}</div>`;
  } else if (s.status === 'claimed' && s.claimed_by === me) {
    action = `
      <a href="https://www.google.com/maps/dir/?api=1&destination=${s.lat},${s.lng}" target="_blank" rel="noopener"
         class="mt-5 block text-center w-full rounded-2xl bg-slate-100 font-bold py-3.5">🧭 Directions</a>
      ${s.task_before_photo ? `<div class="mt-3"><div class="text-xs font-bold text-slate-500 mb-1">TASK BEFORE PHOTO ✓</div><img src="${esc(s.task_before_photo)}" class="rounded-xl aspect-video object-cover w-full" alt="Before cleanup"></div>` : camBtn('📷 Take task BEFORE photo to start', 'bg-amber-400 text-amber-950', `startTask('${s.id}', this)`)}
      ${s.task_before_photo ? `<label data-unlock="${s.self_clean ? Math.round((s.claimed_at + SOLO_MIN_SECONDS) * 1000) : 0}" class="after-btn mt-2 block text-center w-full rounded-2xl bg-forest-500 text-white font-extrabold py-4 text-lg shadow-lg cursor-pointer active:scale-[.98] transition">
        <span class="after-label">📸 Upload after photo</span>
        <input type="file" accept="image/*" capture="environment" class="hidden" onchange="submitClean('${s.id}', this)">
      </label>` : ''}
      <p class="text-xs text-center text-slate-500 mt-2">${s.self_clean
        ? '🧹 Solo cleanup: your report photos are the BEFORE photos. Clean it, then take the after photo from the same spot. The AI compares them.'
        : 'Take the before photo at the spot, clean up, then take the after photo from the same place. The AI compares them.'}</p>
      <button onclick="releaseSpot('${s.id}', this)" class="mt-3 w-full rounded-2xl text-slate-500 font-semibold py-3 underline">Give up this task</button>`;
  } else if (s.status === 'claimed') {
    action = `<div class="mt-5 rounded-2xl bg-amber-50 p-4 text-sm text-amber-800 font-semibold">⏳ ${esc(s.claimed_by)} is cleaning this spot.</div>`;
  } else {
    const own = s.reporter === me;
    const label = own ? 'Clean your report' : 'Claim &amp; clean';
    const pts = own ? Math.round(s.points / 2) : s.points;  // cleaning your own report = solo cleanup, half points
    action = `<button onclick="claim('${s.id}')" class="mt-5 w-full rounded-2xl bg-forest-500 text-white font-extrabold py-4 text-lg shadow-lg active:scale-[.98] transition">${label} · +${pts} pts</button><p class="text-xs text-center text-slate-500 mt-2">${own
      ? 'Cleaning your own report counts as a solo cleanup: half points, the +5 report bonus goes back, max 3 a day.'
      : 'Take a before photo to start, then upload an after photo when you finish. The AI checks it and pays your points.'}</p>`
      + (own ? `<button onclick="deleteSpot('${s.id}', this)" class="mt-3 w-full rounded-2xl border-2 border-rose-200 text-rose-600 font-bold py-3 active:scale-[.98] transition">🗑️ Delete my report</button>` : '');
  }

  openSheet(`
    <div class="mx-auto w-10 h-1.5 rounded-full bg-slate-200 mb-4"></div>
    <div class="text-[11px] font-bold uppercase tracking-wide text-slate-500 mb-2">Original report · before cleanup</div>
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
    ${detection}
    <div class="mt-3 grid grid-cols-2 gap-2 text-sm">
      <div class="rounded-xl bg-slate-50 px-3 py-2">🗑️ <b>${s.bags}</b> bag${s.bags === 1 ? '' : 's'}</div>
      <div class="rounded-xl bg-slate-50 px-3 py-2">⏱️ ~<b>${s.minutes}</b> min</div>
    </div>
    ${hazards}
    <div class="text-xs text-slate-400 mt-3">Reported by ${userLink(s.reporter)} · ${timeAgo(s.created_at)}${Number.isFinite(s.location_accuracy_m) ? ` · report GPS ±${Math.round(s.location_accuracy_m)} m` : ''}${myLocation ? ` · ${fmtDist(distTo(s))}` : ''}${s.ai_source === 'demo' ? ' · demo score' : ''}</div>
    ${s.status === 'open' ? note : ''}
    ${action}`);
}

// Shared: compress photo, get GPS, POST it with extra fields.
async function sendPhoto(url, input, fields, loadingText) {
  const file = input.files[0];
  input.value = '';
  if (!file) return null;
  let blob;
  try { blob = await compress(file); } catch (e) { toast(e.message, 'err'); return null; }
  const img = URL.createObjectURL(blob);
  showLoading('Getting a precise GPS fix…', img);
  const pos = await getLocationOrAsk(img, true);
  if (!pos) { hideLoading(); return null; }
  showLoading(loadingText, img);
  const fd = new FormData();
  fd.append('user', me);
  fd.append('lat', pos.lat);
  fd.append('lng', pos.lng);
  fd.append('accuracy_m', pos.acc);
  for (const [k, v] of Object.entries(fields)) fd.append(k, v);
  fd.append('photo', blob, 'photo.jpg');
  try {
    const r = await api(url, { method: 'POST', body: fd });
    hideLoading();
    await loadSpots();
    refreshPoints();
    return r;
  } catch (e) { hideLoading(); toast(e.message, 'err'); return null; }
}

async function submitFlag(id, input) {
  const r = await sendPhoto(`/api/spots/${id}/flag`, input, {}, 'AI is looking for trash…');
  if (!r) return;
  toast(`Thanks! Spot is back on the map${r.reward ? ` · +${r.reward} pts` : ''}`);
  openSpot(id);
}

// Give up a claimed task (two taps), so the spot goes back on the map for someone else.
async function releaseSpot(id, btn) {
  if (!btn.dataset.armed) {
    btn.dataset.armed = '1';
    btn.textContent = 'Tap again to give it up';
    setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = 'Give up this task'; } }, 4000);
    return;
  }
  const fd = new FormData();
  fd.append('user', me);
  try {
    await api(`/api/spots/${id}/release`, { method: 'POST', body: fd });
    toast('Task released. It’s back on the map for others.');
    await loadSpots();
    openSpot(id);
  } catch (e) { toast(e.message, 'err'); }
}

// Two taps to delete, so a slip of the thumb doesn't remove a report.
async function deleteSpot(id, btn) {
  if (!btn.dataset.armed) {
    btn.dataset.armed = '1';
    btn.textContent = 'Tap again to delete (your +5 goes back)';
    btn.classList.add('bg-rose-50');
    setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = '🗑️ Delete my report'; btn.classList.remove('bg-rose-50'); } }, 4000);
    return;
  }
  const fd = new FormData();
  fd.append('user', me);
  try {
    await api(`/api/spots/${id}/delete`, { method: 'POST', body: fd });
    closeSheet();
    toast('Report deleted.');
    await loadSpots();
    refreshPoints();
  } catch (e) { toast(e.message, 'err'); }
}

async function claim(id) {
  const fd = new FormData();
  fd.append('user', me);
  try {
    await api(`/api/spots/${id}/claim`, { method: 'POST', body: fd });
    const spot = spots.find((s) => s.id === id);
    toast(spot?.reporter === me ? 'Your report is claimed. Take a before photo to start.' : "It's yours! Head there and clean it up 💪");
    await loadSpots();
    openSpot(id);
  } catch (e) { toast(e.message, 'err'); }
}

async function startTask(id, input) {
  const file = input.files[0]; input.value = '';
  if (!file) return;
  let blob;
  try { blob = await compress(file); } catch (e) { return toast(e.message, 'err'); }
  const img = URL.createObjectURL(blob);
  showLoading('Getting a precise GPS fix…', img);
  const pos = await getLocationOrAsk(img, true);
  hideLoading();
  if (!pos) return;
  const fd = new FormData(); fd.append('user', me); fd.append('lat', pos.lat); fd.append('lng', pos.lng); fd.append('accuracy_m', pos.acc); fd.append('photo', blob, 'before.jpg');
  try {
    await api(`/api/spots/${id}/start`, { method: 'POST', body: fd });
    toast('Before photo saved. Cleanup started!'); await loadSpots(); openSpot(id);
  } catch (e) { toast(e.message, 'err'); }
}

async function submitClean(id, input) {
  const r = await sendPhoto(`/api/spots/${id}/clean`, input, {}, 'AI is checking your cleanup…');
  if (r) celebrate(r);
}

function celebrate(r) {
  const head = `<div class="text-4xl font-extrabold text-forest-700 mt-3">+${r.awarded} pts</div>
       <div class="text-slate-600 mt-2">The AI approved your cleanup.${r.capped ? ' (Daily points limit reached.)' : ''} Anyone can still flag it if the spot isn't really clean.</div>`;
  openSheet(`
    <div class="mx-auto w-10 h-1.5 rounded-full bg-slate-200 mb-6"></div>
    <div class="text-center">
      <div class="text-6xl">🎉</div>
      ${head}
      <div class="text-sm text-slate-500 mt-1">AI: ${r.verification.percent_cleaned}% cleaned · ${esc(r.verification.comment)}</div>
      <div class="grid grid-cols-2 gap-2 mt-5">
        <img src="${esc(r.spot.task_before_photo || r.spot.photos[0])}" class="rounded-2xl aspect-square object-cover w-full" alt="">
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
  $('submitSolo').disabled = pending.length === 0;
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

async function submitReport(selfClean = false) {
  if (!pending.length) return;
  showLoading('Getting a precise GPS fix…', pending[0].url);
  const pos = await getLocationOrAsk(pending[0].url, true);
  if (!pos) { hideLoading(); return; }
  showLoading('AI is checking objects and scene context…', pending[0].url);
  const fd = new FormData();
  fd.append('user', me);
  fd.append('lat', pos.lat);
  fd.append('lng', pos.lng);
  fd.append('accuracy_m', pos.acc);
  fd.append('self_clean', selfClean ? 'true' : 'false');
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
    const analysisNote = r.spot.ai_source === 'gemini'
      ? 'Gemini analyzed the photo.'
      : 'AI is unavailable; a demo score was used instead.';
    toast(selfClean
      ? `Solo cleanup started · ${r.spot.points} pts. ${analysisNote} Clean it, then upload the after photo.`
      : `Posted! ${analysisNote} Worth ${r.spot.points} pts to a volunteer (+${r.report_bonus} for you). You can also clean it yourself for half points.`);
  } catch (e) {
    hideLoading();
    if (e.assessment) openSheet(`<div class="mx-auto w-10 h-1.5 rounded-full bg-slate-200 mb-4"></div><h2 class="text-xl font-extrabold">Photo review</h2>${assessmentCard(e.assessment)}<button onclick="closeSheet()" class="mt-4 w-full rounded-2xl bg-forest-900 text-white font-bold py-3">Close</button>`);
    else toast(e.message, 'err');
  }
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
    return `<div data-user="${esc(r.user)}" onclick="openUser(this.dataset.user)" class="cursor-pointer bg-white rounded-3xl shadow-sm ${h} flex flex-col items-center justify-center px-1 ${r.user === me ? 'ring-2 ring-forest-500' : ''}">
      <div class="text-3xl">${medal[i]}</div>
      <div class="font-extrabold text-sm truncate max-w-full">${esc(r.user)}${r.donatable ? ' 💛' : ''}</div>
      <div class="text-forest-700 font-bold text-sm">${r.points} pts</div></div>`;
  }).join('');
  // Only verified volunteers get a badge; nobody else's donation status is shown.
  const badge = (r) => r.donatable
    ? '<span class="ml-1.5 text-[10px] font-extrabold uppercase tracking-wide bg-amber-100 text-amber-800 rounded-full px-2 py-0.5">💛 Verified · donations on</span>'
    : '';
  $('leaderList').innerHTML = rows.length ? rows.map((r, i) => `
    <li data-user="${esc(r.user)}" onclick="openUser(this.dataset.user)" class="flex items-center gap-3 px-4 py-3 cursor-pointer ${r.user === me ? 'bg-forest-50' : ''}">
      <div class="w-7 text-center font-extrabold text-slate-400">${i + 1}</div>
      <div class="flex-1 min-w-0">
        <div class="font-bold truncate">${esc(r.user)}${r.user === me ? ' (you)' : ''}</div>
        <div class="text-xs text-slate-500 mt-0.5">${esc(r.level)} · ${r.cleanups} cleanup${r.cleanups === 1 ? '' : 's'}${badge(r)}</div>
        ${r.goal ? `<div class="text-xs text-forest-700 font-semibold mt-1">🎯 ${r.goal.promise_kg} kg for ${money(r.goal.target_cents)} · ${money(r.goal.raised_cents)} raised</div>` : ''}
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
  $('meTrust').innerHTML = [
    p.trusted ? '🛡️ Trusted volunteer' : p.strikes ? `⚠️ ${p.strikes} "still dirty" flag${p.strikes > 1 ? 's' : ''}` : '🌱 Building trust',
  ].filter(Boolean).map((t) => `<span class="bg-white/15 rounded-full px-3 py-1">${t}</span>`).join('');
  $('meDonations').innerHTML = donationCard(p.donations, true)
    + (p.goal ? `<div class="mt-3">${goalCard(p.goal, p.user, true)}</div>` : '')
    + (p.donations.eligible ? goalForm(p.goal) : '');

  const mine = spots.filter((s) => s.reporter === me || s.cleaner === me || s.claimed_by === me);
  $('myActivity').innerHTML = mine.length ? mine.map((s) => {
    const role = s.cleaner === me ? `Cleaned · +${s.awarded} pts`
      : s.reporter === me ? 'Reported' : 'Claimed: go clean it!';
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
  renderPending();
  go('map');
  if (!me) askName(); else { refreshPoints(); checkLocationAccess(); }
  $('nameInput').addEventListener('keydown', (e) => { if (e.key === 'Enter') saveName(); });
  api('/api/status').then((s) => {
    aiStatus = s;
    if (Number.isFinite(s.max_location_accuracy_m)) PHOTO_LOCATION_ACCURACY_M = s.max_location_accuracy_m;
    $('aiBadge').textContent = s.ai === 'gemini' ? `AI online · ${s.model}` : 'AI unavailable · demo scoring';
    $('aiBadge').title = s.ai === 'gemini' ? '' : (s.last_error || 'Configure GEMINI_API_KEY to enable photo analysis.');
    $('aiBadge').onclick = () => {
      if (aiStatus?.ai !== 'gemini') toast(aiStatus?.last_error || 'Configure GEMINI_API_KEY to enable photo analysis.', 'err');
    };
  }).catch(() => {});
  await loadSpots();
  setInterval(loadSpots, 20000);
}
boot();
