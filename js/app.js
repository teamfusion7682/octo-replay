/* Octo-Replay · FRC #7682 Fusion Robotics
 *
 * Akış: Sezon -> Etkinlik -> Maç (veya takım arama) -> video + TBA verisi + AI JSON + notlar.
 * AI JSON yolu: data/{yıl}/{event_key}/{match_key}.json  (şema: data/schema-example.json)
 * Tüm zaman damgaları videonun saniyesidir.
 */
(function () {
  'use strict';

  var API_TBA = 'api/tba-proxy.php';
  var API_NOTES = 'api/notes.php';
  var DATA_ROOT = 'data';
  var STATIC_TBA_ROOT = 'data/tba';
  var FETCH_TIMEOUT_MS = 15000;
  var POLL_MS = 250;
  var LEVEL_ORDER = { qm: 1, ef: 2, qf: 3, sf: 4, f: 5 };
  var LEVEL_NAME = { qm: 'Eleme', ef: 'Sekizlik Final', qf: 'Çeyrek Final', sf: 'Yarı Final', f: 'Final' };
  var TYPE_NAME = { cycle: 'Cycle', defense: 'Savunma', downtime: 'Arıza', auto: 'Otonom' };
  var CATEGORY_NAME = { general: 'Genel', auto: 'Otonom', cycle: 'Cycle', defense: 'Savunma', reliability: 'Güvenilirlik', endgame: 'Endgame' };
  var DEFAULT_MATCH_LENGTH_S = 160;
  var EVENT_PRIORITY = { auto: 0, cycle: 1, defense: 2, downtime: 2 };

  var state = {
    year: 2026,
    events: [],
    eventKey: '',
    matches: [],
    teams: {},
    ranks: {},
    match: null,
    ai: null,
    aiStatus: 'idle', // idle | loading | ready | waiting | error
    timeline: [],
    filter: 'all',
    dataMode: '', // '' (bilinmiyor) | 'api' (PHP) | 'static' (data/tba)
    loadToken: 0,
    activeTeams: {},
    activeJump: -1
  };

  var $ = function (id) { return document.getElementById(id); };
  var els = {};

  /* ---------------- Yardımcılar ---------------- */

  function make(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function clear(node) {
    while (node.firstChild) { node.removeChild(node.firstChild); }
  }

  function num(value, fallback) {
    var n = (value === null || value === undefined || value === '' || typeof value === 'boolean') ? NaN : Number(value);
    return isFinite(n) ? n : (fallback === undefined ? null : fallback);
  }

  function fmtClock(seconds) {
    var s = Math.max(0, Math.floor(num(seconds, 0)));
    var m = Math.floor(s / 60);
    var r = s % 60;
    return m + ':' + (r < 10 ? '0' : '') + r;
  }

  function fmtSeconds(value) {
    var n = num(value);
    return n === null ? '–' : n.toFixed(1) + ' sn';
  }

  function teamNumber(key) {
    var m = /^frc(\d+)$/.exec(String(key || ''));
    return m ? Number(m[1]) : 0;
  }

  function lsGet(key) {
    try { return window.localStorage.getItem(key); } catch (e) { return null; }
  }

  function lsSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* özel pencere vb. */ }
  }

  /** Zaman aşımlı fetch + HTTP durum kontrolü + JSON ayrıştırma. */
  async function fetchJson(url, options) {
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = controller ? window.setTimeout(function () { controller.abort(); }, FETCH_TIMEOUT_MS) : null;
    var init = Object.assign({ headers: { Accept: 'application/json' }, cache: 'no-cache' }, options || {});
    if (controller) { init.signal = controller.signal; }
    var res;
    try {
      res = await fetch(url, init);
    } catch (err) {
      var timedOut = err && err.name === 'AbortError';
      var e1 = new Error(timedOut ? 'İstek zaman aşımına uğradı.' : 'Ağ hatası: sunucuya ulaşılamadı.');
      e1.status = 0;
      throw e1;
    } finally {
      if (timer) { window.clearTimeout(timer); }
    }
    var text = '';
    try { text = await res.text(); } catch (err2) { text = ''; }
    var data = null;
    var parsed = false;
    if (text) {
      try { data = JSON.parse(text); parsed = true; } catch (err3) { parsed = false; }
    }
    if (!res.ok) {
      var msg = parsed && data && typeof data.error === 'string' ? data.error : 'Sunucu hatası (HTTP ' + res.status + ').';
      var e2 = new Error(msg);
      e2.status = res.status;
      throw e2;
    }
    if (!parsed) {
      var e3 = new Error('Sunucu geçersiz JSON döndürdü.');
      e3.status = res.status;
      throw e3;
    }
    return data;
  }

  async function tbaApi(action, params) {
    var query = new URLSearchParams(Object.assign({ action: action }, params || {}));
    var payload = await fetchJson(API_TBA + '?' + query.toString());
    if (!payload || payload.ok !== true) {
      throw new Error((payload && payload.error) || 'TBA yanıtı geçersiz.');
    }
    return payload;
  }

  /** Statik yayın (GitHub Pages): tools/fetch_tba.py'nin yazdığı data/tba/ dosyaları. */
  function staticUrl(action, params) {
    var p = params || {};
    if (action === 'events') { return STATIC_TBA_ROOT + '/' + (p.year || 2026) + '/events.json'; }
    var file = { matches: 'matches', teams: 'teams', rankings: 'rankings' }[action];
    if (file && /^\d{4}[a-z0-9]{2,20}$/.test(p.event || '')) {
      return STATIC_TBA_ROOT + '/' + p.event.slice(0, 4) + '/' + p.event + '/' + file + '.json';
    }
    return '';
  }

  async function tbaStatic(action, params) {
    var url = staticUrl(action, params);
    if (!url) { throw new Error('Bu işlem statik yayında desteklenmiyor.'); }
    var payload = await fetchJson(url);
    if (!payload || payload.ok !== true) { throw new Error('Statik veri geçersiz.'); }
    return payload;
  }

  /**
   * Önce PHP API'sini dener; arka uç yoksa (GitHub Pages gibi) data/tba/ dosyalarına düşer.
   * Mod ilk başarılı yanıtta belirlenir ve sonraki çağrılarda sabit kalır.
   */
  async function tba(action, params) {
    if (state.dataMode === 'static') { return tbaStatic(action, params); }
    try {
      var live = await tbaApi(action, params);
      state.dataMode = 'api';
      return live;
    } catch (apiErr) {
      if (state.dataMode === 'api') { throw apiErr; } // API daha önce çalışmıştı: bu gerçek bir hata
      try {
        var local = await tbaStatic(action, params);
        state.dataMode = 'static';
        return local;
      } catch (staticErr) {
        var noBackend = apiErr.status === 0 || apiErr.status === 404 || apiErr.status === 405 || /geçersiz JSON/.test(apiErr.message);
        if (!noBackend) { throw apiErr; }
        throw staticErr.status === 404
          ? new Error('Veri dosyaları henüz yüklenmemiş (data/tba/). Repodaki "Veri güncelle" iş akışını çalıştırın.')
          : staticErr;
      }
    }
  }

  function setStatus(node, message, kind) {
    node.textContent = message || '';
    node.classList.toggle('is-error', kind === 'error');
    node.classList.toggle('is-ok', kind === 'ok');
  }

  /* ---------------- Marka / logo ---------------- */

  function initLogo() {
    var img = $('brand-logo');
    var fallback = 'assets/octopus-logo.svg';
    var swapped = false;
    function swap() {
      if (swapped) { return; }
      swapped = true;
      img.src = fallback;
    }
    img.addEventListener('error', swap);
    if (img.complete && img.naturalWidth === 0) { swap(); }
  }

  /* ---------------- Etkinlik / maç seçimi ---------------- */

  function levelRank(level) { return LEVEL_ORDER[level] || 9; }

  function sortMatches(list) {
    return list.slice().sort(function (a, b) {
      return levelRank(a.comp_level) - levelRank(b.comp_level) ||
        num(a.set_number, 0) - num(b.set_number, 0) ||
        num(a.match_number, 0) - num(b.match_number, 0);
    });
  }

  function matchLabel(m) {
    var name = LEVEL_NAME[m.comp_level] || m.comp_level.toUpperCase();
    if (m.comp_level === 'qm') { return name + ' ' + m.match_number; }
    return name + ' ' + m.set_number + ' · Maç ' + m.match_number;
  }

  function shortMatchLabel(m) {
    if (m.comp_level === 'qm') { return 'Q' + m.match_number; }
    return String(m.comp_level).toUpperCase() + m.set_number + 'M' + m.match_number;
  }

  function hasScore(m) {
    var r = m && m.alliances && m.alliances.red ? m.alliances.red.score : null;
    var b = m && m.alliances && m.alliances.blue ? m.alliances.blue.score : null;
    return r !== null && b !== null && r >= 0 && b >= 0;
  }

  function youtubeIdOf(m) {
    var videos = (m && m.videos) || [];
    for (var i = 0; i < videos.length; i++) {
      if (videos[i] && videos[i].type === 'youtube' && /^[\w-]{6,20}$/.test(videos[i].key || '')) {
        return videos[i].key;
      }
    }
    return '';
  }

  function dateRange(e) {
    if (!e.start_date) { return ''; }
    var s = e.start_date.slice(5).split('-').reverse().join('.');
    var f = e.end_date ? e.end_date.slice(5).split('-').reverse().join('.') : '';
    return f && f !== s ? s + '–' + f : s;
  }

  function fillEventSelect() {
    var select = els.eventSelect;
    clear(select);
    if (!state.events.length) {
      select.appendChild(new Option('Bu sezon için Türkiye etkinliği bulunamadı', ''));
      select.disabled = true;
      return;
    }
    select.appendChild(new Option('Etkinlik seçin…', ''));
    var regular = make('optgroup');
    regular.label = 'Resmî etkinlikler';
    var off = make('optgroup');
    off.label = 'Off-Season';
    state.events.forEach(function (e) {
      var label = (e.name || e.key) + (e.city ? ' · ' + e.city : '') + (dateRange(e) ? ' (' + dateRange(e) + ')' : '');
      (e.is_offseason ? off : regular).appendChild(new Option(label, e.key));
    });
    if (regular.children.length) { select.appendChild(regular); }
    if (off.children.length) { select.appendChild(off); }
    select.disabled = false;
  }

  async function loadEvents() {
    setStatus(els.pickerStatus, 'Türkiye etkinlikleri yükleniyor…');
    els.retryEvents.hidden = true;
    els.eventSelect.disabled = true;
    clear(els.eventSelect);
    els.eventSelect.appendChild(new Option('Etkinlikler yükleniyor…', ''));
    try {
      var payload = await tba('events', { year: state.year });
      state.events = Array.isArray(payload.data) ? payload.data : [];
      fillEventSelect();
      var note = payload.meta && payload.meta.stale ? 'TBA şu an yanıt vermiyor; önbellekteki veri gösteriliyor.' : '';
      var staticInfo = '';
      if (state.dataMode === 'static' && payload.meta && payload.meta.fetched_at) {
        staticInfo = 'Veriler ' + new Date(payload.meta.fetched_at * 1000).toLocaleDateString('tr-TR', { dateStyle: 'long' }) + ' tarihinde güncellendi.';
      }
      setStatus(els.pickerStatus, state.events.length ? (note || staticInfo) : 'Bu sezon için Türkiye etkinliği henüz listelenmemiş.', note ? 'error' : '');
    } catch (err) {
      state.events = [];
      clear(els.eventSelect);
      els.eventSelect.appendChild(new Option('Etkinlikler alınamadı', ''));
      setStatus(els.pickerStatus, 'Etkinlikler yüklenemedi: ' + err.message, 'error');
      els.retryEvents.hidden = false;
    }
  }

  function resetMatchControls(message) {
    clear(els.matchSelect);
    els.matchSelect.appendChild(new Option(message, ''));
    els.matchSelect.disabled = true;
    els.teamInput.disabled = true;
    els.teamSubmit.disabled = true;
    els.teamResults.hidden = true;
  }

  async function onEventChange() {
    var key = els.eventSelect.value;
    state.eventKey = key;
    state.matches = [];
    state.teams = {};
    state.ranks = {};
    resetWorkspace();
    if (!key) {
      resetMatchControls('Önce etkinlik seçin');
      return;
    }
    resetMatchControls('Maçlar yükleniyor…');
    setStatus(els.pickerStatus, 'Maçlar, takımlar ve sıralamalar yükleniyor…');
    var token = ++state.loadToken;

    var results = await Promise.allSettled([
      tba('matches', { event: key }),
      tba('teams', { event: key }),
      tba('rankings', { event: key })
    ]);
    if (token !== state.loadToken) { return; }

    if (results[0].status !== 'fulfilled') {
      resetMatchControls('Maçlar alınamadı');
      setStatus(els.pickerStatus, 'Maçlar yüklenemedi: ' + results[0].reason.message, 'error');
      return;
    }
    state.matches = sortMatches(Array.isArray(results[0].value.data) ? results[0].value.data : []);

    if (results[1].status === 'fulfilled' && Array.isArray(results[1].value.data)) {
      results[1].value.data.forEach(function (t) { state.teams[t.team_number] = t; });
    }
    if (results[2].status === 'fulfilled' && results[2].value.data && Array.isArray(results[2].value.data.rankings)) {
      results[2].value.data.rankings.forEach(function (r) { state.ranks[r.team_number] = r; });
    }

    fillMatchSelect();
    var partial = results[1].status !== 'fulfilled' || results[2].status !== 'fulfilled';
    if (!state.matches.length) {
      setStatus(els.pickerStatus, 'Bu etkinlik için henüz maç yayımlanmamış.', '');
    } else {
      setStatus(els.pickerStatus, partial ? 'Takım adları veya sıralamalar alınamadı; maç verisi gösterilecek.' : '', partial ? 'error' : '');
    }
    els.teamInput.disabled = !state.matches.length;
    els.teamSubmit.disabled = !state.matches.length;
    maybeRestoreFromHash();
  }

  function fillMatchSelect() {
    var select = els.matchSelect;
    clear(select);
    if (!state.matches.length) {
      select.appendChild(new Option('Maç bulunamadı', ''));
      select.disabled = true;
      return;
    }
    select.appendChild(new Option('Maç seçin…', ''));
    var groups = {};
    state.matches.forEach(function (m) {
      var g = groups[m.comp_level];
      if (!g) {
        g = make('optgroup');
        g.label = LEVEL_NAME[m.comp_level] || m.comp_level.toUpperCase();
        groups[m.comp_level] = g;
        select.appendChild(g);
      }
      var tag = (youtubeIdOf(m) ? '▶ ' : '') + matchLabel(m) + (hasScore(m) ? ' · ' + m.alliances.red.score + '–' + m.alliances.blue.score : '');
      g.appendChild(new Option(tag, m.key));
    });
    select.disabled = false;
  }

  function findTeamMatches(number) {
    var key = 'frc' + number;
    return state.matches.filter(function (m) {
      return m.alliances.red.team_keys.indexOf(key) >= 0 || m.alliances.blue.team_keys.indexOf(key) >= 0;
    });
  }

  function onTeamSearch(ev) {
    ev.preventDefault();
    var raw = els.teamInput.value.trim();
    clear(els.teamResultsList);
    if (!/^\d{1,5}$/.test(raw) || Number(raw) < 1) {
      els.teamResults.hidden = true;
      setStatus(els.pickerStatus, 'Geçerli bir takım numarası girin (ör. 7682).', 'error');
      return;
    }
    var number = Number(raw);
    var list = findTeamMatches(number);
    els.teamResults.hidden = false;
    var team = state.teams[number];
    els.teamResultsTitle.textContent = 'Takım ' + number + (team && team.nickname ? ' · ' + team.nickname : '') + ' — ' + list.length + ' maç';
    if (!list.length) {
      setStatus(els.pickerStatus, number + ' numaralı takım bu etkinlikte maç yapmamış görünüyor.', 'error');
      return;
    }
    setStatus(els.pickerStatus, '');
    list.forEach(function (m) {
      var key = 'frc' + number;
      var side = m.alliances.red.team_keys.indexOf(key) >= 0 ? 'red' : 'blue';
      var outcome = !hasScore(m) ? 'Oynanmadı' : (m.winning_alliance === '' ? 'Berabere' : (m.winning_alliance === side ? 'Galibiyet' : 'Mağlubiyet'));
      var button = make('button', 'result-item');
      button.type = 'button';
      button.appendChild(make('strong', '', matchLabel(m)));
      button.appendChild(make('span', '', (side === 'red' ? 'Kırmızı' : 'Mavi') + ' · ' + outcome + (hasScore(m) ? ' · ' + m.alliances.red.score + '–' + m.alliances.blue.score : '') + (youtubeIdOf(m) ? ' · Video var' : ' · Video yok')));
      button.addEventListener('click', function () {
        els.matchSelect.value = m.key;
        selectMatch(m.key);
      });
      var li = make('li');
      li.appendChild(button);
      els.teamResultsList.appendChild(li);
    });
  }

  function switchTab(which) {
    var isMatch = which === 'match';
    els.tabMatch.setAttribute('aria-selected', String(isMatch));
    els.tabTeam.setAttribute('aria-selected', String(!isMatch));
    els.tabMatch.tabIndex = isMatch ? 0 : -1;
    els.tabTeam.tabIndex = isMatch ? -1 : 0;
    els.paneMatch.hidden = !isMatch;
    els.paneTeam.hidden = isMatch;
    if (!isMatch && !els.teamInput.disabled) { els.teamInput.focus(); }
  }

  /* ---------------- Çalışma alanı ---------------- */

  function resetWorkspace() {
    state.match = null;
    state.ai = null;
    state.aiStatus = 'idle';
    state.timeline = [];
    state.activeTeams = {};
    state.activeJump = -1;
    stopPolling();
    els.workspace.hidden = true;
    els.welcome.hidden = false;
    if (player && typeof player.stopVideo === 'function') {
      try { player.stopVideo(); } catch (e) { /* oynatıcı hazır değil */ }
    }
    writeHash();
  }

  function writeHash() {
    var parts = [];
    if (state.eventKey) { parts.push('event=' + encodeURIComponent(state.eventKey)); }
    if (state.match) { parts.push('match=' + encodeURIComponent(state.match.key)); }
    var hash = parts.length ? '#' + parts.join('&') : '';
    if (window.location.hash !== hash) {
      try { window.history.replaceState(null, '', window.location.pathname + window.location.search + hash); } catch (e) { /* yok say */ }
    }
  }

  function parseHash() {
    var out = {};
    window.location.hash.replace(/^#/, '').split('&').forEach(function (pair) {
      var i = pair.indexOf('=');
      if (i > 0) {
        try { out[pair.slice(0, i)] = decodeURIComponent(pair.slice(i + 1)); } catch (e) { /* bozuk kodlama */ }
      }
    });
    return out;
  }

  var pendingHash = null;

  function maybeRestoreFromHash() {
    if (!pendingHash) { return; }
    var wanted = pendingHash;
    pendingHash = null;
    if (wanted.match && state.matches.some(function (m) { return m.key === wanted.match; })) {
      els.matchSelect.value = wanted.match;
      selectMatch(wanted.match);
    }
  }

  async function restoreFromHash() {
    var h = parseHash();
    if (!h.event || !/^\d{4}[a-z0-9]{2,20}$/.test(h.event)) { return; }
    var exists = state.events.some(function (e) { return e.key === h.event; });
    if (!exists) { return; }
    pendingHash = h;
    els.eventSelect.value = h.event;
    await onEventChange();
  }

  function selectMatch(key) {
    var match = state.matches.filter(function (m) { return m.key === key; })[0];
    if (!match) { return; }
    openMatch(match);
  }

  async function openMatch(match) {
    var token = ++state.loadToken;
    state.match = match;
    state.ai = null;
    state.aiStatus = 'loading';
    state.timeline = [];
    state.filter = 'all';
    state.activeTeams = {};
    state.activeJump = -1;

    els.welcome.hidden = true;
    els.workspace.hidden = false;
    writeHash();
    renderMatchHeader();
    renderCards();
    renderBadge();
    renderJumps();
    fillNoteTargets();

    // Video önce TBA'dan; AI JSON gelince yedek olarak ondan.
    loadVideo(youtubeIdOf(match));
    loadNotes();

    try {
      var url = DATA_ROOT + '/' + state.year + '/' + encodeURIComponent(match.event_key) + '/' + encodeURIComponent(match.key) + '.json';
      var ai = await fetchJson(url);
      if (token !== state.loadToken) { return; }
      var normalized = normalizeAi(ai);
      if (!normalized) { throw new Error('AI JSON şeması tanınmadı.'); }
      state.ai = normalized;
      state.aiStatus = 'ready';
      state.timeline = buildTimeline(normalized);
      if (!youtubeIdOf(match) && normalized.video.youtube_id) { loadVideo(normalized.video.youtube_id); }
    } catch (err) {
      if (token !== state.loadToken) { return; }
      state.ai = null;
      state.timeline = [];
      // 404 = henüz analiz yüklenmemiş; diğerleri gerçek hata
      state.aiStatus = err.status === 404 ? 'waiting' : (err.status === 0 ? 'waiting' : 'error');
      if (state.aiStatus === 'error') { console.warn('[octo-replay] AI JSON okunamadı:', err.message); }
    }
    renderBadge();
    renderCards();
    renderJumps();
    syncToTime(currentTime());
  }

  /** AI JSON'u doğrular, eksik alanları güvenli varsayılanlarla doldurur. */
  function normalizeAi(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw) || typeof raw.teams !== 'object' || raw.teams === null) { return null; }
    var out = {
      video: { youtube_id: '', duration_s: null },
      periods: raw.periods && typeof raw.periods === 'object' ? raw.periods : {},
      teams: {},
      timeline: Array.isArray(raw.timeline) ? raw.timeline : [],
      generated_at: typeof raw.generated_at === 'string' ? raw.generated_at : '',
      analyzer_version: typeof raw.analyzer_version === 'string' ? raw.analyzer_version : ''
    };
    if (raw.video && typeof raw.video === 'object') {
      out.video.youtube_id = /^[\w-]{6,20}$/.test(raw.video.youtube_id || '') ? raw.video.youtube_id : '';
      out.video.duration_s = num(raw.video.duration_s);
    }
    Object.keys(raw.teams).forEach(function (key) {
      var t = raw.teams[key];
      if (!/^frc\d+$/.test(key) || !t || typeof t !== 'object') { return; }
      out.teams[key] = {
        alliance: t.alliance === 'blue' ? 'blue' : (t.alliance === 'red' ? 'red' : ''),
        track_coverage: num(t.track_coverage),
        auto: t.auto && typeof t.auto === 'object' ? { moved: t.auto.moved === true, active_s: num(t.auto.active_s) } : null,
        cycles: t.cycles && typeof t.cycles === 'object' ? { count: num(t.cycles.count, 0), avg_s: num(t.cycles.avg_s), best_s: num(t.cycles.best_s), times: Array.isArray(t.cycles.times) ? t.cycles.times : [] } : null,
        defense: t.defense && typeof t.defense === 'object' ? { total_s: num(t.defense.total_s, 0), events: Array.isArray(t.defense.events) ? t.defense.events : [] } : null,
        downtime: t.downtime && typeof t.downtime === 'object' ? { total_s: num(t.downtime.total_s, 0), events: Array.isArray(t.downtime.events) ? t.downtime.events : [] } : null
      };
    });
    return out;
  }

  /** Zaman çizelgesi: ai.timeline varsa onu, yoksa takım olaylarından türetileni kullanır. */
  function buildTimeline(ai) {
    var items = [];
    function push(t, end, type, team, label) {
      var start = num(t);
      if (start === null || start < 0 || !TYPE_NAME[type] || !/^frc\d+$/.test(team || '')) { return; }
      var stop = num(end);
      items.push({ t: start, end: stop !== null && stop >= start ? stop : start + 3, type: type, team: team, label: String(label || TYPE_NAME[type]).slice(0, 80) });
    }
    if (ai.timeline.length) {
      ai.timeline.forEach(function (e) {
        if (e && typeof e === 'object') { push(e.t, e.end, e.type, e.team, e.label); }
      });
    } else {
      Object.keys(ai.teams).forEach(function (team) {
        var d = ai.teams[team];
        if (d.cycles) { d.cycles.times.forEach(function (c, i) { if (c) { push(c.start_s, c.end_s, 'cycle', team, 'Cycle ' + (i + 1) + (num(c.duration_s) !== null ? ' · ' + c.duration_s.toFixed(1) + ' sn' : '')); } }); }
        if (d.defense) { d.defense.events.forEach(function (c) { if (c) { push(c.start_s, c.end_s, 'defense', team, 'Savunma'); } }); }
        if (d.downtime) { d.downtime.events.forEach(function (c) { if (c) { push(c.start_s, c.end_s, 'downtime', team, 'Hareketsiz'); } }); }
        if (d.auto && d.auto.moved && ai.periods && num(ai.periods.auto_start_s) !== null) {
          push(ai.periods.auto_start_s, ai.periods.auto_end_s, 'auto', team, 'Otonom');
        }
      });
    }
    items.sort(function (a, b) { return a.t - b.t || a.team.localeCompare(b.team); });
    return items;
  }

  /* ---------------- Render: başlık, skor, rozet ---------------- */

  function renderMatchHeader() {
    var m = state.match;
    var ev = state.events.filter(function (e) { return e.key === m.event_key; })[0];
    els.matchEvent.textContent = ev ? ev.name : m.event_key;
    els.matchTitle.textContent = matchLabel(m);
    var epoch = m.actual_time || m.time || m.predicted_time;
    els.matchTime.textContent = epoch ? new Date(epoch * 1000).toLocaleString('tr-TR', { dateStyle: 'medium', timeStyle: 'short' }) : '';
    var played = hasScore(m);
    els.scoreRed.textContent = played ? m.alliances.red.score : '–';
    els.scoreBlue.textContent = played ? m.alliances.blue.score : '–';
    els.matchResult.textContent = !played ? 'Bu maç henüz oynanmadı veya skor girilmedi.' : (m.winning_alliance === 'red' ? 'Kazanan: Kırmızı İttifak' : (m.winning_alliance === 'blue' ? 'Kazanan: Mavi İttifak' : 'Maç berabere bitti.'));
  }

  function renderBadge() {
    var map = {
      loading: ['AI analizi yükleniyor…', 'is-loading'],
      ready: ['AI analizi hazır', 'is-ready'],
      waiting: ['AI Analizi Bekleniyor', 'is-waiting'],
      error: ['AI verisi okunamadı', 'is-error'],
      idle: ['', '']
    };
    var spec = map[state.aiStatus] || map.idle;
    [els.badge, els.badgeLeft].forEach(function (b) {
      b.textContent = spec[0];
      b.className = 'badge ' + spec[1];
      b.hidden = !spec[0];
    });
  }

  /* ---------------- Render: takım kartları ---------------- */

  function allianceKeys(side) {
    var a = state.match && state.match.alliances ? state.match.alliances[side] : null;
    return a && Array.isArray(a.team_keys) ? a.team_keys : [];
  }

  function statItem(label, value, small) {
    var wrap = make('div', 'stat');
    wrap.appendChild(make('dt', '', label));
    var dd = make('dd', '', value);
    if (small) { dd.appendChild(document.createTextNode(' ')); dd.appendChild(make('small', '', small)); }
    wrap.appendChild(dd);
    return wrap;
  }

  function bar(kind, fraction) {
    var b = make('div', 'bar bar-' + kind);
    var fill = make('i');
    fill.style.width = Math.round(Math.max(0, Math.min(1, fraction)) * 100) + '%';
    b.appendChild(fill);
    return b;
  }

  function matchLengthS() {
    var p = state.ai && state.ai.periods ? state.ai.periods : {};
    var start = num(p.auto_start_s);
    var end = num(p.teleop_end_s);
    if (start !== null && end !== null && end > start) { return end - start; }
    return DEFAULT_MATCH_LENGTH_S;
  }

  function buildCard(key, side) {
    var number = teamNumber(key);
    var info = state.teams[number];
    var rank = state.ranks[number];
    var card = make('article', 'team-card');
    card.dataset.team = key;

    var top = make('div', 'card-top');
    var id = make('div', 'card-team');
    id.appendChild(make('span', 'card-number', number || key));
    id.appendChild(make('span', 'card-name', info && info.nickname ? info.nickname : 'Takım ' + number));
    top.appendChild(id);
    if (rank && rank.rank) { top.appendChild(make('span', 'card-rank', '#' + rank.rank + ' · ' + rank.wins + '-' + rank.losses + (rank.ties ? '-' + rank.ties : ''))); }
    card.appendChild(top);

    var stateTag = make('div', 'card-state', '');
    stateTag.dataset.role = 'state';
    card.appendChild(stateTag);

    var ai = state.ai && state.ai.teams ? state.ai.teams[key] : null;
    if (ai) {
      var stats = make('dl', 'stats');
      stats.appendChild(statItem('Cycle', ai.cycles ? String(ai.cycles.count) : '–', 'adet'));
      stats.appendChild(statItem('Ort. cycle', ai.cycles ? fmtSeconds(ai.cycles.avg_s) : '–'));
      stats.appendChild(statItem('En iyi', ai.cycles ? fmtSeconds(ai.cycles.best_s) : '–'));
      stats.appendChild(statItem('Otonom', ai.auto ? (ai.auto.moved ? 'Hareketli' : 'Hareketsiz') : '–'));
      stats.appendChild(statItem('Savunma', ai.defense ? fmtSeconds(ai.defense.total_s) : '–'));
      stats.appendChild(statItem('Arıza', ai.downtime ? fmtSeconds(ai.downtime.total_s) : '–'));
      card.appendChild(stats);
      var bars = make('div', 'card-bars');
      var len = matchLengthS();
      if (ai.defense) { bars.appendChild(bar('defense', ai.defense.total_s / len)); }
      if (ai.downtime) { bars.appendChild(bar('downtime', ai.downtime.total_s / len)); }
      card.appendChild(bars);
      if (ai.track_coverage !== null && ai.track_coverage < 0.5) {
        card.appendChild(make('p', 'pending-note', 'Düşük takip güvenilirliği (%' + Math.round(ai.track_coverage * 100) + ')'));
      }
    } else if (state.aiStatus === 'loading') {
      card.appendChild(make('p', 'muted pending-note', 'AI verisi yükleniyor…'));
    } else if (state.aiStatus === 'error') {
      card.appendChild(make('p', 'pending-note', 'AI verisi okunamadı'));
    } else if (state.aiStatus === 'ready') {
      card.appendChild(make('p', 'pending-note', 'Bu takım AI analizinde bulunamadı'));
    } else {
      card.appendChild(make('p', 'pending-note', 'AI Analizi Bekleniyor'));
    }
    return card;
  }

  function renderCards() {
    clear(els.cardsRed);
    clear(els.cardsBlue);
    if (!state.match) { return; }
    allianceKeys('red').forEach(function (k) { els.cardsRed.appendChild(buildCard(k, 'red')); });
    allianceKeys('blue').forEach(function (k) { els.cardsBlue.appendChild(buildCard(k, 'blue')); });
    if (!allianceKeys('red').length && !allianceKeys('blue').length) {
      els.cardsRed.appendChild(make('p', 'empty', 'Bu maç için takım bilgisi yok.'));
    }
  }

  /* ---------------- Render: zaman damgası butonları ---------------- */

  function renderJumps() {
    clear(els.jumpFilters);
    clear(els.jumpList);
    els.jumpEmpty.hidden = true;
    if (state.aiStatus !== 'ready') {
      els.jumpEmpty.hidden = false;
      els.jumpEmpty.textContent = state.aiStatus === 'loading' ? 'AI analizi yükleniyor…' : (state.aiStatus === 'error' ? 'AI verisi okunamadı; yalnızca TBA verileri gösteriliyor.' : 'AI Analizi Bekleniyor — bu maç için analiz dosyası henüz yüklenmedi. TBA verileri ve video aşağıda/yanda kullanılabilir.');
      return;
    }
    var types = ['all'];
    state.timeline.forEach(function (e) { if (types.indexOf(e.type) < 0) { types.push(e.type); } });
    types.forEach(function (type) {
      var chip = make('button', 'chip', type === 'all' ? 'Tümü' : TYPE_NAME[type]);
      chip.type = 'button';
      chip.setAttribute('aria-pressed', String(state.filter === type));
      chip.addEventListener('click', function () { state.filter = type; renderJumps(); });
      els.jumpFilters.appendChild(chip);
    });

    var shown = 0;
    state.timeline.forEach(function (e, index) {
      if (state.filter !== 'all' && e.type !== state.filter) { return; }
      shown++;
      var btn = make('button', 'jump-btn');
      btn.type = 'button';
      btn.dataset.type = e.type;
      btn.dataset.index = String(index);
      btn.appendChild(make('span', 'jump-time', fmtClock(e.t)));
      btn.appendChild(make('span', 'jump-label', e.label));
      btn.appendChild(make('span', 'jump-team', 'Takım ' + teamNumber(e.team)));
      btn.addEventListener('click', function () { seekTo(e.t); });
      els.jumpList.appendChild(btn);
    });
    if (!shown) {
      els.jumpEmpty.hidden = false;
      els.jumpEmpty.textContent = 'Bu filtre için zaman damgası yok.';
    }
    state.activeJump = -1;
    highlightJump(currentTime());
  }

  function highlightJump(t) {
    var best = -1;
    state.timeline.forEach(function (e, i) { if (e.t <= t + 0.25) { best = i; } });
    if (best === state.activeJump) { return; }
    state.activeJump = best;
    Array.prototype.forEach.call(els.jumpList.children, function (btn) {
      btn.classList.toggle('is-active', Number(btn.dataset.index) === best);
    });
  }

  /* ---------------- Video (YouTube IFrame API) ---------------- */

  var player = null;
  var playerReady = false;
  var pendingVideoId = '';
  var currentVideoId = '';
  var apiPromise = null;
  var pollTimer = null;

  function loadYouTubeApi() {
    if (apiPromise) { return apiPromise; }
    apiPromise = new Promise(function (resolve, reject) {
      if (window.YT && window.YT.Player) { resolve(window.YT); return; }
      var previous = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = function () {
        if (typeof previous === 'function') { previous(); }
        resolve(window.YT);
      };
      var tag = document.createElement('script');
      tag.src = 'https://www.youtube.com/iframe_api';
      tag.async = true;
      tag.onerror = function () { apiPromise = null; reject(new Error('YouTube API yüklenemedi.')); };
      document.head.appendChild(tag);
      window.setTimeout(function () { if (!(window.YT && window.YT.Player)) { apiPromise = null; reject(new Error('YouTube API zaman aşımı.')); } }, 12000);
    });
    return apiPromise;
  }

  function showOverlay(title, text, videoId) {
    els.overlay.hidden = false;
    els.overlayTitle.textContent = title;
    els.overlayText.textContent = text;
    if (videoId) {
      els.overlayLink.href = 'https://www.youtube.com/watch?v=' + encodeURIComponent(videoId);
      els.overlayLink.hidden = false;
    } else {
      els.overlayLink.hidden = true;
    }
  }

  function hideOverlay() { els.overlay.hidden = true; }

  function stopPolling() {
    if (pollTimer) { window.clearInterval(pollTimer); pollTimer = null; }
  }

  function startPolling() {
    if (pollTimer) { return; }
    pollTimer = window.setInterval(function () { syncToTime(currentTime()); }, POLL_MS);
  }

  function currentTime() {
    if (player && playerReady && typeof player.getCurrentTime === 'function') {
      try { return num(player.getCurrentTime(), 0); } catch (e) { return 0; }
    }
    return 0;
  }

  function seekTo(seconds) {
    if (!(player && playerReady && typeof player.seekTo === 'function')) { return; }
    try {
      player.seekTo(Math.max(0, seconds - 1), true); // olaydan 1 sn önce başla
      player.playVideo();
      syncToTime(Math.max(0, seconds - 1));
    } catch (e) { console.warn('[octo-replay] seek başarısız:', e); }
  }

  async function loadVideo(videoId) {
    if (!videoId) {
      currentVideoId = '';
      stopPolling();
      if (player && playerReady) { try { player.stopVideo(); } catch (e) { /* yok say */ } }
      showOverlay('Bu maç için video yok', 'The Blue Alliance bu maça YouTube videosu eklememiş. TBA verileri ve scouting notları yine de kullanılabilir.', '');
      return;
    }
    if (videoId === currentVideoId && els.overlay.hidden) { return; }
    currentVideoId = videoId;
    pendingVideoId = videoId;
    showOverlay('Video yükleniyor…', '', '');
    try {
      var YT = await loadYouTubeApi();
      if (pendingVideoId !== videoId) { return; }
      if (!player) {
        player = new YT.Player('yt-player', {
          videoId: videoId,
          width: '100%',
          height: '100%',
          playerVars: { rel: 0, modestbranding: 1, playsinline: 1, enablejsapi: 1, origin: window.location.origin },
          events: {
            onReady: function () {
              playerReady = true;
              if (pendingVideoId && pendingVideoId !== videoId) {
                try { player.loadVideoById(pendingVideoId); } catch (e) { /* yok say */ }
              }
              hideOverlay();
              startPolling();
            },
            onStateChange: function (ev) {
              if (ev && ev.data === 1) { startPolling(); }
              syncToTime(currentTime());
            },
            onError: function () {
              showOverlay('Video oynatılamadı', 'Bu video gömülü oynatmaya izin vermiyor veya kaldırılmış olabilir.', currentVideoId);
            }
          }
        });
      } else if (playerReady) {
        player.loadVideoById(videoId);
        hideOverlay();
        startPolling();
      } else {
        pendingVideoId = videoId; // onReady sonrası mevcut videoId ile açılır
      }
    } catch (err) {
      showOverlay('Video yüklenemedi', err.message, videoId);
    }
  }

  /* ---------------- Senkronizasyon ---------------- */

  function syncToTime(t) {
    if (!state.match) { return; }
    var active = {};
    var activeEvent = {};
    if (state.aiStatus === 'ready') {
      state.timeline.forEach(function (e) {
        if (t >= e.t && t <= e.end) {
          active[e.team] = true;
          // Örtüşen olaylarda önce öncelik (savunma/arıza > cycle > otonom), eşitse en son başlayan görünür
          var current = activeEvent[e.team];
          if (!current || EVENT_PRIORITY[e.type] > EVENT_PRIORITY[current.type] ||
              (EVENT_PRIORITY[e.type] === EVENT_PRIORITY[current.type] && current.t <= e.t)) {
            activeEvent[e.team] = e;
          }
        }
      });
      highlightJump(t);
    }
    Array.prototype.forEach.call(document.querySelectorAll('.team-card'), function (card) {
      var key = card.dataset.team;
      var isActive = active[key] === true;
      card.classList.toggle('is-active', isActive);
      var tag = card.querySelector('[data-role="state"]');
      if (tag) {
        var e = activeEvent[key];
        tag.textContent = e ? e.label : '';
        tag.dataset.type = e ? e.type : '';
      }
    });
  }

  /* ---------------- Scouting notları ---------------- */

  function fillNoteTargets() {
    var select = els.noteTarget;
    clear(select);
    select.appendChild(new Option('Genel (maç notu)', ''));
    allianceKeys('red').concat(allianceKeys('blue')).forEach(function (k) {
      var n = teamNumber(k);
      select.appendChild(new Option('Takım ' + n, String(n)));
    });
  }

  function renderNotes(list) {
    clear(els.notesList);
    els.notesCount.textContent = list.length ? '(' + list.length + ')' : '';
    list.forEach(function (n) {
      var item = make('li', 'note-item');
      var meta = make('div', 'note-meta');
      meta.appendChild(make('strong', '', (n.author_name ? n.author_name + ' · ' : '') + 'Takım ' + n.author_team));
      meta.appendChild(make('span', 'tag', CATEGORY_NAME[n.category] || n.category));
      meta.appendChild(make('span', '', n.team_number ? '→ ' + n.team_number : '→ Genel'));
      if (n.rating) { meta.appendChild(make('span', '', '★'.repeat(n.rating))); }
      var when = new Date(n.created_at);
      if (!isNaN(when.getTime())) { meta.appendChild(make('span', '', when.toLocaleString('tr-TR', { dateStyle: 'short', timeStyle: 'short' }))); }
      item.appendChild(meta);
      item.appendChild(make('p', 'note-text', n.note));
      els.notesList.appendChild(item);
    });
  }

  async function loadNotes() {
    if (!state.match) { return; }
    var key = state.match.key;
    if (state.dataMode === 'static') {
      // Statik yayında (PHP/MySQL yok) not gönderilemez ve okunamaz.
      els.noteForm.hidden = true;
      els.notesRefresh.hidden = true;
      els.notesCount.textContent = '';
      clear(els.notesList);
      els.notesState.textContent = 'Scouting notları bu statik yayında kapalı; PHP ve MySQL destekli sunucuda çalışır.';
      return;
    }
    els.noteForm.hidden = false;
    els.notesRefresh.hidden = false;
    els.notesState.textContent = 'Notlar yükleniyor…';
    clear(els.notesList);
    els.notesCount.textContent = '';
    try {
      var payload = await fetchJson(API_NOTES + '?match_key=' + encodeURIComponent(key));
      if (!state.match || state.match.key !== key) { return; }
      if (!payload || payload.ok !== true || !Array.isArray(payload.notes)) { throw new Error('Not yanıtı geçersiz.'); }
      renderNotes(payload.notes);
      els.notesState.textContent = payload.notes.length ? '' : 'Bu maç için henüz not yok. İlk notu siz ekleyin!';
    } catch (err) {
      if (!state.match || state.match.key !== key) { return; }
      els.notesState.textContent = 'Notlar yüklenemedi: ' + err.message;
    }
  }

  async function onNoteSubmit(ev) {
    ev.preventDefault();
    if (!state.match) { return; }
    var authorTeam = els.noteAuthorTeam.value.trim();
    var text = els.noteText.value.trim();
    if (!/^\d{1,5}$/.test(authorTeam) || Number(authorTeam) < 1) {
      setStatus(els.noteStatus, 'Geçerli bir takım numaranızı girin.', 'error');
      els.noteAuthorTeam.focus();
      return;
    }
    if (text.length < 3) {
      setStatus(els.noteStatus, 'Not en az 3 karakter olmalı.', 'error');
      els.noteText.focus();
      return;
    }
    els.noteSubmit.disabled = true;
    setStatus(els.noteStatus, 'Gönderiliyor…');
    try {
      var body = {
        match_key: state.match.key,
        team_number: els.noteTarget.value ? Number(els.noteTarget.value) : null,
        author_team: Number(authorTeam),
        author_name: els.noteAuthorName.value.trim(),
        category: els.noteCategory.value,
        rating: els.noteRating.value ? Number(els.noteRating.value) : null,
        note: text,
        website: els.noteWebsite.value
      };
      var payload = await fetchJson(API_NOTES, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-Requested-With': 'OctoReplay' },
        body: JSON.stringify(body)
      });
      if (!payload || payload.ok !== true) { throw new Error((payload && payload.error) || 'Not kaydedilemedi.'); }
      lsSet('octo.authorTeam', authorTeam);
      lsSet('octo.authorName', els.noteAuthorName.value.trim());
      els.noteText.value = '';
      els.noteRating.value = '';
      updateCounter();
      setStatus(els.noteStatus, 'Not eklendi. Teşekkürler!', 'ok');
      await loadNotes();
    } catch (err) {
      setStatus(els.noteStatus, err.message, 'error');
    } finally {
      els.noteSubmit.disabled = false;
    }
  }

  function updateCounter() {
    els.noteCounter.textContent = els.noteText.value.length + ' / ' + (els.noteText.maxLength > 0 ? els.noteText.maxLength : 1000);
  }

  /* ---------------- Karşılama ekranı ---------------- */

  function isLanding() { return document.body.classList.contains('is-landing'); }

  function enterApp(skipFocus) {
    document.body.classList.remove('is-landing');
    window.scrollTo(0, 0);
    if (!skipFocus && els.eventSelect && !els.eventSelect.disabled) {
      try { els.eventSelect.focus({ preventScroll: true }); } catch (e) { /* yok say */ }
    }
  }

  function showLanding() {
    document.body.classList.add('is-landing');
    window.scrollTo(0, 0);
    try { els.startBtn.focus({ preventScroll: true }); } catch (e) { /* yok say */ }
  }

  /* ---------------- Başlatma ---------------- */

  function bind() {
    var ids = {
      seasonSelect: 'season-select', eventSelect: 'event-select', matchSelect: 'match-select',
      teamForm: 'team-form', teamInput: 'team-input', teamSubmit: 'team-submit',
      tabMatch: 'tab-match', tabTeam: 'tab-team', paneMatch: 'pane-match', paneTeam: 'pane-team',
      landing: 'landing', startBtn: 'start-btn', homeBtn: 'home-btn', homeLink: 'home-link',
      pickerStatus: 'picker-status', retryEvents: 'retry-events',
      teamResults: 'team-results', teamResultsTitle: 'team-results-title', teamResultsList: 'team-results-list',
      welcome: 'welcome', workspace: 'workspace',
      overlay: 'video-overlay', overlayTitle: 'video-overlay-title', overlayText: 'video-overlay-text', overlayLink: 'video-overlay-link',
      badge: 'ai-badge', badgeLeft: 'ai-badge-left', jumpFilters: 'jump-filters', jumpList: 'jump-list', jumpEmpty: 'jump-empty',
      matchEvent: 'match-event', matchTitle: 'match-title', matchTime: 'match-time',
      scoreRed: 'score-red', scoreBlue: 'score-blue', matchResult: 'match-result',
      cardsRed: 'cards-red', cardsBlue: 'cards-blue',
      notesCount: 'notes-count', notesRefresh: 'notes-refresh', notesState: 'notes-state', notesList: 'notes-list',
      noteForm: 'note-form', noteAuthorTeam: 'note-author-team', noteAuthorName: 'note-author-name',
      noteTarget: 'note-target', noteCategory: 'note-category', noteRating: 'note-rating',
      noteText: 'note-text', noteWebsite: 'note-website', noteCounter: 'note-counter',
      noteSubmit: 'note-submit', noteStatus: 'note-status'
    };
    var missing = [];
    Object.keys(ids).forEach(function (name) {
      var node = $(ids[name]);
      if (!node) { missing.push(ids[name]); }
      els[name] = node;
    });
    if (missing.length) { throw new Error('Eksik arayüz öğeleri: ' + missing.join(', ')); }
  }

  function init() {
    try {
      bind();
    } catch (err) {
      console.error('[octo-replay]', err);
      return;
    }
    initLogo();

    if (window.location.protocol === 'file:') {
      var warning = $('file-warning');
      if (warning) { warning.hidden = false; }
    }

    els.startBtn.addEventListener('click', function () { enterApp(false); });
    els.homeBtn.addEventListener('click', showLanding);
    els.homeLink.addEventListener('click', function (ev) { ev.preventDefault(); showLanding(); });
    document.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && isLanding() && (document.activeElement === document.body || document.activeElement === null)) {
        enterApp(false);
      }
    });
    if (parseHash().event) { enterApp(true); } else { showLanding(); }

    els.noteAuthorTeam.value = lsGet('octo.authorTeam') || '';
    els.noteAuthorName.value = lsGet('octo.authorName') || '';
    updateCounter();

    els.seasonSelect.addEventListener('change', function () {
      state.year = Number(els.seasonSelect.value) || 2026;
      resetWorkspace();
      loadEvents();
    });
    els.eventSelect.addEventListener('change', onEventChange);
    els.matchSelect.addEventListener('change', function () {
      if (els.matchSelect.value) { selectMatch(els.matchSelect.value); } else { resetWorkspace(); }
    });
    els.retryEvents.addEventListener('click', loadEvents);
    els.teamForm.addEventListener('submit', onTeamSearch);
    els.tabMatch.addEventListener('click', function () { switchTab('match'); });
    els.tabTeam.addEventListener('click', function () { switchTab('team'); });
    [els.tabMatch, els.tabTeam].forEach(function (tab) {
      tab.addEventListener('keydown', function (ev) {
        if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') {
          var next = tab === els.tabMatch ? 'team' : 'match';
          switchTab(next);
          (next === 'match' ? els.tabMatch : els.tabTeam).focus();
        }
      });
    });
    els.noteForm.addEventListener('submit', onNoteSubmit);
    els.noteText.addEventListener('input', updateCounter);
    els.notesRefresh.addEventListener('click', loadNotes);
    window.addEventListener('hashchange', function () {
      var h = parseHash();
      if (h.event && isLanding()) { enterApp(true); }
      if (h.event && h.event !== state.eventKey && state.events.some(function (e) { return e.key === h.event; })) { restoreFromHash(); }
      else if (h.match && (!state.match || state.match.key !== h.match)) { pendingHash = h; maybeRestoreFromHash(); }
    });

    loadEvents().then(restoreFromHash);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
