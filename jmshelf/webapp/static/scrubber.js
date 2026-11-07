'use strict';
// 阅读器右侧的进度条：拖动或点轨道跳页。
// 拖动时只在旁边显示“第几页 / 共几页”，松手才跳：边拖边跳会让划过的每一页都去下载，白费流量。
// 平时隐藏，滚动时和工具栏打开时出现；隐藏时不拦截触摸，不影响正常滑动。
(function () {
  function makeScrubber({ total, onSeek }) {
    const root = document.createElement('div');
    root.className = 'scrub';
    const track = document.createElement('div');
    track.className = 'track';
    const thumb = document.createElement('div');
    thumb.className = 'thumb';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    track.append(thumb);
    root.append(track, bubble);

    let current = 1;
    let target = 1;
    let dragging = false;
    let pinned = false;
    let hideTimer = null;

    const fraction = (n) => (total > 1 ? (n - 1) / (total - 1) : 0);
    const place = (n) => {
      const top = `${fraction(n) * 100}%`;
      thumb.style.top = top;
      bubble.style.top = top;
    };
    const pageAt = (clientY) => {
      const r = track.getBoundingClientRect();
      const f = Math.min(Math.max((clientY - r.top) / Math.max(r.height, 1), 0), 1);
      return Math.round(f * (total - 1)) + 1;
    };
    const scheduleHide = () => {
      clearTimeout(hideTimer);
      if (!pinned && !dragging) hideTimer = setTimeout(() => root.classList.remove('on'), 1800);
    };
    const show = () => {
      if (total < 2) return;
      root.classList.add('on');
      scheduleHide();
    };

    root.addEventListener('pointerdown', (e) => {
      if (!root.classList.contains('on')) return;
      e.preventDefault();
      e.stopPropagation();
      dragging = true;
      root.classList.add('dragging');
      try { root.setPointerCapture(e.pointerId); } catch (err) { /* 旧版 WebView */ }
      target = pageAt(e.clientY);
      place(target);
      bubble.textContent = `${target} / ${total}`;
      clearTimeout(hideTimer);
    });
    root.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      e.preventDefault();
      target = pageAt(e.clientY);
      place(target);
      bubble.textContent = `${target} / ${total}`;
    });
    const finish = (e) => {
      if (!dragging) return;
      dragging = false;
      root.classList.remove('dragging');
      try { root.releasePointerCapture(e.pointerId); } catch (err) { /* 已释放 */ }
      if (target !== current) onSeek(target);
      else place(current);
      scheduleHide();
    };
    root.addEventListener('pointerup', finish);
    root.addEventListener('pointercancel', finish);
    // 阅读器的点击会切换工具栏、翻页，进度条上的点击不能传下去
    root.addEventListener('click', (e) => e.stopPropagation());

    return {
      el: root,
      set(n) {
        current = n;
        if (!dragging) place(n);
      },
      show,
      pin(on) {
        pinned = on;
        if (on) { clearTimeout(hideTimer); if (total > 1) root.classList.add('on'); } else scheduleHide();
      },
    };
  }
  window.makeScrubber = makeScrubber;
})();
