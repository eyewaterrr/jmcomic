'use strict';
// 书架 Mini App：#/ 书架，#/a/<JM号> 作品，#/r/<章节ID>/<页码> 阅读
(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  const app = document.getElementById('app');
  const toastEl = document.getElementById('toast');
  const S = { token: '', user: null, cleanup: null, parent: null };

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

  let toastTimer = null;
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.hidden = true; }, 2600);
  }

  const notice = (text) => app.replaceChildren(el('p', { class: 'notice' }, text));
  const go = (hash) => { location.hash = hash; };
  const imgUrl = (photo, n) => `/img/${photo}/${n}?t=${encodeURIComponent(S.token)}`;
  const coverUrl = (jm) => `/cover/${jm}?t=${encodeURIComponent(S.token)}`;

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
    document.body.classList.remove('reading');
    themeDark(false);
    const route = parse();
    window.scrollTo(0, 0);
    try {
      if (route.view === 'album') await renderAlbum(route.jm);
      else if (route.view === 'reader') await renderReader(route.photo, route.page);
      else await renderLibrary();
    } catch (e) {
      notice(e.message || String(e));
    }
    if (tg.BackButton) route.view === 'library' ? tg.BackButton.hide() : tg.BackButton.show();
  }

  // ---- 书架 ----
  async function renderLibrary() {
    S.parent = null;
    const { items } = await api('/api/library');
    const head = el('div', { class: 'bar' }, el('h1', {}, '书架'), el('span', { class: 'muted' }, `${items.length} 本`));
    if (!items.length) {
      app.replaceChildren(head, el('p', { class: 'notice' }, '书架是空的。在 Bot 里发送 JM 号或关键词，点“入库”后就会出现在这里。'));
      return;
    }
    app.replaceChildren(head, el('div', { class: 'grid' }, items.map(card)));
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

    const favBtn = el('button', {
      class: 'secondary', disabled: a.fav || a.favPending || a.chapters.length < a.chapterCount,
      onclick: async () => {
        favBtn.disabled = true;
        try {
          const r = await api('/api/favorite', { jmId: jm });
          toast(r.status === 'done' ? '已经收藏过了' : '已加入收藏队列，进度会显示在 Bot 私聊里');
          favBtn.textContent = '正在收藏…';
        } catch (e) {
          toast(e.message);
          favBtn.disabled = false;
        }
      },
    }, a.fav ? '已收藏为 PDF' : a.favPending ? '正在收藏…' : '收藏为 PDF');

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
      el('h3', {}, missing > 0 ? `章节（已入库 ${a.chapters.length}/${a.chapterCount}）` : `章节（${a.chapters.length}）`),
      list,
      missing > 0 ? el('p', { class: 'muted' }, `还有 ${missing} 章没有入库，可以在 Bot 里点“入库”补齐。`) : null));
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
    if (!location.hash || location.hash === '#' || location.hash === '#/') {
      const sp = d.startParam || '';
      const q = new URLSearchParams(location.search).get('a') || '';
      let target = '';
      if (/^c\d+$/.test(sp)) target = `#/r/${sp.slice(1)}`;
      else if (/^a\d+$/.test(sp)) target = `#/a/${sp.slice(1)}`;
      else if (/^\d+$/.test(q)) target = `#/a/${q}`;
      if (target) history.replaceState(null, '', target);
    }
    window.addEventListener('hashchange', render);
    render();
  }

  boot();
})();
