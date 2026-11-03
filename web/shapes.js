// Tidy pen strokes like Apple Notes and Samsung Notes do.
//   smoothStroke: takes the shake out of a stroke as it is finished (a light weighted average).
//   recognizeShape: hold the pen still at the end of a stroke and a rough line, circle, ellipse,
//   triangle or rectangle snaps to a clean one.
// Both work on [x, y, pressure] points in page units and return new points (the input is untouched).

const dist = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1]);

// Two passes of a [1, 2, 1] average on x and y; the ends stay put so strokes still join up.
export function smoothStroke(pts, passes = 2) {
  if (pts.length < 5) return pts.map(p => [...p]);
  let cur = pts.map(p => [...p]);
  for (let k = 0; k < passes; k++) {
    const nxt = cur.map(p => [...p]);
    for (let i = 1; i < cur.length - 1; i++) {
      nxt[i][0] = (cur[i - 1][0] + 2 * cur[i][0] + cur[i + 1][0]) / 4;
      nxt[i][1] = (cur[i - 1][1] + 2 * cur[i][1] + cur[i + 1][1]) / 4;
    }
    cur = nxt;
  }
  return cur;
}

const pathLength = pts => pts.reduce((n, p, i) => i ? n + dist(p, pts[i - 1]) : 0, 0);

function bbox(pts) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const [x, y] of pts) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x); y1 = Math.max(y1, y); }
  return { x0, y0, x1, y1, w: x1 - x0, h: y1 - y0 };
}

function segDist(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], L = dx * dx + dy * dy;
  const t = L ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L)) : 0;
  return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
}

// Ramer-Douglas-Peucker: the corners of a polyline.
function rdp(pts, eps) {
  if (pts.length < 3) return pts.slice();
  let k = 0, d = 0;
  for (let i = 1; i < pts.length - 1; i++) { const di = segDist(pts[i], pts[0], pts.at(-1)); if (di > d) { d = di; k = i; } }
  if (d <= eps) return [pts[0], pts.at(-1)];
  return [...rdp(pts.slice(0, k + 1), eps).slice(0, -1), ...rdp(pts.slice(k), eps)];
}

// Evenly spaced points along straight segments (closed: back to the start).
function sampleSegments(corners, closed, p, step = 4) {
  const out = [], ring = closed ? [...corners, corners[0]] : corners;
  for (let i = 1; i < ring.length; i++) {
    const a = ring[i - 1], b = ring[i], n = Math.max(1, Math.round(dist(a, b) / step));
    for (let j = i === 1 ? 0 : 1; j <= n; j++) out.push([a[0] + (b[0] - a[0]) * j / n, a[1] + (b[1] - a[1]) * j / n, p]);
  }
  return out;
}

function sampleEllipse(cx, cy, rx, ry, start, p) {
  const n = Math.max(24, Math.round(Math.PI * (rx + ry) / 4)), out = [];
  for (let i = 0; i <= n; i++) { const t = start + 2 * Math.PI * i / n; out.push([cx + rx * Math.cos(t), cy + ry * Math.sin(t), p]); }
  return out;
}

const median = xs => { const s = xs.slice().sort((a, b) => a - b); return s[Math.floor(s.length / 2)]; };

// { kind, pts } for a recognisable shape, or null (letters and squiggles stay as written).
export function recognizeShape(pts) {
  if (pts.length < 6) return null;
  const L = pathLength(pts), b = bbox(pts), D = Math.hypot(b.w, b.h);
  if (L < 40 || D < 36) return null;                 // small: letters like o and l stay as written
  const p = median(pts.map(q => q[2] ?? 0.5));
  const gap = dist(pts[0], pts.at(-1));
  const closed = gap < Math.max(18, 0.22 * D) && L > 2.2 * D * 0.9;

  if (!closed) {
    const a = pts[0], z = pts.at(-1);
    const off = Math.max(...pts.map(q => segDist(q, a, z)));
    if (off < Math.max(4, 0.07 * dist(a, z)) && dist(a, z) > 0.85 * L * 0.9) {
      // nearly horizontal or vertical lines snap to exact
      let e = [z[0], z[1]];
      const ang = Math.abs(Math.atan2(z[1] - a[1], z[0] - a[0]) * 180 / Math.PI);
      if (ang < 6 || ang > 174) e = [z[0], a[1]];
      else if (Math.abs(ang - 90) < 6) e = [a[0], z[1]];
      return { kind: 'line', pts: sampleSegments([[a[0], a[1]], e], false, p) };
    }
    const corners = rdp(pts, Math.max(5, 0.06 * D));
    if (corners.length >= 3 && corners.length <= 4) {
      const err = pts.reduce((n, q) => n + Math.min(...corners.slice(1).map((c, i) => segDist(q, corners[i], c))), 0) / pts.length;
      if (err < 0.03 * D) return { kind: 'polyline', pts: sampleSegments(corners.map(c => [c[0], c[1]]), false, p) };
    }
    return null;
  }

  // closed: ellipse or polygon, whichever fits better
  const cx = (b.x0 + b.x1) / 2, cy = (b.y0 + b.y1) / 2, rx = b.w / 2, ry = b.h / 2;
  if (rx < 6 || ry < 6) return null;
  const ellErr = pts.reduce((n, [x, y]) => n + Math.abs(Math.hypot((x - cx) / rx, (y - cy) / ry) - 1), 0) / pts.length;
  let corners = rdp(pts, Math.max(6, 0.09 * D));
  if (corners.length > 2 && dist(corners[0], corners.at(-1)) < Math.max(18, 0.22 * D)) corners = corners.slice(0, -1);
  if (corners.length >= 3 && corners.length <= 4) {
    // join the start and end where the pen began, then measure the fit
    const ring = [...corners, corners[0]];
    const polyErr = pts.reduce((n, q) => n + Math.min(...ring.slice(1).map((c, i) => segDist(q, ring[i], c))), 0) / pts.length / D;
    if (polyErr < 0.035 && polyErr * 1.6 < ellErr) {
      if (corners.length === 4) {
        // a rectangle when its sides run along the page, else a clean quadrilateral
        const axisy = corners.every((c, i) => { const n = corners[(i + 1) % 4]; const a = Math.abs(Math.atan2(n[1] - c[1], n[0] - c[0])) * 180 / Math.PI; return Math.min(a % 90, 90 - a % 90) < 14; });
        if (axisy) return { kind: 'rectangle', pts: sampleSegments([[b.x0, b.y0], [b.x1, b.y0], [b.x1, b.y1], [b.x0, b.y1]], true, p) };
        return { kind: 'quadrilateral', pts: sampleSegments(corners.map(c => [c[0], c[1]]), true, p) };
      }
      return { kind: 'triangle', pts: sampleSegments(corners.map(c => [c[0], c[1]]), true, p) };
    }
  }
  if (ellErr < 0.14) {
    const round = Math.abs(rx - ry) < 0.18 * Math.max(rx, ry);
    const r = (rx + ry) / 2;
    const start = Math.atan2(pts[0][1] - cy, pts[0][0] - cx);
    return round ? { kind: 'circle', pts: sampleEllipse(cx, cy, r, r, start, p) }
      : { kind: 'ellipse', pts: sampleEllipse(cx, cy, rx, ry, start, p) };
  }
  return null;
}
