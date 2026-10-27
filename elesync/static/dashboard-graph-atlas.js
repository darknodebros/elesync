/* Atlas view: a calm map of where your memories come from.
   One glowing orb per source (or per project), laid out on a ring around a golden cloud of every
   memory. Thin spokes join each orb to the centre, small satellites circle each orb, and clicking an
   orb flies in to show its newest memories fanned out around it. It shows structure at a glance, so it
   stays readable however many memories there are. Shared helpers live in dashboard-graph.js. */
const ATLAS_RING = 300;            // world radius of the orb ring
const ATLAS_MAX_ORBS = 12;         // the rest are grouped into one "Everything else" orb
const ATLAS_DUST_LIMIT = 2200;     // memories drawn in the central cloud
const ATLAS_FAN_LIMIT = 150;       // memories shown around a focused orb
const ATLAS_DUST_COLORS = ['#ffd98a', '#ffc766', '#fff1c9', '#f2b84b'];
const ATLAS_OTHER_COLOR = '#b9b2a3';

function createAtlasScene(canvas, panel, data) {
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('This browser does not support the graph canvas');
  const reduced = graphReducedMotion();
  const bySource = graphSettings.colorBy === 'source';

  /* ── Model: group memories into orbs ── */
  const projectNames = new Map(data.nodes.filter(n => n.kind === 'project').map(n => [n.id.replace(/^project:/, ''), n.label]));
  const memories = data.nodes.filter(n => n.kind === 'memory');
  const buckets = new Map();
  memories.forEach(m => {
    const key = bySource ? String(m.source || 'manual').toLowerCase() : ((m.project_ids || [])[0] || '');
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(m);
  });
  let ranked = [...buckets.entries()].sort((a, b) => b[1].length - a[1].length);
  if (ranked.length > ATLAS_MAX_ORBS) {
    const rest = ranked.slice(ATLAS_MAX_ORBS - 1).flatMap(([, list]) => list);
    ranked = ranked.slice(0, ATLAS_MAX_ORBS - 1).concat([['__other', rest]]);
  }
  const nameOf = key => key === '__other' ? 'Everything else'
    : bySource ? graphSourceLabel(key) : key === '' ? 'No project' : (projectNames.get(key) || 'Project');
  const colorOf = (key, rank) => key === '__other' ? ATLAS_OTHER_COLOR : key === '' && !bySource ? GRAPH_UNLINKED_COLOR
    : bySource ? (GRAPH_SOURCE_COLORS[key] || graphColorAt(rank + GRAPH_PALETTE.length)) : graphColorAt(rank);
  const biggest = Math.max(1, ...ranked.map(([, list]) => list.length));
  const orbs = ranked.map(([key, list], i) => {
    const angle = -Math.PI / 2 + (i * 2 * Math.PI) / ranked.length;
    const reach = ATLAS_RING * (0.92 + 0.16 * graphUnit('atlas' + key));
    return {
      id: 'orb:' + key, kind: 'orb', key, index: i, label: nameOf(key), color: colorOf(key, i), memories: list, count: list.length,
      angle, bx: Math.cos(angle) * reach, by: Math.sin(angle) * reach, x: 0, y: 0, sx: 0, sy: 0,
      radius: 12 + 20 * Math.sqrt(list.length / biggest), hidden: false, phase: graphUnit('p' + key) * 6.283,
      satellites: Array.from({ length: Math.min(14, 3 + Math.round(Math.sqrt(list.length))) }, (_, s) => ({
        phase: graphUnit(key + s) * 6.283, speed: (0.12 + graphUnit('s' + key + s) * 0.3) * (s % 2 ? 1 : -1), reach: 1.7 + graphUnit('r' + key + s) * 1.1,
      })),
    };
  });
  const orbOf = new Map();
  orbs.forEach(o => o.memories.forEach(m => orbOf.set(m.id, o)));
  const groups = orbs.map(o => ({ key: o.key, count: o.count, color: o.color, label: o.label }));
  const hiddenGroups = new Set();

  /* Central cloud: every memory (sampled when there are very many) as a gold speck near the middle. */
  const dust = [];
  {
    const random = graphRandom(9), step = Math.max(1, Math.ceil(memories.length / ATLAS_DUST_LIMIT));
    for (let i = 0; i < memories.length; i += step) {
      const u = Math.max(1e-6, random()), v = random(), spread = Math.sqrt(-2 * Math.log(u)) * ATLAS_RING * 0.1;
      const radius = Math.min(ATLAS_RING * 0.46, spread);
      dust.push({ orb: orbOf.get(memories[i].id), x: Math.cos(v * 6.283) * radius, y: Math.sin(v * 6.283) * radius,
        color: ATLAS_DUST_COLORS[i % ATLAS_DUST_COLORS.length], phase: random() * 6.283, size: 1 + random() * 1.4 });
    }
  }

  /* ── View ── */
  let width = 0, height = 0, dpr = 1, backdrop = null;
  const view = { x: 0, y: 0, scale: 1 };
  let intro = reduced ? 1 : 0, flight = null, dirty = true, destroyed = false, raf = 0, lastDraw = 0, lastFrame = 0;
  let focused = null, selectedMemory = null, hovered = null, fan = [], matches = null;
  const overview = () => {
    const scale = Math.min(width, height - 36) / (2 * ATLAS_RING * 1.5);
    return { x: width / 2, y: (height - 24) / 2, scale };
  };
  function resize() {
    width = canvas.clientWidth; height = canvas.clientHeight;
    if (!width || !height) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    backdrop = graphBackdrop(width, height, dpr);
    if (!focused) Object.assign(view, overview());
    dirty = true;
  }
  function flyTo(target) {
    if (reduced) { Object.assign(view, target); dirty = true; return; }
    flight = { from: { ...view }, to: target, start: performance.now(), duration: 600 };
  }
  function zoomAt(factor, px, py) {
    const scale = Math.max(0.25, Math.min(5, view.scale * factor)), ratio = scale / view.scale;
    view.x = px - (px - view.x) * ratio; view.y = py - (py - view.y) * ratio; view.scale = scale;
    flight = null; dirty = true;
  }
  const ease = () => 1 - Math.pow(1 - intro, 3);

  /* ── Focus: fly in to an orb and fan out its newest memories ── */
  function layoutFan(orb, extra) {
    const list = orb.memories.slice(0, ATLAS_FAN_LIMIT);
    if (extra && !list.includes(extra)) list.push(extra);
    fan = list.map((m, k) => {
      const angle = k * 2.39996, radius = orb.radius + 16 + 7.5 * Math.sqrt(k);
      return { m, x: orb.bx + Math.cos(angle) * radius, y: orb.by + Math.sin(angle) * radius, sx: 0, sy: 0 };
    });
    return Math.max(...fan.map(f => Math.hypot(f.x - orb.bx, f.y - orb.by)), orb.radius * 2) + 36;
  }
  function focus(orb, extra = null) {
    focused = orb; selectedMemory = extra; dirty = true;
    const extent = layoutFan(orb, extra), scale = Math.max(1.1, Math.min(3.4, Math.min(width, height) / (2 * extent)));
    flyTo({ x: width / 2 - orb.bx * scale, y: height / 2 - orb.by * scale, scale });
    describe(orb);
  }
  function back() {
    focused = null; selectedMemory = null; fan = []; dirty = true;
    flyTo(overview());
    describe(null);
  }

  /* ── Inspector ── */
  const openButton = (memory, text) => {
    const button = graphElement('button', text || memory.label, 'btn');
    button.onclick = () => openDetail(memory.id);
    return button;
  };
  function describe(item) {
    if (!panel) return;
    panel.replaceChildren();
    if (!item) {
      panel.textContent = 'Each orb is a group of your memories. Click one to fly in; click the centre or Reset view to come back. Drag to pan, scroll to zoom.';
      return;
    }
    if (item.kind === 'orb') {
      canvas.setAttribute('aria-label', `${item.label}: ${item.count} memories. Press Enter to fly in, Escape to return.`);
      panel.append(graphElement('p', bySource ? 'Source' : 'Project', 'graph-kind'), graphElement('h3', item.label),
        graphElement('p', `${item.count.toLocaleString()} ${item.count === 1 ? 'memory' : 'memories'}`));
      item.memories.slice(0, 25).forEach(m => panel.append(openButton(m)));
      if (item.count > 25) panel.append(graphElement('p', `…and ${(item.count - 25).toLocaleString()} more`, 'text-secondary'));
      return;
    }
    panel.append(graphElement('p', 'Memory', 'graph-kind'), graphElement('h3', item.label),
      graphElement('p', `${item.source || 'memory'}${item.tags && item.tags.length ? ' · ' + item.tags.join(', ') : ''}`), openButton(item, 'Open memory'));
  }
  function find(query) {
    const q = query.trim().toLowerCase();
    if (!q) { matches = null; back(); return; }
    const found = memories.filter(m => m.label.toLowerCase().includes(q) || (m.tags || []).some(t => t.toLowerCase().includes(q)));
    matches = found.length ? new Set(found) : null;
    dirty = true;
    if (!found.length) { if (panel) panel.textContent = 'No matching memory. Everything remains in the atlas.'; return; }
    focus(orbOf.get(found[0].id), found[0]);
    describe(found[0]);
  }

  /* ── Drawing ── */
  const place = time => {
    const e = ease();
    orbs.forEach(o => { o.x = o.bx * e; o.y = o.by * e; o.sx = o.x * view.scale + view.x; o.sy = o.y * view.scale + view.y; });
    fan.forEach(f => { f.sx = f.x * view.scale + view.x; f.sy = f.y * view.scale + view.y; });
    return time;
  };
  function draw(now) {
    if (!width || !height) return;
    const time = place(now / 1000), e = ease();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(backdrop, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const cx = view.x, cy = view.y, s = view.scale;
    // Orbit rings and spokes
    ctx.lineWidth = 1;
    ctx.strokeStyle = 'rgba(255,226,163,0.11)';
    [0.62, 1.0, 1.3].forEach(k => { ctx.beginPath(); ctx.arc(cx, cy, ATLAS_RING * k * e * s, 0, Math.PI * 2); ctx.stroke(); });
    ctx.globalCompositeOperation = 'lighter';
    orbs.forEach(o => {
      if (o.hidden) return;
      ctx.strokeStyle = o.color; ctx.globalAlpha = focused && focused !== o ? 0.05 : 0.22;
      ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(o.sx, o.sy); ctx.stroke();
    });
    // Central cloud
    const core = graphSprite('#ffd98a'), coreSize = 190 * s * Math.max(0.2, e);
    ctx.globalAlpha = 0.55; ctx.drawImage(core, cx - coreSize / 2, cy - coreSize / 2, coreSize, coreSize);
    dust.forEach(d => {
      if (d.orb && d.orb.hidden) return;
      const glow = focused && d.orb === focused;
      ctx.globalAlpha = (focused && !glow ? 0.25 : 0.85) * (reduced ? 1 : 0.75 + 0.25 * Math.sin(time * 1.2 + d.phase));
      ctx.fillStyle = glow ? focused.color : d.color;
      const side = Math.max(1.2, d.size * Math.pow(s, 0.5));
      ctx.fillRect(cx + d.x * e * s - side / 2, cy + d.y * e * s - side / 2, side, side);
    });
    drawOrbs(time);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    drawFan();
    if (graphSettings.showLabels) drawLabels();
  }
  function drawOrbs(time) {
    orbs.forEach(o => {
      if (o.hidden) return;
      const dim = focused && focused !== o ? 0.25 : (matches && !o.memories.some(m => matches.has(m)) ? 0.3 : 1);
      const r = o.radius * view.scale * (hovered === o ? 1.12 : 1), size = r * 3.2;
      ctx.globalAlpha = dim;
      ctx.drawImage(graphSprite(o.color), o.sx - size / 2, o.sy - size / 2, size, size);
      // Satellites: tiny specks circling the orb, more of them for bigger groups.
      ctx.fillStyle = o.color;
      o.satellites.forEach(sat => {
        const a = sat.phase + (reduced ? 0 : time * sat.speed), d = r * sat.reach;
        ctx.globalAlpha = dim * 0.7;
        ctx.fillRect(o.sx + Math.cos(a) * d - 0.8, o.sy + Math.sin(a) * d * 0.72 - 0.8, 1.7, 1.7);
      });
      if (o === focused) {
        ctx.globalAlpha = 1; ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(o.sx, o.sy, r * 0.95, 0, Math.PI * 2); ctx.stroke();
      }
    });
  }
  function drawFan() {
    if (!focused || !fan.length) return;
    ctx.globalCompositeOperation = 'lighter';
    ctx.strokeStyle = focused.color; ctx.globalAlpha = 0.14; ctx.lineWidth = 1;
    ctx.beginPath();
    fan.forEach(f => { ctx.moveTo(focused.sx, focused.sy); ctx.lineTo(f.sx, f.sy); });
    ctx.stroke();
    const size = Math.max(7, 9 * Math.pow(view.scale, 0.6));
    fan.forEach(f => {
      const lit = f.m === selectedMemory || f.m === (hovered && hovered.m) || (matches && matches.has(f.m));
      ctx.globalAlpha = lit ? 1 : 0.8;
      const bigger = lit ? size * 1.5 : size;
      ctx.drawImage(graphSprite(focused.color), f.sx - bigger / 2, f.sy - bigger / 2, bigger, bigger);
    });
    ctx.globalCompositeOperation = 'source-over';
    const over = hovered && hovered.m ? hovered : fan.find(f => f.m === selectedMemory);
    if (over) pill(over.sx, over.sy, over.m.label, focused.color);
  }
  function pill(x, y, text, color) {
    ctx.font = `11px ${GRAPH_FONT}`; ctx.textBaseline = 'middle'; ctx.textAlign = 'left';
    const label = text.length > 52 ? text.slice(0, 51) + '…' : text, w = ctx.measureText(label).width + 14;
    const left = Math.max(6, Math.min(width - w - 6, x + 10));
    ctx.fillStyle = 'rgba(6,8,18,0.92)'; ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.globalAlpha = 1;
    ctx.beginPath(); ctx.roundRect(left, y - 12, w, 24, 8); ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#f4ecdc'; ctx.fillText(label, left + 7, y);
  }
  function drawLabels() {
    ctx.globalAlpha = 1;
    try { ctx.letterSpacing = '2px'; } catch (error) { /* older browsers draw it unspaced */ }
    orbs.forEach(o => {
      if (o.hidden || o === focused) return;  // the focused orb is named in the inspector, and its label would sit on the fan
      const dim = focused && focused !== o ? 0.35 : 1, dx = Math.cos(o.angle), dy = Math.sin(o.angle);
      const gap = o.radius * view.scale * 1.7 + 12, x = o.sx + dx * gap, y = o.sy + dy * gap;
      ctx.textAlign = dx > 0.3 ? 'left' : dx < -0.3 ? 'right' : 'center';
      ctx.textBaseline = dy > 0.4 ? 'top' : dy < -0.4 ? 'bottom' : 'middle';
      ctx.font = `600 12px ${GRAPH_FONT}`; ctx.globalAlpha = dim; ctx.fillStyle = o.color;
      const name = o.label.length > 24 ? o.label.slice(0, 23) + '…' : o.label;
      ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(4,6,11,0.85)'; ctx.lineJoin = 'round';
      ctx.strokeText(name.toUpperCase(), x, y); ctx.fillText(name.toUpperCase(), x, y);
      ctx.font = `10px ${GRAPH_FONT}`; ctx.globalAlpha = dim * 0.6; ctx.fillStyle = '#d9cfba';
      const subY = y + (ctx.textBaseline === 'bottom' ? -15 : ctx.textBaseline === 'top' ? 15 : 14);
      ctx.fillText(`${o.count.toLocaleString()} ${o.count === 1 ? 'memory' : 'memories'}`, x, subY);
    });
    // The centre: the whole cloud.
    ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.globalAlpha = focused ? 0.45 : 0.9;
    ctx.font = `600 12px ${GRAPH_FONT}`; ctx.fillStyle = '#ffe2a3';
    ctx.fillText('ALL MEMORIES', view.x, view.y + 54 * view.scale * ease());
    ctx.font = `10px ${GRAPH_FONT}`; ctx.globalAlpha *= 0.65;
    ctx.fillText(`${memories.length.toLocaleString()} in total`, view.x, view.y + 54 * view.scale * ease() + 16);
    try { ctx.letterSpacing = '0px'; } catch (error) { /* see above */ }
    ctx.globalAlpha = 1;
  }

  /* ── Pointer, wheel and keyboard ── */
  const localPoint = e => { const rect = canvas.getBoundingClientRect(); return { x: e.clientX - rect.left, y: e.clientY - rect.top }; };
  function pick(p) {
    if (focused) {
      let best = null, gap = 10;
      fan.forEach(f => { const d = Math.hypot(f.sx - p.x, f.sy - p.y); if (d < gap) { gap = d; best = f; } });
      if (best) return best;
    }
    for (const o of orbs) if (!o.hidden && Math.hypot(o.sx - p.x, o.sy - p.y) < o.radius * view.scale * 1.15 + 6) return o;
    return Math.hypot(view.x - p.x, view.y - p.y) < 44 * view.scale + 14 ? { kind: 'center' } : null;
  }
  let drag = null;
  canvas.onpointerdown = e => { canvas.focus({ preventScroll: true }); canvas.setPointerCapture(e.pointerId); drag = { start: localPoint(e), viewX: view.x, viewY: view.y, moved: false }; };
  canvas.onpointermove = e => {
    const p = localPoint(e);
    if (!drag) {
      const over = pick(p), id = over ? (over.m ? over.m.id : over.id || over.kind) : null, before = hovered ? (hovered.m ? hovered.m.id : hovered.id || hovered.kind) : null;
      if (id !== before) { hovered = over; canvas.style.cursor = over ? 'pointer' : 'grab'; dirty = true; }
      return;
    }
    if (Math.hypot(p.x - drag.start.x, p.y - drag.start.y) > 4) drag.moved = true;
    if (drag.moved) { view.x = drag.viewX + p.x - drag.start.x; view.y = drag.viewY + p.y - drag.start.y; flight = null; dirty = true; }
  };
  canvas.onpointerup = e => {
    const was = drag; drag = null;
    if (!was || was.moved) return;
    const item = pick(localPoint(e));
    if (!item) { if (focused) back(); return; }
    if (item.kind === 'center') back();
    else if (item.kind === 'orb') focus(item);
    else if (item.m) { selectedMemory = item.m; describe(item.m); dirty = true; }
  };
  canvas.onpointercancel = () => { drag = null; };
  canvas.onpointerleave = () => { if (!drag && hovered) { hovered = null; dirty = true; } };
  canvas.onwheel = e => { e.preventDefault(); const p = localPoint(e); zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y); };
  canvas.onkeydown = e => {
    const live = orbs.filter(o => !o.hidden), at = focused ? live.indexOf(focused) : -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (!live.length) return;
      const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1;
      focus(live[(at + step + live.length) % live.length]);
    } else if (e.key === 'Escape' || e.key === 'Home') { e.preventDefault(); back(); }
    else if (e.key === '+' || e.key === '=') zoomAt(1.25, width / 2, height / 2);
    else if (e.key === '-') zoomAt(0.8, width / 2, height / 2);
  };

  /* ── Frame loop: stops by itself when the graph page is left ── */
  function frame(now) {
    raf = 0;
    if (destroyed || currentPage !== 'graph') return;
    const dt = lastFrame ? Math.min(0.1, (now - lastFrame) / 1000) : 0;
    lastFrame = now;
    if (intro < 1) { intro = Math.min(1, intro + dt / 1.3); dirty = true; }
    if (flight) {
      const t = Math.min(1, (now - flight.start) / flight.duration), k = 1 - Math.pow(1 - t, 3);
      view.x = flight.from.x + (flight.to.x - flight.from.x) * k;
      view.y = flight.from.y + (flight.to.y - flight.from.y) * k;
      view.scale = flight.from.scale + (flight.to.scale - flight.from.scale) * k;
      if (t >= 1) flight = null;
      dirty = true;
    }
    if (dirty || (!reduced && now - lastDraw > 32)) { draw(now); dirty = false; lastDraw = now; }
    raf = requestAnimationFrame(frame);
  }
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  canvas.style.cursor = 'grab';
  describe(null);
  resize();
  raf = requestAnimationFrame(frame);
  return {
    nodes: orbs, groups, hiddenGroups, find, select: item => (item ? focus(item) : back()), fit: back, refresh() { dirty = true; },
    zoomBy: factor => zoomAt(factor, width / 2, height / 2),
    reheat() { dirty = true; },
    toggleGroup(key, visible) { if (visible) hiddenGroups.delete(key); else hiddenGroups.add(key); orbs.forEach(o => { o.hidden = hiddenGroups.has(o.key); }); dirty = true; },
    animate() { intro = reduced ? 1 : 0; dirty = true; },
    restyle() { if (width && height) { backdrop = graphBackdrop(width, height, dpr); dirty = true; } },
    resume() { if (!raf && !destroyed) raf = requestAnimationFrame(frame); },
    destroy() { destroyed = true; cancelAnimationFrame(raf); observer.disconnect(); },
  };
}
