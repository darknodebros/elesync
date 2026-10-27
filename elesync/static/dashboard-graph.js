/* Knowledge graph: one glowing cloud of memories, drawn on a canvas.
   Memories are the dots. Projects and shared tags are labelled hubs that pull their
   memories together, and memories linked to each other directly (caused_by,
   depends_on, ...) are joined by an orange line. Colour marks the community a memory
   belongs to, and connectors take that colour, so related regions blend into each
   other. Every line is a recorded connection; nothing is inferred.
   Layout lives in dashboard-graph-layout.js. */
let graphSeq = 0, graphScene = null;

const GRAPH_STORAGE_KEY = 'elesync-graph-settings';
const GRAPH_DEFAULTS = {
  nodeSize: 1, linkThickness: 1, textFade: 1,
  center: 0.4, repel: 1.6, link: 1, distance: 40,
  showTags: true, showOrphans: true, showLabels: true, depth3d: true, colorBy: 'source', view: 'cloud',
};
const GRAPH_PALETTE = ['#9ee34a', '#ff4d94', '#ffa133', '#4d8dff', '#32d5c0', '#b074ff', '#ffd93d', '#ff6f5e'];
// Each kind of source keeps one recognisable colour, so a WhatsApp chat looks the same everywhere.
const GRAPH_SOURCE_COLORS = {
  manual: '#ffd93d', whatsapp: '#3ddc84', telegram: '#2aabee', claude: '#b074ff', chatgpt: '#32d5c0',
  gemini: '#4d8dff', grok: '#f2f4ff', deepseek: '#5b6cff', perplexity: '#ff4d94', copilot: '#ff6f5e',
  cursor: '#a0aec0', windsurf: '#7dd3fc', phone: '#ffe27a', summarize: '#c8ff7a',
};
const GRAPH_UNLINKED_COLOR = '#8fa3c7';
const GRAPH_LINK_COLOR = '#ff9f6b';
const GRAPH_BACKGROUND = '#04060b';
const GRAPH_MAX_ZOOM = 10;
const GRAPH_MIN_ZOOM = 0.05;
const GRAPH_AMBIENT_LIMIT = 3000;   // drift and travelling pulses only below this many nodes
const GRAPH_LITE_LIMIT = 3500;      // above this, memories draw as plain dots until zoomed in
const GRAPH_MAX_SIGNALS = 36;
const GRAPH_DEPTH_RANGE = 1400;    // world units over which perspective scales a dot by about 2x
const GRAPH_SWAY = 0.45;           // radians the cloud turns to each side while swaying
const GRAPH_BANNER_MAX_STRETCH = 6; // the Home banner widens the cloud at most this many times to fill its width
const GRAPH_FONT = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';

let graphSettings = graphLoadSettings();
function graphLoadSettings() {
  try { return { ...GRAPH_DEFAULTS, ...JSON.parse(localStorage.getItem(GRAPH_STORAGE_KEY) || '{}') }; }
  catch (error) { return { ...GRAPH_DEFAULTS }; }
}
function graphSaveSettings() {
  try { localStorage.setItem(GRAPH_STORAGE_KEY, JSON.stringify(graphSettings)); } catch (error) { /* private mode: keep going */ }
}

function graphUnit(text) {
  let hash = 2166136261;
  for (const ch of String(text)) { hash ^= ch.charCodeAt(0); hash = Math.imul(hash, 16777619); }
  return (hash >>> 0) / 4294967296;
}
function graphRandom(seed) {
  return () => {
    seed = (seed + 0x6D2B79F5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
function graphReducedMotion() { return matchMedia('(prefers-reduced-motion: reduce)').matches; }
function graphHslToHex(h, s, l) {
  const a = s * Math.min(l, 1 - l), f = n => {
    const k = (n + h / 30) % 12, v = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(255 * v).toString(16).padStart(2, '0');
  };
  return '#' + f(0) + f(8) + f(4);
}
function graphSourceLabel(source) {
  if (source === 'manual') return 'Notes';
  return typeof sourceLabel === 'function' ? sourceLabel(source) : source;
}
function graphColorAt(rank) {
  return rank < GRAPH_PALETTE.length ? GRAPH_PALETTE[rank] : graphHslToHex((rank * 137.508) % 360, 0.8, 0.62);
}

/* A ringed disc with a soft halo and a bright centre, rendered once per colour and reused. */
const graphSprites = new Map();
function graphSprite(color) {
  if (graphSprites.has(color)) return graphSprites.get(color);
  const sprite = document.createElement('canvas');
  sprite.width = sprite.height = 128;
  const c = sprite.getContext('2d'), halo = c.createRadialGradient(64, 64, 0, 64, 64, 64);
  halo.addColorStop(0, color + '66');
  halo.addColorStop(0.5, color + '33');
  halo.addColorStop(1, color + '00');
  c.fillStyle = halo;
  c.fillRect(0, 0, 128, 128);
  c.beginPath(); c.arc(64, 64, 36, 0, Math.PI * 2);
  c.lineWidth = 6; c.strokeStyle = color + '88'; c.stroke();
  c.beginPath(); c.arc(64, 64, 27, 0, Math.PI * 2);
  c.fillStyle = color; c.fill();
  c.beginPath(); c.arc(64, 64, 10, 0, Math.PI * 2);
  c.fillStyle = '#ffffff'; c.globalAlpha = 0.95; c.fill();
  graphSprites.set(color, sprite);
  return sprite;
}

/* Quiet backdrop: near-black, a faint teal glow behind the centre, and a few distant stars. */
function graphBackdrop(width, height, dpr) {
  const cosmic = document.documentElement.dataset.theme === 'cosmic';
  const layer = document.createElement('canvas');
  layer.width = Math.round(width * dpr);
  layer.height = Math.round(height * dpr);
  const c = layer.getContext('2d'), random = graphRandom(11);
  c.scale(dpr, dpr);
  c.fillStyle = cosmic ? '#05060b' : GRAPH_BACKGROUND;
  c.fillRect(0, 0, width, height);
  const glow = c.createRadialGradient(width * 0.5, height * 0.55, 0, width * 0.5, height * 0.55, Math.max(width, height) * 0.6);
  const haze = cosmic ? '58,40,14' : '18,52,60';
  glow.addColorStop(0, `rgba(${haze},${cosmic ? 0.5 : 0.55})`);
  glow.addColorStop(1, `rgba(${haze},0)`);
  c.fillStyle = glow;
  c.fillRect(0, 0, width, height);
  for (let i = 0, stars = Math.round(width * height / 9000); i < stars; i++) {
    c.globalAlpha = 0.15 + random() * 0.4;
    c.fillStyle = '#dfe8ff';
    c.fillRect(random() * width, random() * height, 1, 1);
  }
  return layer;
}

function graphZoom(factor) { if (graphScene) graphScene.zoomBy(factor); }
function graphReset() { if (graphScene) graphScene.fit(); }
function graphFind(query) { if (graphScene) graphScene.find(query); }
function graphAnimate() { if (graphScene) graphScene.animate(); }

async function loadGraph(refreshProjects = false) {
  const seq = ++graphSeq;
  const status = document.getElementById('graph-status'), select = document.getElementById('graph-project');
  status.textContent = 'Loading your graph…';
  try {
    if (refreshProjects) {
      const selected = select.value, data = await api('/api/projects');
      if (seq !== graphSeq) return;
      select.replaceChildren(new Option('All memories', ''));
      data.projects.forEach(p => select.add(new Option(p.name, p.id)));
      select.value = data.projects.some(p => p.id === selected) ? selected : '';
    }
    const data = await api('/api/graph?project_id=' + encodeURIComponent(select.value));
    if (seq !== graphSeq) return;
    const hubs = kind => data.nodes.filter(n => n.kind === kind).length;
    const links = data.edges.filter(e => e.kind === 'link').length;
    status.textContent = data.shown
      ? `${data.shown.toLocaleString()} memories · ${hubs('project')} projects · ${hubs('tag')} shared tags · ${data.edges.length.toLocaleString()} connections`
        + (links ? ` · ${links.toLocaleString()} direct link${links === 1 ? '' : 's'}` : '')
        + (data.truncated ? ` · Showing the newest ${data.shown.toLocaleString()} of ${data.total.toLocaleString()}; choose a project to narrow the view` : '')
      : 'No memories here yet. Save or import memories to start your graph.';
    document.getElementById('graph-search').value = '';
    renderGraph(data);
  } catch (error) {
    if (seq !== graphSeq) return;
    if (graphScene) graphScene.destroy();
    graphScene = null;
    document.getElementById('graph-inspector').textContent = 'Use Refresh to try again.';
    status.textContent = 'Could not load graph: ' + error.message;
  }
}

let graphLastData = null;
function renderGraph(data) {
  graphLastData = data;
  if (graphScene) graphScene.destroy();
  graphBuildControls();
  const canvas = document.getElementById('knowledge-graph'), panel = document.getElementById('graph-inspector');
  graphScene = graphSettings.view === 'atlas' ? createAtlasScene(canvas, panel, data) : createGraphScene(canvas, panel, data);
  graphSyncView();
  graphRenderGroups(graphScene);
}

/* Cloud (every memory as a dot) or Atlas (one orb per source or project). */
function graphSyncView() {
  const atlas = graphSettings.view === 'atlas';
  [['cloud', !atlas], ['atlas', atlas]].forEach(([name, on]) => {
    const button = document.getElementById('graph-view-' + name);
    if (button) { button.setAttribute('aria-pressed', String(on)); button.classList.toggle('active', on); }
  });
  const host = document.getElementById('graph-controls');
  if (host) host.dataset.view = atlas ? 'atlas' : 'cloud';
}
function graphSetView(name) {
  graphSettings.view = name === 'atlas' ? 'atlas' : 'cloud';
  graphSaveSettings();
  graphSyncView();
  if (graphLastData) renderGraph(graphLastData);
}

/* ── Controls: filters, groups, display and force sliders (built once) ── */
let graphControlsReady = false;
function graphElement(tag, text, className) {
  const el = document.createElement(tag);
  if (text) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function graphCheckbox(key, text, cloudOnly = false) {
  const label = graphElement('label', '', 'graph-check'), input = graphElement('input');
  input.type = 'checkbox'; input.checked = graphSettings[key];
  input.onchange = () => { graphSettings[key] = input.checked; graphSaveSettings(); if (graphScene) graphScene.refresh(); };
  label.append(input, document.createTextNode(' ' + text));
  if (cloudOnly) label.classList.add('cloud-only');
  return label;
}
function graphSlider(key, text, min, max, step, forces) {
  const row = graphElement('label', '', 'graph-slider'), input = graphElement('input');
  input.type = 'range'; input.min = min; input.max = max; input.step = step; input.value = graphSettings[key];
  input.setAttribute('aria-label', text);
  input.oninput = () => {
    graphSettings[key] = Number(input.value); graphSaveSettings();
    if (graphScene) { if (forces) graphScene.reheat(); else graphScene.refresh(); }
  };
  row.append(graphElement('span', text), input);
  return row;
}
function graphBuildControls() {
  if (graphControlsReady) return;
  const host = document.getElementById('graph-controls');
  if (!host) return;
  graphControlsReady = true;
  const toggle = graphElement('button', 'Display', 'btn graph-controls-toggle'), panel = graphElement('div');
  toggle.type = 'button'; toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', 'graph-controls-panel');
  panel.id = 'graph-controls-panel'; panel.hidden = true;
  toggle.onclick = () => { panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden)); };
  const section = (title, ...children) => { const box = graphElement('section'); box.append(graphElement('h4', title), ...children); return box; };
  const groups = graphElement('div', '', 'graph-groups'); groups.id = 'graph-groups';
  const animate = graphElement('button', 'Animate', 'btn'); animate.type = 'button'; animate.onclick = graphAnimate;
  const colorBy = graphElement('label', '', 'graph-slider'), colorSelect = graphElement('select');
  [['source', 'Source (notes, chats…)'], ['project', 'Project']].forEach(([value, text]) => colorSelect.add(new Option(text, value)));
  colorSelect.value = graphSettings.colorBy; colorSelect.setAttribute('aria-label', 'Colour by');
  colorSelect.onchange = () => { graphSettings.colorBy = colorSelect.value; graphSaveSettings(); if (graphLastData) renderGraph(graphLastData); };
  colorBy.append(graphElement('span', 'Colour by'), colorSelect);
  panel.append(
    section('Filters', colorBy, graphCheckbox('showTags', 'Tags', true), graphCheckbox('showOrphans', 'Memories with no connections', true), graphCheckbox('showLabels', 'Labels'), graphCheckbox('depth3d', '3D motion', true)),
    section('Groups', groups),
    section('Display', graphSlider('nodeSize', 'Node size', 0.5, 2.5, 0.05, false), graphSlider('linkThickness', 'Link thickness', 0.3, 3, 0.05, false), graphSlider('textFade', 'Text fade', 0.3, 3, 0.05, false)),
    section('Forces', graphSlider('center', 'Center force', 0, 3, 0.05, true), graphSlider('repel', 'Repel force', 0.2, 3, 0.05, true), graphSlider('link', 'Link force', 0.2, 3, 0.05, true), graphSlider('distance', 'Link distance', 10, 120, 1, true), animate),
  );
  panel.querySelectorAll('section').forEach(box => { if (['Display', 'Forces'].includes(box.firstChild.textContent)) box.classList.add('cloud-only'); });
  host.append(toggle, panel);
}
function graphRenderGroups(scene) {
  const list = document.getElementById('graph-groups');
  if (!list) return;
  list.replaceChildren();
  scene.groups.forEach(group => {
    const label = graphElement('label', '', 'graph-check graph-group'), input = graphElement('input'), dot = graphElement('i', '', 'dot');
    input.type = 'checkbox'; input.checked = !scene.hiddenGroups.has(group.key);
    input.onchange = () => scene.toggleGroup(group.key, input.checked);
    dot.style.background = group.color; dot.style.boxShadow = '0 0 6px ' + group.color;
    label.append(input, dot, graphElement('span', group.label), graphElement('em', group.count.toLocaleString()));
    list.append(label);
  });
}

/* options.embedded draws a read-only copy (the Home banner): no controls or inspector, hub labels only,
   and a click opens the full graph. options.settings overrides the saved display settings. */
function createGraphScene(canvas, panel, data, options = {}) {
  const embedded = Boolean(options.embedded), page = options.page || 'graph', cfg = options.settings || graphSettings;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('This browser does not support the graph canvas');
  const reduced = graphReducedMotion();

  /* ── Model ── */
  const nodes = data.nodes.map((n, i) => ({
    ...n, index: i, x: 0, y: 0, vx: 0, vy: 0, sx: 0, sy: 0, degree: 0, links: [], related: [],
    pinned: false, hidden: false, phase: (i * 7.31) % 6.283, zr: 0, z: 0, f: 1,
  }));
  const byId = new Map(nodes.map(n => [n.id, n]));
  const edges = [];
  data.edges.forEach(e => {
    const a = byId.get(e.source), b = byId.get(e.target);
    if (!a || !b) return;
    edges.push({ a, b, kind: e.kind, relation: e.relation || '', color: GRAPH_LINK_COLOR });
    a.degree++; b.degree++; a.links.push(b); b.links.push(a);
    if (e.kind === 'link') {
      a.related.push({ node: b, relation: e.relation, outgoing: true });
      b.related.push({ node: a, relation: e.relation, outgoing: false });
    }
  });
  const hubs = nodes.filter(n => n.kind !== 'memory');
  const memories = nodes.filter(n => n.kind === 'memory');

  /* Community: a memory belongs to its project, else to its busiest tag (never to a memory it links to).
     Tags take the community most of their members belong to. */
  const hubRank = hub => (hub.kind === 'project' ? 1e9 : 0) + hub.degree;
  const bySource = cfg.colorBy === 'source';
  memories.forEach(n => {
    if (bySource) { n.group = 'source:' + String(n.source || 'manual').toLowerCase(); return; }
    const home = n.links.filter(l => l.kind !== 'memory').sort((a, b) => hubRank(b) - hubRank(a))[0];
    n.group = home ? home.id : '';
  });
  hubs.forEach(h => {
    if (!bySource && h.kind === 'project') { h.group = h.id; return; }
    const votes = new Map();
    h.links.forEach(m => { if (m.group) votes.set(m.group, (votes.get(m.group) || 0) + 1); });
    h.group = [...votes.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] || (bySource ? '' : h.id);
  });
  const tally = () => {
    const sizes = new Map();
    nodes.forEach(n => { if (n.group) sizes.set(n.group, (sizes.get(n.group) || 0) + 1); });
    return sizes;
  };
  // Projects always get their own colour; a tag-only community needs enough members to earn one.
  const minCommunity = Math.max(8, Math.round(memories.length * 0.01));
  const firstSizes = tally();
  nodes.forEach(n => {
    const keep = n.group && (bySource || n.group.startsWith('project:') || firstSizes.get(n.group) >= minCommunity);
    if (!keep) n.group = '';
  });
  const groupSizes = tally();
  const ranked = [...groupSizes.entries()].sort((a, b) => b[1] - a[1]);
  const groupColor = new Map(ranked.map(([key, , ], rank) => [key, (bySource && GRAPH_SOURCE_COLORS[key.slice(7)]) || graphColorAt(rank + (bySource ? GRAPH_PALETTE.length : 0))]));
  const groups = ranked.map(([key, count]) => ({ key, count, color: groupColor.get(key), label: bySource ? graphSourceLabel(key.slice(7)) : (byId.get(key) || { label: key }).label }));
  const unlinked = memories.filter(n => !n.group).length;
  if (unlinked) groups.push({ key: '', count: unlinked, color: GRAPH_UNLINKED_COLOR, label: bySource ? 'Unknown source' : 'No project or tag' });
  nodes.forEach(n => {
    n.color = n.group ? groupColor.get(n.group) : GRAPH_UNLINKED_COLOR;
    if (n.kind === 'project') { n.radius = 7 + Math.min(9, Math.sqrt(n.degree) * 0.4); n.charge = 4; n.mass = 3; n.zr = 0; }
    else if (n.kind === 'tag') { n.radius = 4 + Math.min(5, Math.sqrt(n.degree) * 0.3); n.charge = 2.5; n.mass = 2; n.zr = 0; }
    else {
      // Most memories are small dots, some medium, a few large, as in the reference; links add weight.
      const u = (Math.imul(n.index + 1, 2654435761) >>> 0) / 4294967296;
      n.radius = (u < 0.7 ? 1.5 : u < 0.95 ? 2.6 : 4.2) + Math.min(1.2, Math.sqrt(n.degree) * 0.25); n.charge = 1; n.mass = 1;
      n.zr = (graphUnit('g' + n.group) * 2 - 1) * 0.5 + (graphUnit(n.id) * 2 - 1) * 0.5;
    }
  });
  edges.forEach(e => { if (e.kind !== 'link') e.color = e.a.color; });
  const edgeGroups = new Map();
  edges.forEach(e => {
    if (!edgeGroups.has(e.color)) edgeGroups.set(e.color, []);
    edgeGroups.get(e.color).push(e);
  });
  const hubsByDegree = hubs.slice().sort((a, b) => b.degree - a.degree);
  // The few busiest memories are named on the canvas, like the labelled notes in the reference.
  const featured = memories.filter(n => n.degree >= 5).sort((a, b) => b.degree - a.degree).slice(0, 5);
  const hiddenGroups = new Set();
  const large = memories.length > GRAPH_LITE_LIMIT;
  const ambient = !reduced && nodes.length <= GRAPH_AMBIENT_LIMIT;

  /* Start each community in its own patch, so the layout begins close to its answer. */
  function seed() {
    const random = graphRandom(5);
    const centers = new Map(groups.map((g, i) => {
      const ring = 38 * Math.sqrt(i + 0.5) * Math.sqrt(groups.length);
      return [g.key, { x: Math.cos(i * 2.39996) * ring, y: Math.sin(i * 2.39996) * ring }];
    }));
    nodes.forEach(n => {
      const c = centers.get(n.group) || { x: 0, y: 0 }, size = groupSizes.get(n.group) || 20;
      const angle = random() * Math.PI * 2, radius = (6 + 5 * Math.sqrt(size)) * Math.sqrt(random());
      n.x = c.x + Math.cos(angle) * radius; n.y = c.y + Math.sin(angle) * radius; n.vx = n.vy = 0;
    });
  }
  seed();
  const layout = createGraphLayout(nodes, edges, cfg);

  /* ── View: screen = world * scale + offset ── */
  let width = 0, height = 0, dpr = 1, backdrop = null;
  // `stretch` widens the picture sideways; only the embedded Home banner uses it (see fitTarget).
  const view = { x: 0, y: 0, scale: 1, stretch: 1 };
  let autoFit = true, fitted = false, flight = null, dirty = true, destroyed = false, raf = 0, lastDraw = 0;
  let hovered = null, selected = null, matches = null, cloudRadius = 300, rotation = 0;

  function fitTarget() {
    const shown = nodes.filter(n => !n.hidden);
    if (!shown.length || !width) return { x: width / 2, y: height / 2, scale: 1, stretch: 1 };
    let mx = 0, my = 0;
    shown.forEach(n => { mx += n.x; my += n.y; });
    mx /= shown.length; my /= shown.length;
    let vx = 0, vy = 0;
    shown.forEach(n => { vx += (n.x - mx) ** 2; vy += (n.y - my) ** 2; });
    // Fit the bulk of the cloud (about 2 standard deviations), so one stray node cannot shrink everything.
    const reach = embedded ? 1.7 : 2.1;
    let spanX = Math.max(80, 2 * reach * Math.sqrt(vx / shown.length));
    const spanY = Math.max(80, 2 * reach * Math.sqrt(vy / shown.length));
    cloudRadius = Math.sqrt((vx + vy) / shown.length);
    if (embedded) {
      // The banner frames the middle 94% of dots across, so a lopsided cloud (one big project off to one side)
      // is centred on its bulk and its long tail does not hang off one edge once stretched.
      const xs = shown.map(n => n.x).sort((a, b) => a - b), last = xs.length - 1;
      const lo = xs[Math.floor(last * 0.03)], hi = xs[Math.ceil(last * 0.97)];
      spanX = Math.max(80, (hi - lo) * 1.08); mx = (lo + hi) / 2;
    }
    const usableHeight = height - (embedded ? 0 : 40);
    const scale = Math.max(GRAPH_MIN_ZOOM, Math.min(2.4, Math.min(width / spanX, usableHeight / spanY)));
    // The Home banner is wide and short. A well-connected vault settles into a round cloud whatever the layout's
    // `aspect` does, so the banner also stretches the picture sideways until the cloud spans its full width.
    const stretch = embedded ? Math.max(1, Math.min(GRAPH_BANNER_MAX_STRETCH, width / spanX / scale)) : 1;
    return { x: width / 2 - mx * scale * stretch, y: usableHeight / 2 + 6 - my * scale, scale, stretch };
  }
  function fit() { autoFit = false; flyTo(fitTarget()); }
  function flyTo(target) {
    if (reduced) { Object.assign(view, target); dirty = true; return; }
    flight = { from: { ...view }, to: target, start: performance.now(), duration: 450 };
  }
  function zoomAt(factor, px, py) {
    const scale = Math.max(GRAPH_MIN_ZOOM, Math.min(GRAPH_MAX_ZOOM, view.scale * factor)), ratio = scale / view.scale;
    view.x = px - (px - view.x) * ratio; view.y = py - (py - view.y) * ratio; view.scale = scale;
    autoFit = false; flight = null; dirty = true;
  }
  function resize() {
    width = canvas.clientWidth; height = canvas.clientHeight;
    if (!width || !height) return;
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
    backdrop = graphBackdrop(width, height, dpr);
    if (autoFit) Object.assign(view, fitTarget());
    dirty = true;
  }
  const sizeOf = n => n.radius * cfg.nodeSize * Math.pow(view.scale, 0.45) * n.f;
  const spins = () => ambient && cfg.depth3d;

  function applyFilters() {
    nodes.forEach(n => {
      n.hidden = hiddenGroups.has(n.group)
        || (n.kind === 'tag' && !cfg.showTags)
        || (n.kind === 'memory' && !cfg.showOrphans && n.degree === 0);
    });
    dirty = true;
  }

  /* ── Drawing ── */
  /* A gentle 3D sway: every dot has a depth, the cloud turns a little to each side, and nearer dots
     draw larger. Layout stays flat and cheap; only the projection moves. */
  function place(time) {
    const spin = spins();
    rotation = spin ? Math.sin(time * 0.13) * GRAPH_SWAY : 0;
    const cos = Math.cos(rotation), sin = Math.sin(rotation);
    nodes.forEach(n => {
      n.z = n.zr * cloudRadius * 0.9;
      // The banner's stretch widens the flat layout before the turn, so the sway itself is not magnified.
      const across = n.x * view.stretch * cos + n.z * sin, depth = -n.x * sin + n.z * cos;
      n.f = spin ? Math.max(0.6, Math.min(1.7, 1 + depth / GRAPH_DEPTH_RANGE * (300 / Math.max(120, cloudRadius)) * 1.4)) : 1;
      // A stretched banner already fills its width, so nearer dots grow but are not pushed off the edges.
      const reach = spin && !embedded ? n.f : 1;
      let x = across * view.scale * reach + view.x, y = n.y * view.scale * (spin ? n.f : 1) + view.y;
      if (ambient && n.kind === 'memory') { x += Math.sin(time * 0.35 + n.phase) * 1.6; y += Math.cos(time * 0.29 + n.phase * 1.3) * 1.6; }
      n.sx = x; n.sy = y;
    });
  }
  function focusSet() {
    const focus = selected || hovered;
    if (!focus && !matches) return null;
    const set = new Set();
    if (matches) matches.forEach(n => set.add(n));
    if (focus) { set.add(focus); focus.links.forEach(n => set.add(n)); }
    return set;
  }
  function draw(now) {
    if (!width || !height) return;
    const time = now / 1000;
    place(time);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    ctx.drawImage(backdrop, 0, 0);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const focus = focusSet(), active = selected || hovered;
    ctx.globalCompositeOperation = 'lighter';
    drawEdges(focus, active);
    if (ambient) drawSignals(time, focus);
    drawNodes(focus);
    ctx.globalCompositeOperation = 'source-over';
    ctx.globalAlpha = 1;
    if (cfg.showLabels) drawLabels(focus, active);
    else if (active) label(active, 12, '#ffffff', 1, true);
  }
  function drawEdges(focus, active) {
    ctx.lineCap = 'round';
    const quiet = Math.max(0.07, Math.min(0.34, 6 / Math.sqrt(edges.length + 1)));
    const lineWidth = 0.7 * cfg.linkThickness;
    edgeGroups.forEach((group, color) => {
      const path = new Path2D(), lit = new Path2D();
      let any = false, anyLit = false;
      group.forEach(e => {
        if (e.a.hidden || e.b.hidden) return;
        const ax = e.a.sx, ay = e.a.sy, bx = e.b.sx, by = e.b.sy;
        if ((ax < -20 && bx < -20) || (ax > width + 20 && bx > width + 20) || (ay < -20 && by < -20) || (ay > height + 20 && by > height + 20)) return;
        if (active && (e.a === active || e.b === active)) { lit.moveTo(ax, ay); lit.lineTo(bx, by); anyLit = true; }
        else { path.moveTo(ax, ay); path.lineTo(bx, by); any = true; }
      });
      ctx.strokeStyle = color;
      // Direct links are rare and deliberate, so they stay visible over the hub web.
      const direct = color === GRAPH_LINK_COLOR;
      if (any) { ctx.globalAlpha = direct ? (focus ? 0.15 : 0.42) : (focus ? 0.05 : quiet); ctx.lineWidth = lineWidth * (direct ? 1.3 : 1); ctx.stroke(path); }
      if (anyLit) { ctx.globalAlpha = 0.9; ctx.lineWidth = lineWidth * 2.2; ctx.stroke(lit); }
    });
  }

  /* Pulses of light that travel along connections, like signals passing between memories. */
  const signals = [];
  let lastSignalTime = 0;
  function drawSignals(time, focus) {
    if (!signals.length && edges.length) {
      const random = graphRandom(23);
      for (let i = 0; i < Math.min(GRAPH_MAX_SIGNALS, edges.length); i++) signals.push({ edge: edges[Math.floor(random() * edges.length)], t: random(), speed: 0.12 + random() * 0.2 });
    }
    const dt = lastSignalTime ? Math.min(0.1, time - lastSignalTime) : 0;
    lastSignalTime = time;
    signals.forEach(s => {
      s.t += s.speed * dt;
      if (s.t > 1) { s.t = 0; s.edge = edges[Math.floor(Math.random() * edges.length)]; }
      const e = s.edge;
      if (e.a.hidden || e.b.hidden || (focus && !(focus.has(e.a) && focus.has(e.b)))) return;
      const x = e.a.sx + (e.b.sx - e.a.sx) * s.t, y = e.a.sy + (e.b.sy - e.a.sy) * s.t;
      const size = 7 * cfg.nodeSize;
      ctx.globalAlpha = 0.9 * Math.sin(Math.PI * s.t);
      ctx.drawImage(graphSprite(e.color), x - size / 2, y - size / 2, size, size);
    });
  }
  function drawNodes(focus) {
    const lite = large && view.scale < 1.5;
    nodes.forEach(n => {
      if (n.hidden) return;
      const r = sizeOf(n), x = n.sx, y = n.sy;
      if (x < -r * 4 || x > width + r * 4 || y < -r * 4 || y > height + r * 4) return;
      const dim = focus && !focus.has(n) ? 0.16 : 1;
      if (lite && n.kind === 'memory') {
        const side = Math.max(1.6, r);
        ctx.globalAlpha = dim * 0.85; ctx.fillStyle = n.color;
        ctx.fillRect(x - side / 2, y - side / 2, side, side);
        return;
      }
      const size = r * 5.2;
      ctx.globalAlpha = dim;
      ctx.drawImage(graphSprite(n.color), x - size / 2, y - size / 2, size, size);
      if (n === selected) {
        ctx.globalAlpha = 1; ctx.strokeStyle = '#ffffff'; ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(x, y, Math.max(7, r * 1.6), 0, Math.PI * 2); ctx.stroke();
      }
    });
  }
  function drawLabels(focus, active) {
    ctx.textBaseline = 'middle';
    const fade = cfg.textFade;
    hubsByDegree.forEach((n, rank) => {
      if (n.hidden || (embedded && n.kind !== 'project')) return;
      const lit = focus && focus.has(n);
      if (!(n.kind === 'project' || lit || view.scale >= fade * 1.6)) return;
      const sub = `${n.kind === 'project' ? 'PROJECT' : 'TAG'} · ${n.degree.toLocaleString()} nodes`;
      label(n, n.kind === 'project' ? 12 : 10, n.color, focus && !lit ? 0.25 : (n.kind === 'project' ? 0.95 : 0.8), false, n.kind === 'project', embedded ? '' : sub);
    });
    if (embedded) return;
    if (active && active.kind === 'memory') label(active, 12, '#ffffff', 1, true);
    if (focus && active && focus.size <= 14) focus.forEach(n => { if (n.kind === 'memory' && n !== active && !n.hidden) label(n, 10, '#dfe4ff', 0.9); });
    if (!focus) featured.forEach(n => { if (!n.hidden) label(n, 10, n.color, 0.6); });
    if (!focus && view.scale >= fade * 2.4) {
      let shown = 0;
      memories.forEach(n => {
        if (n.hidden || shown >= 60 || n.sx < 0 || n.sx > width || n.sy < 0 || n.sy > height) return;
        label(n, 10, n.color, 0.8); shown++;
      });
    }
  }
  function label(n, size, color, alpha, pill = false, upper = false, sub = '') {
    const r = sizeOf(n);
    if (n.sx < -200 || n.sx > width + 200 || n.sy < -20 || n.sy > height + 20) return;
    let text = upper ? n.label.toUpperCase() : n.label;
    if (text.length > 44) text = text.slice(0, 43) + '…';
    ctx.font = `${size}px ${GRAPH_FONT}`;
    const textWidth = ctx.measureText(text).width;
    // Labels sit beside their dot and flip to the other side near the edge of the canvas.
    const flip = n.sx + r * 1.2 + 6 + textWidth > width - 6;
    const x = Math.max(8, flip ? n.sx - r * 1.2 - 6 - textWidth : n.sx + r * 1.2 + 6), y = n.sy;
    ctx.globalAlpha = alpha;
    if (pill) {
      ctx.fillStyle = 'rgba(6,8,18,0.9)'; ctx.strokeStyle = n.color; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect(x - 7, y - 12, textWidth + 14, 24, 8); ctx.fill(); ctx.stroke();
    } else {
      ctx.lineWidth = 3; ctx.strokeStyle = 'rgba(4,6,11,0.85)'; ctx.lineJoin = 'round';
      ctx.strokeText(text, x, y);
    }
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
    if (sub) {
      // A dim second line, like the reference's "SOCIAL · 10 nodes".
      ctx.font = `${size - 2}px ${GRAPH_FONT}`; ctx.globalAlpha = alpha * 0.55;
      ctx.fillText(sub, x, y + size + 2);
    }
  }

  /* ── Selection and inspector ── */
  function memoryButton(memory) {
    const button = graphElement('button', memory.label, 'btn');
    button.onclick = () => openDetail(memory.id);
    return button;
  }
  function select(node, focusView = false) {
    selected = node; if (panel) panel.replaceChildren(); dirty = true;
    if (!panel) return;
    if (!node) { panel.textContent = 'Select a dot, tag or project to see how your memories connect.'; return; }
    if (focusView) { autoFit = false; flyTo({ x: width / 2 - node.x * view.scale, y: height / 2 - node.y * view.scale, scale: view.scale }); }
    const kindName = { memory: 'Memory', tag: 'Shared tag', project: 'Project' }[node.kind];
    canvas.setAttribute('aria-label', `${kindName}: ${node.label}. Arrow keys move between nodes; Enter opens a memory.`);
    panel.append(graphElement('p', kindName, 'graph-kind'), graphElement('h3', node.label));
    if (node.kind === 'memory') {
      const projectNames = node.links.filter(l => l.kind === 'project').map(l => l.label);
      const tagNames = node.links.filter(l => l.kind === 'tag').map(l => l.label);
      panel.append(graphElement('p', `${node.source || 'memory'}${node.tags.length ? ' · ' + node.tags.join(', ') : ''}`));
      if (projectNames.length) panel.append(graphElement('p', 'In project: ' + projectNames.join(', ')));
      if (tagNames.length) panel.append(graphElement('p', `Shares ${tagNames.length} tag${tagNames.length === 1 ? '' : 's'} with other memories`));
      panel.append(memoryButton(node));
      if (node.related.length) {
        panel.append(graphElement('p', 'Linked memories', 'graph-kind'));
        node.related.forEach(r => {
          const relation = r.relation.replace(/_/g, ' ');
          panel.append(graphElement('p', r.outgoing ? `${relation} →` : `← ${relation}`, 'text-secondary'), memoryButton(r.node));
        });
      }
    } else {
      const members = node.links.filter(l => l.kind === 'memory');
      panel.append(graphElement('p', `${members.length} connected ${members.length === 1 ? 'memory' : 'memories'}`));
      members.slice(0, 25).forEach(m => panel.append(memoryButton(m)));
      if (members.length > 25) panel.append(graphElement('p', `…and ${members.length - 25} more`, 'text-secondary'));
    }
  }
  function find(query) {
    const q = query.trim().toLowerCase();
    if (!q || !panel) { matches = null; select(null); return; }
    const found = nodes.filter(n => !n.hidden && (n.label.toLowerCase().includes(q) || (n.tags || []).some(t => t.toLowerCase().includes(q))));
    matches = found.length ? new Set(found) : null;
    dirty = true;
    if (found.length) select(found[0], true);
    else panel.textContent = 'No matching memory, tag or project. Everything remains in the graph.';
  }

  /* ── Pointer, wheel and keyboard ── */
  function localPoint(e) { const rect = canvas.getBoundingClientRect(); return { x: e.clientX - rect.left, y: e.clientY - rect.top }; }
  function pick(p) {
    let best = null, bestGap = 12;
    nodes.forEach(n => {
      if (n.hidden) return;
      const gap = Math.hypot(n.sx - p.x, n.sy - p.y) - sizeOf(n);
      if (gap < bestGap) { bestGap = gap; best = n; }
    });
    return best;
  }
  const pointers = new Map();
  let drag = null, pinch = null;
  canvas.onpointerdown = e => {
    canvas.focus({ preventScroll: true });
    canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId, localPoint(e));
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y), scale: view.scale };
      drag = null; return;
    }
    const p = localPoint(e), node = pick(p);
    drag = { node, start: p, moved: false, viewX: view.x, viewY: view.y };
    if (node) node.pinned = true;
  };
  canvas.onpointermove = e => {
    const p = localPoint(e);
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, p);
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()], distance = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt(Math.max(GRAPH_MIN_ZOOM, Math.min(GRAPH_MAX_ZOOM, pinch.scale * distance / pinch.distance)) / view.scale, (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    if (!drag) {
      const over = pick(p);
      if (over !== hovered) { hovered = over; canvas.style.cursor = over ? 'pointer' : 'grab'; dirty = true; }
      return;
    }
    if (Math.hypot(p.x - drag.start.x, p.y - drag.start.y) > 4) drag.moved = true;
    if (!drag.moved) return;
    autoFit = false; flight = null; dirty = true;
    if (drag.node) {
      const f = drag.node.f || 1, across = (p.x - view.x) / (view.scale * f);
      drag.node.x = (across - drag.node.z * Math.sin(rotation)) / Math.cos(rotation); drag.node.y = (p.y - view.y) / (view.scale * f);
      layout.reheat(0.3);
    } else { view.x = drag.viewX + p.x - drag.start.x; view.y = drag.viewY + p.y - drag.start.y; }
  };
  canvas.onpointerup = e => {
    pointers.delete(e.pointerId);
    if (pointers.size < 2) pinch = null;
    if (!drag) return;
    if (drag.node) drag.node.pinned = false;
    if (!drag.moved) select(drag.node);
    drag = null;
  };
  canvas.onpointercancel = e => { pointers.delete(e.pointerId); if (drag && drag.node) drag.node.pinned = false; drag = pinch = null; };
  canvas.onpointerleave = () => { if (!drag && hovered) { hovered = null; dirty = true; } };
  canvas.onwheel = e => { e.preventDefault(); const p = localPoint(e); zoomAt(e.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y); };
  canvas.onkeydown = e => {
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault();
      const visible = nodes.filter(n => !n.hidden);
      if (!visible.length) return;
      const step = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1;
      const at = selected ? visible.indexOf(selected) : -1;
      select(visible[(at + step + visible.length) % visible.length], true);
    } else if (e.key === 'Enter' && selected && selected.kind === 'memory') { e.preventDefault(); openDetail(selected.id); }
    else if (e.key === 'Escape') select(null);
    else if (e.key === 'Home') { e.preventDefault(); fit(); }
    else if (e.key === '+' || e.key === '=') zoomAt(1.25, width / 2, height / 2);
    else if (e.key === '-') zoomAt(0.8, width / 2, height / 2);
  };

  /* ── Frame loop: stops by itself when the graph page is left ── */
  function frame(now) {
    raf = 0;
    if (destroyed || currentPage !== page) return;
    if (!layout.settled) {
      const stop = performance.now() + 9;
      do { layout.tick(); } while (!layout.settled && performance.now() < stop);
      dirty = true;
    }
    // Keep easing toward the fit until it is reached, even after the layout has settled. A small vault settles
    // during the warm-up before the first frame, and used to stay framed on its round starting positions.
    if (autoFit && !fitted) {
      const target = fitTarget();
      view.x += (target.x - view.x) * 0.2; view.y += (target.y - view.y) * 0.2; view.scale += (target.scale - view.scale) * 0.2;
      view.stretch += (target.stretch - view.stretch) * 0.2;
      const close = Math.abs(target.x - view.x) < 0.5 && Math.abs(target.y - view.y) < 0.5
        && Math.abs(target.scale - view.scale) < target.scale * 0.002 && Math.abs(target.stretch - view.stretch) < 0.002;
      if (layout.settled && close) { Object.assign(view, target); fitted = true; }
      dirty = true;
    }
    if (flight) {
      const t = Math.min(1, (now - flight.start) / flight.duration), ease = 1 - Math.pow(1 - t, 3);
      view.x = flight.from.x + (flight.to.x - flight.from.x) * ease;
      view.y = flight.from.y + (flight.to.y - flight.from.y) * ease;
      view.scale = flight.from.scale + (flight.to.scale - flight.from.scale) * ease;
      view.stretch = flight.from.stretch + ((flight.to.stretch ?? flight.from.stretch) - flight.from.stretch) * ease;
      if (t >= 1) flight = null;
      dirty = true;
    }
    // Ambient motion redraws at about 30 frames a second; interaction and layout redraw every frame.
    if (dirty || (ambient && now - lastDraw > 32)) { draw(now); dirty = false; lastDraw = now; }
    raf = requestAnimationFrame(frame);
  }

  if (embedded) {
    canvas.onpointerdown = canvas.onpointermove = canvas.onpointerup = canvas.onpointercancel = canvas.onpointerleave = canvas.onwheel = canvas.onkeydown = null;
    canvas.onclick = () => go('graph');
  }
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  canvas.style.cursor = embedded ? 'pointer' : 'grab';
  if (panel) panel.textContent = 'Select a dot, tag or project to see how your memories connect. Drag to move things, scroll or pinch to zoom.';
  resize();
  if (reduced) { for (let i = 0; i < 400 && !layout.settled; i++) layout.tick(); Object.assign(view, fitTarget()); autoFit = false; }
  else { const stop = performance.now() + 60; while (!layout.settled && performance.now() < stop) layout.tick(); }
  applyFilters();
  raf = requestAnimationFrame(frame);
  return {
    nodes, groups, hiddenGroups, select, find, fit, refresh: applyFilters,
    zoomBy: factor => zoomAt(factor, width / 2, height / 2),
    reheat: () => layout.reheat(0.5),
    toggleGroup(key, visible) { if (visible) hiddenGroups.delete(key); else hiddenGroups.add(key); applyFilters(); },
    animate() { seed(); autoFit = true; fitted = false; layout.reheat(1); dirty = true; },
    resume() { if (!raf && !destroyed) raf = requestAnimationFrame(frame); },
    restyle() { if (width && height) { backdrop = graphBackdrop(width, height, dpr); dirty = true; } },
    destroy() { destroyed = true; cancelAnimationFrame(raf); observer.disconnect(); },
  };
}

/* ── Home banner: a quiet, live copy of the graph behind the welcome card ── */
let homeGraphScene = null, homeGraphLoadedAt = 0;
const HOME_GRAPH_MEMORIES = 650;
const HOME_GRAPH_REFRESH_MS = 60000;

/* Keep the newest memories and the hubs that still connect to them, so the banner stays light. */
function graphSample(data, limit) {
  const kept = new Set(data.nodes.filter(n => n.kind === 'memory').slice(0, limit).map(n => n.id));
  // An edge always starts at a memory (project and tag edges point to hubs; direct links join two memories).
  const edges = data.edges.filter(e => kept.has(e.source));
  const linked = new Set(edges.flatMap(e => [e.source, e.target]));
  const nodes = data.nodes.filter(n => kept.has(n.id) || (n.kind !== 'memory' && linked.has(n.id)));
  const ids = new Set(nodes.map(n => n.id));
  return { ...data, nodes, edges: edges.filter(e => ids.has(e.source) && ids.has(e.target)) };
}
async function loadHomeGraph() {
  const canvas = document.getElementById('home-graph');
  if (!canvas || currentPage !== 'overview') return;
  if (homeGraphScene && Date.now() - homeGraphLoadedAt < HOME_GRAPH_REFRESH_MS) { homeGraphScene.resume(); return; }
  try {
    const data = await api('/api/graph?project_id=');
    if (currentPage !== 'overview') return;
    if (homeGraphScene) homeGraphScene.destroy();
    homeGraphLoadedAt = Date.now();
    const aspect = Math.max(1, Math.min(3.2, (canvas.clientWidth || 800) / Math.max(1, canvas.clientHeight || 220)));
    homeGraphScene = createGraphScene(canvas, null, graphSample(data, HOME_GRAPH_MEMORIES), {
      embedded: true, page: 'overview',
      settings: { ...GRAPH_DEFAULTS, colorBy: 'source', center: 0.5, repel: 1.2, distance: 30, nodeSize: 1.15, aspect },
    });
  } catch (error) { /* the banner simply stays plain if the graph cannot load */ }
}
