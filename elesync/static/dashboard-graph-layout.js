/* Force layout for the knowledge graph. Pure computation, no DOM, so it can be run
   and timed in Node. Repulsion uses a Barnes-Hut quadtree (O(n log n)) with a
   1/distance falloff, springs run along connections, and a soft gravity keeps the
   cloud together. Positions are written straight onto the node objects.

   A node is { x, y, vx, vy, charge, mass, pinned }. An edge is { a, b, kind },
   where a and b are node objects that also carry their position in `index`. */
const GRAPH_REPEL_BASE = 30;
const GRAPH_THETA_SQUARED = 0.81;
const GRAPH_ALPHA_MIN = 0.002;
const GRAPH_MAX_SPEED = 40;
const GRAPH_MAX_DEPTH = 40;

function createGraphLayout(nodes, edges, params) {
  const count = nodes.length;
  const degree = new Float64Array(count);
  edges.forEach(e => { degree[e.a.index]++; degree[e.b.index]++; });
  const bias = edges.map(e => degree[e.a.index] / (degree[e.a.index] + degree[e.b.index]));
  const pull = edges.map(e => 1 / Math.min(degree[e.a.index], degree[e.b.index]));
  const spread = edges.map(e => 2 * Math.sqrt(Math.max(degree[e.a.index], degree[e.b.index])));

  /* Quadtree pools, reused every tick so a tick allocates nothing. */
  const capacity = count * 8 + 64;
  const cx = new Float64Array(capacity), cy = new Float64Array(capacity), half = new Float64Array(capacity);
  const mass = new Float64Array(capacity), sumX = new Float64Array(capacity), sumY = new Float64Array(capacity);
  const leaf = new Int32Array(capacity), kids = new Int32Array(capacity * 4);
  const stack = new Int32Array(512);
  let used = 0, alpha = 1;
  const decay = count > 5000 ? 0.95 : 0.9772;

  function newCell(x, y, h) {
    const cell = used++;
    cx[cell] = x; cy[cell] = y; half[cell] = h;
    mass[cell] = sumX[cell] = sumY[cell] = 0;
    leaf[cell] = -1;
    kids.fill(-1, cell * 4, cell * 4 + 4);
    return cell;
  }
  function descend(cell, i, depth) {
    const n = nodes[i], quadrant = (n.x > cx[cell] ? 1 : 0) + (n.y > cy[cell] ? 2 : 0);
    let child = kids[cell * 4 + quadrant];
    if (child < 0) {
      const h = half[cell] / 2;
      child = newCell(cx[cell] + (quadrant & 1 ? h : -h), cy[cell] + (quadrant & 2 ? h : -h), h);
      kids[cell * 4 + quadrant] = child;
    }
    insert(child, i, depth + 1);
  }
  function insert(cell, i, depth) {
    const n = nodes[i], c = n.charge;
    mass[cell] += c; sumX[cell] += n.x * c; sumY[cell] += n.y * c;
    if (leaf[cell] === -1) { leaf[cell] = i; return; }
    if (leaf[cell] >= 0) {
      // Coincident points or a full pool stay aggregated in one cell rather than splitting forever.
      if (depth >= GRAPH_MAX_DEPTH || used + 8 > capacity) return;
      const other = leaf[cell];
      leaf[cell] = -2;
      descend(cell, other, depth);
    }
    descend(cell, i, depth);
  }
  function build() {
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (let i = 0; i < count; i++) {
      const n = nodes[i];
      if (n.x < minX) minX = n.x; if (n.x > maxX) maxX = n.x;
      if (n.y < minY) minY = n.y; if (n.y > maxY) maxY = n.y;
    }
    used = 0;
    newCell((minX + maxX) / 2, (minY + maxY) / 2, Math.max(maxX - minX, maxY - minY) / 2 + 1);
    for (let i = 0; i < count; i++) insert(0, i, 0);
  }
  function repel(i, strength) {
    const n = nodes[i];
    let sp = 0, fx = 0, fy = 0;
    stack[sp++] = 0;
    while (sp > 0) {
      const cell = stack[--sp], m = mass[cell];
      if (m === 0) continue;
      let dx = n.x - sumX[cell] / m, dy = n.y - sumY[cell] / m, d2 = dx * dx + dy * dy;
      const owner = leaf[cell];
      if (owner === i) continue;
      if (owner < 0 && half[cell] * half[cell] * 4 >= GRAPH_THETA_SQUARED * d2) {
        for (let q = 0; q < 4; q++) if (kids[cell * 4 + q] >= 0) stack[sp++] = kids[cell * 4 + q];
        continue;
      }
      if (d2 < 1) { dx = (i % 2 ? 0.5 : -0.5); dy = 0.5; d2 = 1; }
      const w = strength * m / d2;
      fx += dx * w; fy += dy * w;
    }
    n.vx += fx / n.mass; n.vy += fy / n.mass;
  }

  function tick() {
    if (alpha < GRAPH_ALPHA_MIN) return false;
    build();
    const strength = GRAPH_REPEL_BASE * params.repel * alpha, aspect = params.aspect || 1;
    for (let i = 0; i < count; i++) repel(i, strength);
    for (let k = 0; k < edges.length; k++) {
      const e = edges[k], a = e.a, b = e.b;
      const dx = b.x + b.vx - a.x - a.vx, dy = b.y + b.vy - a.y - a.vy;
      const d = Math.hypot(dx, dy) || 1e-6;
      const rest = (params.distance + spread[k]) * (e.kind === 'link' ? 0.8 : 1);
      const f = (d - rest) / d * alpha * pull[k] * params.link;
      b.vx -= dx * f * bias[k]; b.vy -= dy * f * bias[k];
      a.vx += dx * f * (1 - bias[k]); a.vy += dy * f * (1 - bias[k]);
    }
    for (let i = 0; i < count; i++) {
      const n = nodes[i], gravity = params.center * (degree[i] ? 0.02 : 0.06) * alpha;
      // `aspect` > 1 lets the cloud stretch sideways, so a wide banner is filled instead of a round blob.
      n.vx -= n.x * gravity / aspect; n.vy -= n.y * gravity * aspect;
      n.vx *= 0.6; n.vy *= 0.6;
      if (n.pinned) { n.vx = n.vy = 0; continue; }
      const speed = Math.hypot(n.vx, n.vy);
      if (speed > GRAPH_MAX_SPEED) { n.vx *= GRAPH_MAX_SPEED / speed; n.vy *= GRAPH_MAX_SPEED / speed; }
      n.x += n.vx; n.y += n.vy;
    }
    alpha *= decay;
    return true;
  }
  return {
    tick,
    reheat(level) { alpha = Math.max(alpha, level); },
    get alpha() { return alpha; },
    get settled() { return alpha < GRAPH_ALPHA_MIN; },
  };
}
