(() => {
  'use strict';
  const body = document.body;
  const DEFAULT_RIGHTS = body.dataset.defaultRights || 'YOUSSEF';
  const MAX_STREAMS = Number(body.dataset.maxStreams || 10);
  const MAX_GROUP_ROWS = 20;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  }
  function toast(message, type = 'success') {
    const region = $('#toastRegion');
    if (!region) return;
    const item = document.createElement('div');
    item.className = `toast ${type}`;
    item.textContent = message;
    region.appendChild(item);
    setTimeout(() => item.remove(), 4200);
  }
  async function api(url, options = {}) {
    const response = await fetch(url, {
      credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', ...(options.headers || {})},
      ...options,
    });
    let data = {};
    try { data = await response.json(); } catch { /* non-JSON response */ }
    if (response.status === 401) {
      location.href = '/login';
      throw new Error('انتهت الجلسة. سجّل الدخول مجددًا.');
    }
    if (!response.ok) throw new Error(data.detail || data.message || `حدث خطأ (${response.status}).`);
    return data;
  }
  function setBusy(button, busy, busyText = 'جارٍ التنفيذ…') {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.innerHTML;
      button.innerHTML = `<span class="spinner"></span> ${escapeHtml(busyText)}`;
      button.disabled = true;
    } else {
      button.innerHTML = button.dataset.originalText || button.innerHTML;
      button.disabled = false;
    }
  }
  function statusLabel(status) {
    const labels = {starting: 'يبدأ التشغيل', running: 'يعمل', reconnecting: 'إعادة اتصال', 'جاهز': 'جاهز', stopped: 'متوقف', error: 'خطأ'};
    return labels[status] || status || 'غير معروف';
  }
  function statusClass(status) {
    if (status === 'running' || status === 'جاهز') return 'running';
    if (status === 'reconnecting') return 'reconnecting';
    return 'starting';
  }
  function renderStreams(streams) {
    const container = $('#streamsList');
    $('#streamCount').textContent = String(streams.length);
    if (!streams.length) {
      container.innerHTML = '<div class="empty-state"><span>◌</span><b>لا توجد بثوث حاليًا</b><p>سيظهر أي بث تبدأه في هذه القائمة.</p></div>';
      return;
    }
    container.innerHTML = streams.map(stream => `
      <article class="stream-card">
        <div class="stream-details">
          <div class="stream-title-line"><span class="stream-title">${escapeHtml(stream.name)}</span><span class="status-badge ${statusClass(stream.status)}">${escapeHtml(statusLabel(stream.status))}</span></div>
          <div class="stream-meta"><span>النوع: ${escapeHtml(stream.type)}</span><span>الوضع: ${escapeHtml(stream.mode_label)}</span><span>المدة: <b dir="ltr">${escapeHtml(stream.uptime)}</b></span><span>محاولات: ${Number(stream.retry_count || 0)}</span>${stream.rights_text ? `<span>© ${escapeHtml(stream.rights_text)}</span>` : ''}</div>
          ${stream.last_error ? `<div class="stream-error">${escapeHtml(stream.last_error)}</div>` : ''}
        </div>
        <div class="stream-actions"><button class="mini-button" data-log-name="${escapeHtml(stream.name)}">السجلات</button><button class="mini-button stop" data-stop-name="${escapeHtml(stream.name)}">إيقاف البث</button></div>
      </article>`).join('');
    $$('[data-stop-name]', container).forEach(btn => btn.addEventListener('click', async () => {
      const name = btn.dataset.stopName;
      if (!confirm(`إيقاف البث «${name}»؟`)) return;
      btn.disabled = true;
      try { const result = await api('/api/stream/stop', {method:'POST', body:JSON.stringify({name})}); toast(result.message); await refreshState(); }
      catch (error) { toast(error.message, 'error'); btn.disabled = false; }
    }));
    $$('[data-log-name]', container).forEach(btn => btn.addEventListener('click', () => openLogs(btn.dataset.logName)));
  }
  function renderHls(jobs) {
    const container = $('#hlsList');
    $('#hlsCount').textContent = String(jobs.length);
    if (!jobs.length) { container.innerHTML = '<div class="empty-inline">لا توجد روابط HLS نشطة.</div>'; return; }
    container.innerHTML = jobs.map(job => `
      <article class="hls-card">
        <div class="hls-card-top"><span class="hls-card-title">HLS · ${escapeHtml(job.id)}</span><span class="status-badge ${statusClass(job.status)}">${escapeHtml(statusLabel(job.status))}</span></div>
        <div class="stream-meta"><span>المدة: <b dir="ltr">${escapeHtml(job.uptime)}</b></span><span>محاولات: ${Number(job.retry_count || 0)}</span>${job.add_rights ? `<span>© ${escapeHtml(job.rights_text)}</span>` : '<span>بدون حقوق</span>'}</div>
        <code class="hls-url">${escapeHtml(job.url)}</code>
        ${job.last_error ? `<div class="stream-error">${escapeHtml(job.last_error)}</div>` : ''}
        <div class="hls-actions"><button class="mini-button" data-copy-url="${escapeHtml(job.url)}">نسخ الرابط</button><button class="mini-button" data-hls-log="${escapeHtml(job.id)}">السجلات</button><button class="mini-button stop" data-stop-hls="${escapeHtml(job.id)}">إيقاف HLS</button></div>
      </article>`).join('');
    $$('[data-copy-url]', container).forEach(btn => btn.addEventListener('click', async () => {
      const value = btn.dataset.copyUrl;
      try { await navigator.clipboard.writeText(value); toast('تم نسخ رابط HLS.'); }
      catch { const temp = document.createElement('textarea'); temp.value = value; document.body.appendChild(temp); temp.select(); document.execCommand('copy'); temp.remove(); toast('تم نسخ الرابط.'); }
    }));
    $$('[data-hls-log]', container).forEach(btn => btn.addEventListener('click', () => openHlsLogs(btn.dataset.hlsLog)));
    $$('[data-stop-hls]', container).forEach(btn => btn.addEventListener('click', async () => {
      const id = btn.dataset.stopHls;
      if (!confirm(`إيقاف رابط HLS ${id}؟`)) return;
      btn.disabled = true;
      try { const result = await api('/api/hls/stop', {method:'POST', body:JSON.stringify({id})}); toast(result.message); await refreshState(); }
      catch (error) { toast(error.message, 'error'); btn.disabled = false; }
    }));
  }
  async function refreshState(quiet = true) {
    try {
      const data = await api('/api/state');
      renderStreams(data.streams || []);
      renderHls(data.hls_jobs || []);
      $('#streamLimit').textContent = String(data.limits?.streams ?? MAX_STREAMS);
      $('#ffmpegStatus').textContent = data.ffmpeg_available ? 'جاهز' : 'غير مثبت';
      $('#ffprobeStatus').textContent = data.ffprobe_available ? 'جاهز' : 'غير مثبت';
      $('#ffmpegValue').textContent = data.ffmpeg_available ? 'Ready' : 'Missing';
      $('#ffmpegValue').style.color = data.ffmpeg_available ? 'var(--green)' : 'var(--red)';
      $('#serverText').textContent = data.ffmpeg_available && data.ffprobe_available ? 'متصل' : 'تحقق من الأدوات';
      $('#serverText').style.color = data.ffmpeg_available && data.ffprobe_available ? 'var(--green)' : 'var(--amber)';
    } catch (error) {
      if (!quiet) toast(error.message, 'error');
    }
  }
  async function openLogs(name) {
    try {
      const data = await api(`/api/stream/logs?name=${encodeURIComponent(name)}`);
      $('#modalTitle').textContent = `سجلات البث: ${name}`;
      $('#modalBody').textContent = data.logs || 'لا توجد سجلات.';
      $('#modal').hidden = false;
    } catch (error) { toast(error.message, 'error'); }
  }
  async function openHlsLogs(id) {
    try {
      const data = await api(`/api/hls/logs?id=${encodeURIComponent(id)}`);
      $('#modalTitle').textContent = `سجلات HLS: ${id}`;
      $('#modalBody').textContent = data.logs || 'لا توجد سجلات.';
      $('#modal').hidden = false;
    } catch (error) { toast(error.message, 'error'); }
  }
  $('#closeModal')?.addEventListener('click', () => { $('#modal').hidden = true; });
  $('#modal')?.addEventListener('click', event => { if (event.target.id === 'modal') $('#modal').hidden = true; });
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && $('#modal')) $('#modal').hidden = true; });

  // SOLO stream
  $('#soloForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#soloSubmit'); setBusy(button, true, 'جارٍ فحص المصدر والتشغيل…');
    try {
      const body = {
        name: $('#soloName').value,
        key: $('#soloKey').value,
        url: $('#soloUrl').value,
        mode: $('#soloMode').value,
        add_rights: $('#addRights').checked,
        rights_text: $('#rightsText').value || DEFAULT_RIGHTS,
      };
      const data = await api('/api/stream/start', {method:'POST', body:JSON.stringify(body)});
      toast(data.message || 'تم تجهيز البث.');
      $('#soloName').value = '';
      $('#soloKey').value = '';
      await refreshState();
      document.querySelector('#activeSection').scrollIntoView({behavior:'smooth', block:'start'});
    } catch (error) { toast(error.message, 'error'); }
    finally { setBusy(button, false); }
  });

  // Dynamic group form, one row per source.
  function addGroupRow(prefill = {}) {
    const container = $('#groupRows');
    const current = $$('.group-row', container).length;
    if (current >= MAX_GROUP_ROWS) { toast(`الحد الأقصى للمجموعة ${MAX_GROUP_ROWS} بثوث.`, 'error'); return; }
    const node = document.createElement('div');
    node.className = 'group-row';
    node.innerHTML = `
      <div class="group-row-head"><span>مصدر البث ${current + 1}</span><button class="remove-row" type="button" aria-label="حذف المصدر" title="حذف المصدر">×</button></div>
      <div class="field"><label>اسم البث</label><input data-field="name" required maxlength="80" placeholder="اسم مميز" value="${escapeHtml(prefill.name || '')}"></div>
      <div class="field"><label>مفتاح Facebook</label><input data-field="key" required maxlength="512" autocomplete="off" placeholder="Stream Key" value="${escapeHtml(prefill.key || '')}"></div>
      <div class="field field-full"><label>رابط المصدر</label><input data-field="url" type="url" required maxlength="8192" dir="ltr" placeholder="https://example.com/live.m3u8" value="${escapeHtml(prefill.url || '')}"></div>`;
    node.querySelector('.remove-row').addEventListener('click', () => {
      if ($$('.group-row', container).length === 1) { toast('يجب إبقاء مصدر واحد على الأقل.', 'error'); return; }
      node.remove(); renumberRows();
    });
    container.appendChild(node);
  }
  function renumberRows() {
    $$('.group-row-head span', $('#groupRows')).forEach((el, index) => { el.textContent = `مصدر البث ${index + 1}`; });
  }
  $('#addGroupRow')?.addEventListener('click', () => addGroupRow());
  addGroupRow();

  $('#groupForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const rows = $$('.group-row', $('#groupRows'));
    if (!rows.length) { toast('أضف مصدر بث واحدًا على الأقل.', 'error'); return; }
    const streams = rows.map(row => ({name: row.querySelector('[data-field="name"]').value, key: row.querySelector('[data-field="key"]').value, url: row.querySelector('[data-field="url"]').value}));
    const button = $('#groupSubmit'); setBusy(button, true, 'جارٍ فحص المجموعة…');
    try {
      const data = await api('/api/stream/group', {method:'POST', body:JSON.stringify({streams, mode:$('#groupMode').value})});
      toast(data.message || 'تم إرسال المجموعة للتشغيل.');
      $('#groupRows').innerHTML = ''; addGroupRow();
      await refreshState();
      document.querySelector('#activeSection').scrollIntoView({behavior:'smooth', block:'start'});
    } catch (error) { toast(error.message, 'error'); }
    finally { setBusy(button, false); }
  });

  $('#stopAllButton')?.addEventListener('click', async () => {
    if (!confirm('هل تريد إرسال أمر إيقاف لجميع البثوث؟')) return;
    const button = $('#stopAllButton'); button.disabled = true;
    try { const data = await api('/api/stream/stop-all', {method:'POST', body:'{}'}); toast(data.message); await refreshState(); }
    catch (error) { toast(error.message, 'error'); }
    finally { button.disabled = false; }
  });
  $('#refreshButton')?.addEventListener('click', () => refreshState(false));

  $('#checkForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#checkSubmit'); const resultBox = $('#checkResult');
    resultBox.hidden = true; setBusy(button, true, 'جارٍ الفحص…');
    try {
      const data = await api('/api/check-url', {method:'POST', body:JSON.stringify({url:$('#checkUrl').value})});
      const p = data.probe || {};
      resultBox.className = 'check-result success';
      resultBox.innerHTML = `<h4>✓ المصدر قابل للقراءة</h4><div class="probe-grid"><div class="probe-item"><small>الصيغة</small><b>${escapeHtml(p.format || '—')}</b></div><div class="probe-item"><small>المدة بالثواني</small><b>${escapeHtml(p.duration || 'غير محددة')}</b></div><div class="probe-item"><small>ترميز الفيديو</small><b>${escapeHtml(p.video || 'غير موجود')}</b></div><div class="probe-item"><small>ترميز الصوت</small><b>${escapeHtml(p.audio || 'غير موجود')}</b></div></div>`;
      resultBox.hidden = false; toast('نجح فحص رابط المصدر.');
    } catch (error) {
      resultBox.className = 'check-result failed';
      resultBox.innerHTML = `<h4>تعذر قراءة المصدر</h4><div>${escapeHtml(error.message)}</div>`;
      resultBox.hidden = false;
    } finally { setBusy(button, false); }
  });

  $('#hlsForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = $('#hlsSubmit'); setBusy(button, true, 'جارٍ فحص المصدر وإنشاء HLS…');
    try {
      let sourceUrl = $('#hlsUrl').value.trim();
      const txtFile = $('#hlsTxtFile').files?.[0];
      if (!sourceUrl && txtFile) {
        if (txtFile.size > 20000000) throw new Error('حجم ملف TXT يتجاوز 20 ميغابايت.');
        const content = await txtFile.text();
        sourceUrl = content.split(/\r?\n/).map(line => line.trim()).find(line => line && !line.startsWith('#')) || '';
      }
      if (!sourceUrl) throw new Error('أدخل رابط المصدر أو اختر ملف TXT.');
      const data = await api('/api/hls/start', {method:'POST', body:JSON.stringify({url:sourceUrl, add_rights:$('#hlsAddRights').checked, rights_text:$('#hlsRightsText').value || DEFAULT_RIGHTS})});
      toast(data.message || 'بدأ إنشاء رابط HLS.');
      $('#hlsUrl').value = '';
      $('#hlsTxtFile').value = '';
      await refreshState();
      document.querySelector('#hlsList').scrollIntoView({behavior:'smooth', block:'nearest'});
    } catch (error) { toast(error.message, 'error'); }
    finally { setBusy(button, false); }
  });

  $('#logoutButton')?.addEventListener('click', async () => {
    try { await api('/api/logout', {method:'POST', body:'{}'}); location.href = '/login'; }
    catch (error) { toast(error.message, 'error'); }
  });

  // Local clock; server work continues when the browser is closed because FFmpeg runs on the host.
  function setClock() { const now = new Date(); $('#clock').textContent = now.toLocaleDateString('ar-MA', {day:'2-digit', month:'short'}) + ' · ' + now.toLocaleTimeString('ar-MA', {hour:'2-digit', minute:'2-digit'}); }
  setClock(); setInterval(setClock, 30000);
  refreshState(false);
  setInterval(() => refreshState(true), 3500);
})();
