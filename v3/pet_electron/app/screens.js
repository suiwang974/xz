// 多屏：桌宠窗口覆盖所有显示器工作区并集
function unionBounds(displays) {
  const ws = (displays || []).map((d) => d && d.workArea).filter((w) => w && w.width > 0 && w.height > 0);
  if (!ws.length) return null;
  const x1 = Math.min(...ws.map((w) => w.x)), y1 = Math.min(...ws.map((w) => w.y));
  const x2 = Math.max(...ws.map((w) => w.x + w.width)), y2 = Math.max(...ws.map((w) => w.y + w.height));
  return { x: x1, y: y1, width: x2 - x1, height: y2 - y1 };
}
function displayAt(displays, pt) {
  const hit = (displays || []).find((d) => { const b = d.bounds || d.workArea; return pt.x >= b.x && pt.x < b.x + b.width && pt.y >= b.y && pt.y < b.y + b.height; });
  return hit || (displays || [])[0] || null;
}
module.exports = { unionBounds, displayAt };
