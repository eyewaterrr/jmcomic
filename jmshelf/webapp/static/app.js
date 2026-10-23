'use strict';
// 书架 Mini App
// 路由：顶部标签 #/ 书架、#/search 搜索、#/jobs 任务；#/folders 收藏夹管理，#/a/<JM号> 书库里的作品，
//       #/jm/<JM号> JM 作品详情（搜索结果），#/r/<章节ID>/<页码> 阅读
// 启动参数：startapp=c<章节ID> 打开该章；startapp=a<JM号>、?read=<JM号> 从上次位置继续读这本
(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const app = document.getElementById('app');
  const toastEl = document.getElementById('toast');
  const S = { token: '', user: null, cleanup: null, parent: null, tab: 'library', search: null };

  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'style') node.style.cssText = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else if (k in node) node[k] = v;
      else node.setAttribute(k, v === true ? '' : v);
    }
    for (const c of children.flat()) {
      if (c != null && c !== false) node.append(c.nodeType ? c : String(c));
    }
    return node;
  }

  const store = {
    get(key, fallback) {
      try { return localStorage.getItem(key) ?? fallback; } catch (e) { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, value); } catch (e) { /* 隐私模式等 */ }
    },
  };

  let toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2800);
  }

  function confirmDialog(message) {
    return new Promise((resolve) => {
      if (tg && tg.showConfirm) {
        try { tg.showConfirm(message, (ok) => resolve(!!ok)); return; } catch (e) { /* 旧版客户端 */ }
      }
      resolve(window.confirm(message));
    });
  }

  const notice = (text) => app.replaceChildren(el('p', { class: 'notice' }, text));
  const go = (hash) => { location.hash = hash; };
  const imgUrl = (photo, n) => `/img/${photo}/${n}?t=${encodeURIComponent(S.token)}`;
  const coverUrl = (jm) => `/cover/${jm}?t=${encodeURIComponent(S.token)}`;
  const NEXT_TEXT = {
    archive: '已收藏，正在入库，完成后自动生成 PDF',
    pdf: '已收藏，正在生成 PDF，进度见 Bot 私聊',
    done: '已保存',
    none: '已取消收藏，精选频道里的 PDF 保留不删',
  };

  // ---- 登录与请求 ----
  async function login() {
    const r = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ initData: tg.initData }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `登录失败（${r.status}）`);
    S.token = d.token;
    S.user = d.user;
    return d;
  }

  async function api(path, body) {
    for (let attempt = 0; attempt < 2; attempt++) {
      const opts = { headers: { Authorization: `Bearer ${S.token}` } };
      if (body !== undefined) {
        opts.method = 'POST';
        opts.headers['Content-Type'] = 'application/json';
        opts.body = JSON.stringify(body);
      }
      const r = await fetch(path, opts);
      if (r.status === 401 && attempt === 0) { await login(); continue; }
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `请求失败（${r.status}）`);
      return d;
    }
  }

  // ---- 路由 ----
  function parse() {
    const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean);
    if (parts[0] === 'a' && parts[1]) return { view: 'album', jm: Number(parts[1]) };
    if (parts[0] === 'r' && parts[1]) return { view: 'reader', photo: Number(parts[1]), page: Number(parts[2]) || null };
    if (parts[0] === 'folders') return { view: 'folders' };
    if (parts[0] === 'search') return { view: 'search' };
    if (parts[0] === 'jobs') return { view: 'jobs' };
    if (parts[0] === 'jm' && parts[1]) return { view: 'jm', jm: Number(parts[1]) };
    return { view: 'library' };
  }

  function themeDark(dark) {
    try {
      tg.setHeaderColor(dark ? '#000000' : 'bg_color');
      tg.setBackgroundColor(dark ? '#000000' : 'bg_color');
    } catch (e) { /* 旧版客户端不支持 */ }
  }

  async function render() {
    if (S.cleanup) { S.cleanup(); S.cleanup = null; }
    document.querySelectorAll('.sheet-bg').forEach((n) => n.remove());
    document.body.classList.remove('reading');
    themeDark(false);
    const route = parse();
    window.scrollTo(0, 0);
    try {
      if (route.view === 'album') await renderAlbum(route.jm);
      else if (route.view === 'reader') await renderReader(route.photo, route.page);
      else if (route.view === 'folders') await renderFolders();
      else if (route.view === 'search') await renderSearch();
      else if (route.view === 'jobs') await renderJobs();
      else if (route.view === 'jm') await renderJm(route.jm);
      else await renderLibrary();
    } catch (e) {
      notice(e.message || String(e));
    }
    const top = ['library', 'search', 'jobs'].includes(route.view);
    if (tg.BackButton) top ? tg.BackButton.hide() : tg.BackButton.show();
  }

  // 从上次读到的位置继续读这本：返回要跳转的路由
  async function continueTarget(jm) {
    try {
      const a = await api(`/api/album/${jm}`);
      if (a.progress) return `#/r/${a.progress.photoId}/${a.progress.page}`;
      if (a.chapters.length) return `#/r/${a.chapters[0].photoId}`;
      return `#/a/${jm}`;
    } catch (e) {
      toast(e.message);
      return '#/';
    }
  }

  // ---- 书架 ----
  async function renderLibrary() {
    S.parent = null;
    S.tab = 'library';
    const data = await api('/api/library');
    let filter = store.get('jm.filter', 'all');
    let folder = Number(store.get('jm.folder', '0')) || 0;
    if (folder && !data.folders.some((f) => f.id === folder)) folder = 0;

    const count = el('span', { class: 'muted' });
    const seg = el('div', { class: 'seg' });
    const chips = el('div', { class: 'chips' });
    const grid = el('div', { class: 'grid' });
    const empty = el('p', { class: 'notice' });

    const draw = () => {
      seg.replaceChildren(...[['all', '全部'], ['fav', '已收藏'], ['unfav', '未收藏']].map(([key, label]) =>
        el('button', { class: key === filter ? 'on' : '', onclick: () => { filter = key; store.set('jm.filter', key); draw(); } }, label)));

      chips.hidden = filter !== 'fav';
      chips.replaceChildren(
        el('button', { class: folder === 0 ? 'chip on' : 'chip', onclick: () => { folder = 0; store.set('jm.folder', '0'); draw(); } }, '全部收藏夹'),
        ...data.folders.map((f) => el('button', {
          class: folder === f.id ? 'chip on' : 'chip',
          onclick: () => { folder = f.id; store.set('jm.folder', String(f.id)); draw(); },
        }, `${f.name} ${f.count}`)),
        el('button', { class: 'chip ghost', onclick: () => go('#/folders') }, '管理收藏夹'));

      let items = data.items;
      if (filter === 'fav') items = items.filter((i) => i.fav && (!folder || i.folders.includes(folder)));
      else if (filter === 'unfav') items = items.filter((i) => !i.fav);
      count.textContent = `${items.length} 本`;
      grid.replaceChildren(...items.map(card));
      empty.hidden = items.length > 0;
      empty.textContent = data.items.length
        ? (filter === 'fav' ? '这里还没有作品。在作品页点“收藏”，或在 Bot 里点“收藏”。' : '没有符合条件的作品。')
        : '书架是空的。在 Bot 里发送 JM 号或关键词，点“入库”后就会出现在这里。';
    };
    draw();
    app.replaceChildren(tabsBar('library'), seg, chips, el('div', { class: 'sub' }, count), grid, empty);
  }

  function card(it) {
    const meta = [it.chapters > 1 ? `${it.archived}/${it.chapters} 章` : `${it.pages} 页`];
    if (it.progress) meta.push(it.chapters > 1 ? `读到 ${it.progress.sort}-${it.progress.page}` : `读到 ${it.progress.page}`);
    return el('a', { class: 'card', href: `#/a/${it.jmId}` },
      el('div', { class: 'cover' },
        it.cover ? el('img', { src: coverUrl(it.jmId), loading: 'lazy', alt: '' }) : null,
        it.fav ? el('span', { class: 'badge' }, '已收藏') : null),
      el('div', { class: 't' }, it.title),
      el('div', { class: 'm' }, meta.join(' · ')));
  }

  // ---- 收藏夹管理 ----
  async function renderFolders() {
    S.parent = '#/';
    const { folders } = await api('/api/folders');
    const name = el('input', { class: 'input', placeholder: '新收藏夹的名字', maxLength: 30 });
    const create = el('button', {
      class: 'primary small',
      onclick: async () => {
        try { await api('/api/folders', { name: name.value }); render(); } catch (e) { toast(e.message); }
      },
    }, '新建');

    const row = (f) => {
      const info = el('div', { class: 'grow' }, el('div', {}, f.name), el('div', { class: 'muted small' },
        `${f.count} 本` + (f.source === 'jm' ? ' · 由 JM 同步，不能手动修改' : f.default ? ' · 默认' : '')));
      const actions = [];
      if (f.source === 'local') {
        actions.push(el('button', {
          class: 'chip',
          onclick: () => {
            const input = el('input', { class: 'input', value: f.name, maxLength: 30 });
            li.replaceChildren(input,
              el('button', {
                class: 'chip',
                onclick: async () => {
                  try { await api(`/api/folders/${f.id}/rename`, { name: input.value }); render(); } catch (e) { toast(e.message); }
                },
              }, '保存'),
              el('button', { class: 'chip ghost', onclick: () => render() }, '取消'));
            input.focus();
          },
        }, '改名'));
      }
      if (f.source === 'local' && !f.default) {
        actions.push(el('button', {
          class: 'chip danger',
          onclick: async () => {
            if (!await confirmDialog(`删除收藏夹“${f.name}”？里面的作品不会被删除。`)) return;
            try { await api(`/api/folders/${f.id}/delete`, {}); render(); } catch (e) { toast(e.message); }
          },
        }, '删除'));
      }
      const li = el('li', { class: 'folder-row' }, info, ...actions);
      return li;
    };

    app.replaceChildren(el('div', { class: 'album' },
      el('h2', {}, '收藏夹'),
      el('p', { class: 'muted small' }, '作品放进任意一个收藏夹就算收藏，会自动生成 PDF 发到精选频道。'),
      el('div', { class: 'row' }, name, create),
      el('ol', { class: 'chapters' }, folders.map(row))));
  }

  // ---- 作品 ----
  async function renderAlbum(jm) {
    S.parent = '#/';
    const a = await api(`/api/album/${jm}`);
    const prog = a.progress;
    const multi = a.chapterCount > 1;
    const at = prog && a.chapters.find((c) => c.photoId === prog.photoId);
    const first = a.chapters[0];

    let label = '开始阅读';
    if (at) label = multi ? `继续阅读：第 ${at.sort} 章 第 ${prog.page} 页` : `继续阅读：第 ${prog.page} 页`;
    const readBtn = el('button', {
      class: 'primary', disabled: !first,
      onclick: () => go(at ? `#/r/${at.photoId}/${prog.page}` : `#/r/${first.photoId}`),
    }, first ? label : '还没有入库的章节');
    const favBtn = el('button', { class: 'secondary', onclick: () => openFolderSheet(a) }, a.fav ? '管理收藏夹' : '收藏');

    const member = a.folders.filter((f) => f.member).map((f) => f.name);
    const pdfText = { done: '已生成', running: '生成中' }[a.pdf] || (a.fav ? '等入库完成后生成' : '收藏后自动生成');

    const list = el('ol', { class: 'chapters' }, a.chapters.map((c) => {
      const here = prog && prog.photoId === c.photoId;
      return el('li', {}, el('a', { href: `#/r/${c.photoId}` },
        el('span', { class: 'name' }, multi ? `第 ${c.sort} 章  ${c.title}` : a.title),
        el('span', { class: here ? 'info here' : 'info' }, here ? `读到 ${prog.page}/${c.pages}` : `${c.pages} 页`)));
    }));
    const missing = a.chapterCount - a.chapters.length;

    app.replaceChildren(el('div', { class: 'album' },
      el('div', { class: 'album-head' },
        el('div', { class: 'cover' }, a.cover ? el('img', { src: coverUrl(jm), alt: '' }) : null),
        el('div', {},
          el('h2', {}, a.title),
          el('p', { class: 'muted' }, `JM${jm}`),
          a.authors.length ? el('p', { class: 'muted' }, a.authors.join('、')) : null,
          a.tags.length ? el('p', { class: 'muted' }, a.tags.join(' · ')) : null)),
      el('div', { class: 'actions' }, readBtn, favBtn),
      el('p', { class: 'muted small' }, member.length ? `收藏夹：${member.join('、')}` : '未收藏', `　PDF：${pdfText}`),
      el('h3', {}, missing > 0 ? `章节（已入库 ${a.chapters.length}/${a.chapterCount}）` : `章节（${a.chapters.length}）`),
      list,
      missing > 0 ? el('p', { class: 'muted' }, `还有 ${missing} 章没有入库，可以在 Bot 里点“入库”补齐。`) : null));
  }

  function openFolderSheet(a) {
    const checks = new Map();
    const options = el('div', { class: 'checks' });
    const addOption = (f, checked) => {
      const box = el('input', { type: 'checkbox', checked });
      checks.set(f.id, box);
      options.append(el('label', { class: 'check' }, box, el('span', {}, f.name)));
    };
    a.folders.filter((f) => f.source === 'local').forEach((f) => addOption(f, a.fav ? f.member : f.default));
    const synced = a.folders.filter((f) => f.source === 'jm' && f.member).map((f) => f.name);

    const name = el('input', { class: 'input', placeholder: '新建收藏夹', maxLength: 30 });
    const create = el('button', {
      class: 'chip',
      onclick: async () => {
        try {
          const r = await api('/api/folders', { name: name.value });
          const f = r.folders.find((x) => x.id === r.id);
          addOption(f, true);
          name.value = '';
        } catch (e) { toast(e.message); }
      },
    }, '新建');

    const close = () => bg.remove();
    const save = el('button', {
      class: 'primary',
      onclick: async () => {
        save.disabled = true;
        const ids = [...checks].filter(([, box]) => box.checked).map(([id]) => id);
        try {
          const r = await api(`/api/album/${a.jmId}/folders`, { folderIds: ids });
          close();
          toast(NEXT_TEXT[r.next] || '已保存');
          render();
        } catch (e) {
          toast(e.message);
          save.disabled = false;
        }
      },
    }, '完成');

    const bg = el('div', { class: 'sheet-bg', onclick: (e) => { if (e.target === bg) close(); } },
      el('div', { class: 'sheet' },
        el('h3', {}, '收藏到'),
        options,
        synced.length ? el('p', { class: 'muted small' }, `JM 同步的收藏夹：${synced.join('、')}（不能在这里修改）`) : null,
        el('div', { class: 'row' }, name, create),
        el('p', { class: 'muted small' }, '全部取消勾选就是取消收藏；精选频道里的 PDF 不会删除。'),
        save));
    document.body.append(bg);
  }

  // ---- 顶部标签 ----
  const TABS = [['library', '书架', '#/'], ['search', '搜索', '#/search'], ['jobs', '任务', '#/jobs']];
  function tabsBar(active) {
    return el('nav', { class: 'tabs' }, TABS.map(([key, label, hash]) =>
      el('button', { class: key === active ? 'on' : '', onclick: () => { if (key !== active) go(hash); } }, label)));
  }

  const STATE_TEXT = { archived: '已入库', archiving: '入库中', partial: '部分入库' };
  const jmCoverUrl = (jm) => `/jmcover/${jm}?t=${encodeURIComponent(S.token)}`;

  // ---- 搜索（和给 Bot 发消息的功能相同） ----
  function searchCard(it) {
    return el('a', { class: 'card', href: `#/jm/${it.jmId}`, 'data-jm': it.jmId },
      el('div', { class: 'cover' },
        el('img', { src: jmCoverUrl(it.jmId), loading: 'lazy', alt: '' }),
        STATE_TEXT[it.state] ? el('span', { class: 'badge' }, STATE_TEXT[it.state]) : null,
        it.fav ? el('span', { class: 'badge right' }, '已收藏') : null),
      el('div', { class: 't' }, it.title || `JM${it.jmId}`),
      el('div', { class: 'm' }, it.author ? `JM${it.jmId} · ${it.author}` : `JM${it.jmId}`));
  }

  async function renderSearch() {
    S.tab = 'search';
    const st = S.search || (S.search = { q: '', mode: '', items: [], page: 0, pages: 0, total: 0, scroll: 0 });
    let loading = false;

    const input = el('textarea', {
      class: 'input search-input', rows: 1, value: st.q,
      placeholder: '关键词、JM 号或作品链接，可一次粘贴多个号码',
    });
    const info = el('p', { class: 'muted small search-info' });
    const bulk = el('div', { class: 'chips bulk' });
    const grid = el('div', { class: 'grid' });
    const more = el('p', { class: 'notice small' });

    const drawInfo = () => {
      if (!st.mode) info.textContent = '';
      else info.textContent = st.mode === 'ids' ? `识别到 ${st.items.length} 个 JM 号` : `“${st.q}”共 ${st.total} 条`;
      info.hidden = !info.textContent;
      bulk.replaceChildren();
      if (st.mode === 'ids' && st.items.length > 1) {
        const todo = st.items.filter((i) => i.state === 'none' || i.state === 'partial').map((i) => i.jmId);
        if (todo.length) bulk.append(el('button', { class: 'chip', onclick: () => bulkAction('archive', todo) }, `全部入库（${todo.length} 本）`));
        bulk.append(el('button', { class: 'chip', onclick: () => bulkAction('favorite', st.items.map((i) => i.jmId)) }, '全部收藏'));
      }
      bulk.hidden = !bulk.childElementCount;
      more.hidden = !st.mode || !st.items.length;
      more.textContent = st.mode === 'search' && st.page < st.pages ? '继续下滑加载更多' : '没有更多了';
    };
    const drawAll = () => { grid.replaceChildren(...st.items.map(searchCard)); drawInfo(); };

    // 多个 JM 号时先列出号码，再逐个补上标题
    const fillTitles = async () => {
      for (const it of st.items.filter((i) => !i.title)) {
        try {
          const a = await api(`/api/jm/album/${it.jmId}`);
          it.title = a.title;
          it.author = a.authors.join('、');
        } catch (e) {
          it.title = '取不到详情';
        }
        const card = grid.querySelector(`[data-jm="${it.jmId}"]`);
        if (card) card.replaceWith(searchCard(it));
      }
    };

    const search = async (q) => {
      q = q.trim();
      if (!q || loading) return;
      loading = true;
      input.blur();
      Object.assign(st, { q, mode: '', items: [], page: 0, pages: 0, total: 0 });
      drawAll();
      info.textContent = '搜索中…';
      info.hidden = false;
      try {
        const d = await api(`/api/jm/search?q=${encodeURIComponent(q)}&page=1`);
        Object.assign(st, { mode: d.mode, items: d.items, page: d.page, pages: d.pages, total: d.total });
        drawAll();
        window.scrollTo(0, 0);
        if (d.mode === 'ids') fillTitles();
      } catch (e) {
        info.textContent = e.message;
        info.hidden = false;
      }
      loading = false;
    };

    const loadMore = async () => {
      if (loading || st.mode !== 'search' || st.page >= st.pages) return;
      loading = true;
      more.textContent = '加载中…';
      try {
        const d = await api(`/api/jm/search?q=${encodeURIComponent(st.q)}&page=${st.page + 1}`);
        const seen = new Set(st.items.map((i) => i.jmId));
        const add = d.items.filter((i) => !seen.has(i.jmId));
        st.items.push(...add);
        st.page = d.page;
        grid.append(...add.map(searchCard));
      } catch (e) {
        toast(e.message);
      }
      loading = false;
      drawInfo();
    };

    const bulkAction = async (kind, jmIds) => {
      try {
        const r = await api(`/api/jm/${kind}`, { jmIds });
        if (kind === 'archive') toast(r.queued ? `已加入入库队列 ${r.queued} 本，进度见“任务”` : '都已经入库或正在入库');
        else toast('已全部收藏，入库和 PDF 进度见“任务”');
        const d = await api(`/api/jm/search?q=${encodeURIComponent(st.q)}&page=1`);
        const byId = new Map(d.items.map((i) => [i.jmId, i]));
        st.items.forEach((it) => Object.assign(it, byId.get(it.jmId) || {}, { title: it.title, author: it.author }));
        drawAll();
      } catch (e) {
        toast(e.message);
      }
    };

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); search(input.value); }
    });
    app.replaceChildren(tabsBar('search'),
      el('div', { class: 'searchbar' }, input, el('button', { class: 'primary small', onclick: () => search(input.value) }, '搜索')),
      info, bulk, grid, more);
    drawAll();
    if (st.items.length) window.scrollTo(0, st.scroll);
    else setTimeout(() => input.focus(), 50);

    const io = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) loadMore(); },
      { rootMargin: '600px 0px' });
    io.observe(more);
    S.cleanup = () => { st.scroll = window.scrollY; io.disconnect(); };
  }

  // ---- JM 作品详情（搜索结果点进来） ----
  async function renderJm(jm) {
    S.parent = S.tab === 'jobs' ? '#/jobs' : S.tab === 'library' ? '#/' : '#/search';
    const a = await api(`/api/jm/album/${jm}`);
    const multi = a.chapters.length > 1;

    const act = (label, cls, fn) => el('button', {
      class: cls,
      onclick: async (e) => {
        const btn = e.currentTarget;
        btn.disabled = true;
        try { await fn(); } catch (err) { toast(err.message); }
        render();
      },
    }, label);
    const actions = el('div', { class: 'actions' });
    if (a.archivedChapters > 0) {
      actions.append(el('button', { class: 'primary', onclick: async () => go(await continueTarget(jm)) }, '在线阅读'));
    }
    if (a.state === 'archiving') {
      actions.append(el('button', { class: 'secondary', disabled: true }, '入库中…'));
    } else if (a.state !== 'archived') {
      const label = a.state === 'partial' ? `补齐入库（已入库 ${a.archivedChapters}/${a.chapters.length} 章）` : '入库';
      actions.append(act(label, a.archivedChapters ? 'secondary' : 'primary', async () => {
        const r = await api('/api/jm/archive', { jmIds: [jm] });
        toast(r.queued ? '已加入入库队列，进度见“任务”' : '已经入库了');
      }));
    }
    if (!a.fav) {
      actions.append(act('收藏', 'secondary', async () => {
        const r = await api('/api/jm/favorite', { jmIds: [jm] });
        toast(NEXT_TEXT[r.next] || '已收藏');
      }));
    } else if (a.inLibrary) {
      actions.append(el('button', { class: 'secondary', onclick: () => go(`#/a/${jm}`) }, '在书架中打开，管理收藏夹'));
    }

    const stateText = { archived: '已入库', archiving: '入库中', partial: `部分入库（${a.archivedChapters}/${a.chapters.length} 章）` }[a.state] || '未入库';
    const pdfText = { done: '已生成', running: '生成中' }[a.pdf] || (a.fav ? '等入库完成后生成' : '收藏后自动生成');
    app.replaceChildren(el('div', { class: 'album' },
      el('div', { class: 'album-head' },
        el('div', { class: 'cover' }, el('img', { src: jmCoverUrl(jm), alt: '' })),
        el('div', {},
          el('h2', {}, a.title),
          el('p', { class: 'muted' }, `JM${jm}`),
          a.authors.length ? el('p', { class: 'muted' }, a.authors.join('、')) : null,
          a.works.length ? el('p', { class: 'muted' }, a.works.join('、')) : null,
          el('p', { class: 'muted' }, `${a.chapters.length} 章 · ${a.pageCount || '-'} 页`))),
      a.tags.length ? el('p', { class: 'muted small' }, a.tags.join(' · ')) : null,
      actions,
      el('p', { class: 'muted small' }, `状态：${stateText}　收藏：${a.fav ? a.folders.join('、') : '未收藏'}　PDF：${pdfText}`),
      multi ? el('h3', {}, `章节（${a.chapters.length}）`) : null,
      multi ? el('ol', { class: 'chapters' }, a.chapters.slice(0, 200).map((c) =>
        el('li', {}, el('div', { class: 'chapter-row' },
          el('span', { class: 'name' }, `第 ${c.sort} 章  ${c.title}`))))) : null));

    // 入库或生成 PDF 进行中时，每 5 秒刷新一次状态
    if (a.state === 'archiving' || a.pdf === 'running') {
      const timer = setInterval(async () => {
        try {
          const b = await api(`/api/jm/album/${jm}`);
          if (b.state !== a.state || b.pdf !== a.pdf || b.archivedChapters !== a.archivedChapters) render();
        } catch (e) { /* 下次再试 */ }
      }, 5000);
      S.cleanup = () => clearInterval(timer);
    }
  }

  // ---- 任务 ----
  const STATUS_TEXT = { queued: '排队中', running: '进行中', done: '完成', failed: '失败' };
  const KIND_TEXT = { archive: '入库', favorite: '收藏 PDF' };

  async function renderJobs() {
    S.tab = 'jobs';
    const head = el('p', { class: 'muted small jobs-head' });
    const list = el('ol', { class: 'chapters jobs' });
    app.replaceChildren(tabsBar('jobs'), head, list);
    const draw = (d) => {
      head.textContent = d.running || d.queued ? `进行中 ${d.running} 个，排队 ${d.queued} 个，每 3 秒自动刷新` : '当前没有进行中的任务';
      if (!d.items.length) {
        list.replaceChildren(el('li', {}, el('p', { class: 'notice' }, '还没有任务。')));
        return;
      }
      list.replaceChildren(...d.items.map((j) => {
        const status = j.status === 'queued' && j.retryAt ? '等待重试' : (STATUS_TEXT[j.status] || j.status);
        const time = new Date(j.updatedAt * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
        const detail = j.status === 'failed' ? j.error : j.status === 'done' ? '' : j.step;
        return el('li', {}, el('a', { class: 'job', href: `#/jm/${j.jmId}` },
          el('div', { class: 'grow' },
            el('div', { class: 'name' }, `${KIND_TEXT[j.kind] || j.kind} · JM${j.jmId}`),
            j.title ? el('div', { class: 'muted small clip' }, j.title) : null,
            detail ? el('div', { class: j.status === 'failed' ? 'small err' : 'small muted' }, detail) : null),
          el('div', { class: 'job-side' }, el('span', { class: `pill ${j.status}` }, status), el('span', { class: 'muted small' }, time))));
      }));
    };
    draw(await api('/api/jobs'));
    const timer = setInterval(async () => {
      try { draw(await api('/api/jobs')); } catch (e) { /* 下次再试 */ }
    }, 3000);
    S.cleanup = () => clearInterval(timer);
  }

  // ---- 阅读 ----
  async function renderReader(photo, page) {
    const c = await api(`/api/chapter/${photo}`);
    S.parent = `#/a/${c.jmId}`;
    document.body.classList.add('reading');
    themeDark(true);

    const total = c.pages.length;
    let current = Math.min(Math.max(page || c.page || 1, 1), total);
    let saved = 0;
    let saveTimer = null;
    const mode = S.user.readMode === 'page' ? 'page' : 'scroll';

    const root = el('div', { class: mode === 'page' ? 'reader page-mode hud-hidden' : 'reader hud-hidden' });
    const counter = el('span', {}, `${current} / ${total}`);
    const top = el('div', { class: 'hud top' },
      el('span', { class: 'title' }, c.chapterCount > 1 ? `第 ${c.sort} 章  ${c.title}` : c.albumTitle), counter);
    const bottom = el('div', { class: 'hud bottom' },
      el('button', { disabled: !c.prev, onclick: () => go(`#/r/${c.prev}`) }, '上一章'),
      el('button', { onclick: () => switchMode() }, mode === 'scroll' ? '切换为翻页' : '切换为滑动'),
      el('button', { disabled: !c.next, onclick: () => go(`#/r/${c.next}`) }, '下一章'));
    const toggleHud = () => root.classList.toggle('hud-hidden');

    const save = (keepalive = false) => {
      if (current === saved) return;
      saved = current;
      fetch('/api/progress', {
        method: 'POST', keepalive,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${S.token}` },
        body: JSON.stringify({ photoId: photo, page: current }),
      }).catch(() => {});
    };
    const setCurrent = (n) => {
      if (n === current && saved) return;
      current = n;
      counter.textContent = `${n} / ${total}`;
      history.replaceState(null, '', `#/r/${photo}/${n}`);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(save, 1200);
    };
    const onHide = () => { if (document.visibilityState === 'hidden') save(true); };
    document.addEventListener('visibilitychange', onHide);

    const switchMode = () => {
      S.user.readMode = mode === 'scroll' ? 'page' : 'scroll';
      api('/api/settings', { readMode: S.user.readMode }).catch(() => {});
      render();
    };

    const loadInto = (img, n) => {
      img.onerror = () => {
        if (img.dataset.retry) return;
        img.dataset.retry = '1';
        setTimeout(() => { img.src = `${imgUrl(photo, n)}&r=1`; }, 1500);
      };
      img.src = imgUrl(photo, n);
    };

    let teardown = () => {};
    if (mode === 'scroll') {
      const list = el('div', { class: 'pages' }, c.pages.map(([w, h], i) =>
        el('div', { class: 'pg', 'data-n': i + 1, style: `aspect-ratio: ${w} / ${h}` }, el('img', { alt: '', decoding: 'async' }))));
      const end = el('div', { class: 'end' },
        el('div', {}, c.next ? '本章完' : '全部读完了'),
        c.next
          ? el('button', { class: 'primary', onclick: () => go(`#/r/${c.next}`) }, '下一章')
          : el('button', { class: 'secondary', onclick: () => go(S.parent) }, '返回作品'));
      root.append(top, list, end, bottom);
      root.addEventListener('click', (e) => { if (!e.target.closest('button')) toggleHud(); });
      app.replaceChildren(root);

      const io = new IntersectionObserver((entries) => entries.forEach((e) => {
        if (e.isIntersecting) {
          loadInto(e.target.querySelector('img'), Number(e.target.dataset.n));
          io.unobserve(e.target);
        }
      }), { rootMargin: '200% 0px' });
      list.querySelectorAll('.pg').forEach((p) => io.observe(p));

      let ticking = false;
      const onScroll = () => {
        if (ticking) return;
        ticking = true;
        requestAnimationFrame(() => {
          ticking = false;
          const hit = document.elementFromPoint(innerWidth / 2, innerHeight * 0.35);
          const pg = hit && hit.closest && hit.closest('.pg');
          if (pg) setCurrent(Number(pg.dataset.n));
        });
      };
      if (current > 1) list.children[current - 1].scrollIntoView({ block: 'start' });
      window.addEventListener('scroll', onScroll, { passive: true });
      teardown = () => { io.disconnect(); window.removeEventListener('scroll', onScroll); };
    } else {
      const img = el('img', { alt: '' });
      const stage = el('div', { class: 'stage' }, img);
      root.append(stage, top, bottom);
      app.replaceChildren(root);

      const preloaded = new Set();
      const show = (n) => {
        n = Math.min(Math.max(n, 1), total);
        delete img.dataset.retry;
        loadInto(img, n);
        setCurrent(n);
        for (let k = n + 1; k <= Math.min(total, n + 3); k++) {
          if (!preloaded.has(k)) { preloaded.add(k); new Image().src = imgUrl(photo, k); }
        }
      };
      const next = () => {
        if (current < total) show(current + 1);
        else if (c.next) go(`#/r/${c.next}`);
        else toast('全部读完了');
      };
      const prev = () => {
        if (current > 1) show(current - 1);
        else toast(c.prev ? '已经是本章第一页，可以点“上一章”' : '已经是第一页');
      };

      let startX = 0;
      let startY = 0;
      let swipedAt = 0;
      stage.addEventListener('touchstart', (e) => { startX = e.touches[0].clientX; startY = e.touches[0].clientY; }, { passive: true });
      stage.addEventListener('touchend', (e) => {
        const dx = e.changedTouches[0].clientX - startX;
        const dy = e.changedTouches[0].clientY - startY;
        if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
          swipedAt = Date.now();
          dx < 0 ? next() : prev();
        }
      }, { passive: true });
      stage.addEventListener('click', (e) => {
        if (Date.now() - swipedAt < 400) return;
        const x = e.clientX / innerWidth;
        if (x < 0.3) prev();
        else if (x > 0.7) next();
        else toggleHud();
      });
      const onKey = (e) => {
        if (e.key === 'ArrowRight' || e.key === ' ') next();
        else if (e.key === 'ArrowLeft') prev();
      };
      document.addEventListener('keydown', onKey);
      teardown = () => document.removeEventListener('keydown', onKey);
      show(current);
    }

    S.cleanup = () => {
      teardown();
      clearTimeout(saveTimer);
      save(true);
      document.removeEventListener('visibilitychange', onHide);
    };
  }

  // ---- 启动 ----
  async function boot() {
    if (!tg || !tg.initData) {
      notice('请在 Telegram 里通过 Bot 打开这个页面。');
      return;
    }
    tg.ready();
    tg.expand();
    try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) { /* 旧版客户端 */ }
    if (tg.BackButton) tg.BackButton.onClick(() => go(S.parent || '#/'));

    let d;
    try {
      d = await login();
    } catch (e) {
      notice(e.message);
      return;
    }
    // Telegram 把启动参数放在 #tgWebAppData=... 里，这时的 hash 不是本页的路由，要换掉
    if (!/^#\/./.test(location.hash)) {
      const params = new URLSearchParams(location.search);
      const sp = d.startParam || '';
      const chapter = /^c(\d+)$/.exec(sp);
      const album = /^a(\d+)$/.exec(sp);
      const jm = (album && album[1]) || params.get('read') || params.get('a') || '';
      let target = '#/';
      if (chapter) target = `#/r/${chapter[1]}`;
      else if (/^\d+$/.test(jm)) target = await continueTarget(Number(jm));
      history.replaceState(null, '', location.pathname + location.search + target);
    }
    window.addEventListener('hashchange', render);
    render();
  }

  boot();
})();
