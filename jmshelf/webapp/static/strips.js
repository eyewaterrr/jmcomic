// 和 jmcomic JmImageTool.decode_and_save 相同的还原：原图从下往上切成 num 条，依次从上往下摆。
// 返回 [[源 y, 目标 y, 高度], ...]；num 为 0 表示没打乱。
function strips(h, num) {
  if (!num) return [[0, 0, h]];
  const move = Math.floor(h / num), over = h % num, out = [];
  for (let i = 0; i < num; i++) {
    let height = move, sy = h - move * (i + 1) - over, dy = move * i;
    if (i === 0) height += over; else dy += over;
    out.push([sy, dy, height]);
  }
  return out;
}
if (typeof module !== 'undefined') module.exports = { strips };
