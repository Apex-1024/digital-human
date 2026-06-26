// 课灵 AI 批量制课系统 · 前端逻辑
const state = {
  pptxPath: null,
  pptxName: null,
  pptxSize: 0,
  avatar: 'teacher_female',
  voice: 'zh-CN-XiaoxiaoNeural',
  ratio: '16:9',
  resolution: '720p',
  enableSubtitle: true,
  enableBgm: true,
  jobs: [],
  pollTimer: null,
};

// ============ 工具 ============
const $ = (id) => document.getElementById(id);
const api = {
  get: (url) => fetch(url).then(r => r.json()),
  post: (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }).then(r => r.json()),
  del: (url) => fetch(url, { method: 'DELETE' }).then(r => r.json()),
  upload: (url, form) => fetch(url, { method: 'POST', body: form }).then(r => r.json()),
};

function toast(msg, type = '') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast show ' + type;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 3000);
}

function formatSize(mb) {
  return mb < 1 ? (mb * 1024).toFixed(0) + ' KB' : mb.toFixed(2) + ' MB';
}

// ============ 启动 ============
async function init() {
  await checkHealth();
  await loadAvatars();
  await loadVoices();
  await loadJobs();
  bindEvents();
  startPolling();
}

async function checkHealth() {
  const h = await api.get('/api/health');
  const el = $('health');
  if (h.ok && h.ffmpeg) {
    el.classList.add('ok');
    el.querySelector('.txt').textContent = '服务正常 · ffmpeg 已就绪';
  } else if (h.ok) {
    el.querySelector('.txt').textContent = '服务正常 · ⚠️ ffmpeg 未检测到';
  } else {
    el.querySelector('.txt').textContent = '服务异常';
  }
}

async function loadAvatars() {
  const r = await api.get('/api/avatars');
  const grid = $('avatarGrid');
  grid.innerHTML = r.avatars.map(a => `
    <div class="avatar-item ${a.id === state.avatar ? 'active' : ''}" data-id="${a.id}">
      <img src="/api/avatars/${a.id}/preview" onerror="this.src='/api/avatars/teacher_female/preview'">
      <div class="name">${a.name}</div>
    </div>
  `).join('');
  grid.querySelectorAll('.avatar-item').forEach(el => {
    el.onclick = () => {
      state.avatar = el.dataset.id;
      grid.querySelectorAll('.avatar-item').forEach(x => x.classList.remove('active'));
      el.classList.add('active');
    };
  });
}

async function loadVoices() {
  const r = await api.get('/api/voices');
  const sel = $('voiceSelect');
  sel.innerHTML = r.voices.map(v => `<option value="${v.id}" ${v.id === state.voice ? 'selected' : ''}>${v.name}</option>`).join('');
  sel.onchange = () => state.voice = sel.value;
}

// ============ 事件绑定 ============
function bindEvents() {
  // 拖拽
  const dz = $('dropzone');
  const fi = $('fileInput');
  dz.onclick = () => fi.click();
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('dragover'); };
  dz.ondragleave = () => dz.classList.remove('dragover');
  dz.ondrop = (e) => {
    e.preventDefault();
    dz.classList.remove('dragover');
    if (e.dataTransfer.files[0]) handleFile(e.dataTransfer.files[0]);
  };
  fi.onchange = (e) => { if (e.target.files[0]) handleFile(e.target.files[0]); };

  // 画幅 / 分辨率
  bindSeg('ratioSeg', 'ratio');
  bindSeg('resSeg', 'resolution');

  // 复选
  $('optSubtitle').onchange = (e) => state.enableSubtitle = e.target.checked;
  $('optBgm').onchange = (e) => state.enableBgm = e.target.checked;

  // 提交
  $('btnSubmit').onclick = submitJob;
  $('btnRefresh').onclick = loadJobs;
}

function bindSeg(id, key) {
  const seg = $(id);
  seg.querySelectorAll('button').forEach(btn => {
    btn.onclick = () => {
      state[key] = btn.dataset.val;
      seg.querySelectorAll('button').forEach(x => x.classList.remove('active'));
      btn.classList.add('active');
    };
  });
}

// ============ 上传 ============
async function handleFile(file) {
  const ext = file.name.toLowerCase().split('.').pop();
  if (!['pptx', 'ppt'].includes(ext)) {
    toast('只支持 .pptx / .ppt 文件', 'error');
    return;
  }
  if (file.size > 200 * 1024 * 1024) {
    toast('文件超过 200MB 限制', 'error');
    return;
  }
  const form = new FormData();
  form.append('file', file);
  toast('上传中...');
  const r = await api.upload('/api/upload', form);
  if (r.error) { toast(r.error, 'error'); return; }
  state.pptxPath = r.path;
  state.pptxName = r.filename;
  state.pptxSize = r.size_mb;
  $('fileInfo').hidden = false;
  $('fiName').textContent = r.filename;
  $('fiMeta').textContent = formatSize(r.size_mb);
  $('btnSubmit').disabled = false;
  toast('上传成功', 'success');
}

// ============ 提交任务 ============
async function submitJob() {
  if (!state.pptxPath) { toast('请先上传 PPT', 'error'); return; }
  $('btnSubmit').disabled = true;
  const r = await api.post('/api/jobs', {
    pptx_path: state.pptxPath,
    avatar: state.avatar,
    voice: state.voice,
    ratio: state.ratio,
    resolution: state.resolution,
    enable_subtitle: state.enableSubtitle,
    enable_bgm: state.enableBgm,
  });
  if (r.error) { toast(r.error, 'error'); $('btnSubmit').disabled = false; return; }
  toast('任务已提交：' + r.job_id, 'success');
  $('btnSubmit').disabled = false;
  loadJobs();
}

// ============ 任务列表 ============
async function loadJobs() {
  const r = await api.get('/api/jobs');
  state.jobs = r.jobs || [];
  renderJobs();
}

function renderJobs() {
  const list = $('jobList');
  if (!state.jobs.length) {
    list.innerHTML = '<div class="empty">还没有任务，先拖入 PPT 吧 ✨</div>';
    return;
  }
  list.innerHTML = state.jobs.map(j => {
    const pct = Math.round((j.progress || 0) * 100);
    const actions = [];
    if (j.status === 'running' || j.status === 'pending') {
      actions.push(`<button class="btn danger" onclick="cancelJob('${j.job_id}')">取消</button>`);
    }
    if (j.status === 'done' && j.output_path) {
      actions.push(`<a class="btn success" href="/api/jobs/${j.job_id}/download" download>⬇ 下载视频</a>`);
    }
    actions.push(`<button class="btn danger" onclick="deleteJob('${j.job_id}')">删除</button>`);

    return `
      <div class="job-item ${j.status}">
        <div class="job-head">
          <div>
            <div class="job-name">${j.filename}</div>
            <div class="job-id">ID: ${j.job_id} · ${j.avatar} · ${j.ratio} · ${j.resolution}</div>
          </div>
          <span class="badge ${j.status}">${statusLabel(j.status)}</span>
        </div>
        <div class="progress-bar">
          <div class="progress-fill ${j.status}" style="width:${pct}%"></div>
        </div>
        <div class="job-stage">${j.stage} · ${pct}%</div>
        ${j.error ? `<div class="job-error">❌ ${j.error}</div>` : ''}
        <div class="job-actions">${actions.join('')}</div>
      </div>
    `;
  }).join('');
}

function statusLabel(s) {
  return { pending: '等待中', running: '渲染中', done: '已完成', failed: '失败', canceled: '已取消' }[s] || s;
}

window.cancelJob = async (id) => {
  await api.post(`/api/jobs/${id}/cancel`);
  toast('已取消');
  loadJobs();
};
window.deleteJob = async (id) => {
  if (!confirm('确认删除？输出文件会一并清理。')) return;
  await api.del(`/api/jobs/${id}`);
  loadJobs();
};

// ============ 轮询 ============
function startPolling() {
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    if (state.jobs.some(j => j.status === 'running' || j.status === 'pending')) {
      await loadJobs();
    }
  }, 2000);
}

init();
