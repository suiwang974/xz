// 速度曲线（canvas，跟随主题色）
export function drawSpeed(cv, hist) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, hgt = cv.clientHeight;
  if (!w || !hgt) return;
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = hgt * dpr; }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, hgt);
  const cs = getComputedStyle(document.documentElement);
  const acc = cs.getPropertyValue('--acc').trim() || '#3E6FD0';
  const acc2 = cs.getPropertyValue('--acc2').trim() || '#38C6D9';
  const line = cs.getPropertyValue('--line2').trim() || '#eee';
  const pts = hist.slice(-90);
  g.strokeStyle = line; g.lineWidth = 1;
  for (let i = 1; i < 3; i++) { const y = Math.round((hgt * i) / 3) + 0.5; g.beginPath(); g.moveTo(0, y); g.lineTo(w, y); g.stroke(); }
  if (pts.length < 2) return;
  const max = Math.max(1024 * 64, ...pts.map((p) => Math.max(p.down, p.up))) * 1.15;
  const X = (i) => (w * i) / (pts.length - 1), Y = (v) => hgt - 2 - (hgt - 4) * (v / max);
  const grad = g.createLinearGradient(0, 0, 0, hgt);
  grad.addColorStop(0, acc + '55'); grad.addColorStop(1, acc + '00');
  g.beginPath(); g.moveTo(0, hgt);
  pts.forEach((p, i) => g.lineTo(X(i), Y(p.down)));
  g.lineTo(w, hgt); g.closePath(); g.fillStyle = grad; g.fill();
  const lg = g.createLinearGradient(0, 0, w, 0); lg.addColorStop(0, acc); lg.addColorStop(1, acc2);
  g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(X(i), Y(p.down)) : g.moveTo(X(i), Y(p.down))));
  g.strokeStyle = lg; g.lineWidth = 1.8; g.lineJoin = 'round'; g.stroke();
  if (pts.some((p) => p.up > 0)) {
    g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(X(i), Y(p.up)) : g.moveTo(X(i), Y(p.up))));
    g.strokeStyle = cs.getPropertyValue('--seed').trim() || '#7B61D9'; g.lineWidth = 1.2; g.setLineDash([3, 3]); g.stroke(); g.setLineDash([]);
  }
}
