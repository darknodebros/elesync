"""The knowledge-graph force layout is plain JavaScript with no DOM, so Node can run it."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

LAYOUT = Path(__file__).resolve().parents[1] / "elesync" / "static" / "dashboard-graph-layout.js"

SCRIPT = r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[1], 'utf8') + ';globalThis.createGraphLayout = createGraphLayout;');
let seed = 3;
const random = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
const nodes = [];
const make = (cluster, charge, mass) => {
  const node = { index: nodes.length, cluster, x: (random() - 0.5) * 200, y: (random() - 0.5) * 200,
                 vx: 0, vy: 0, charge, mass, pinned: false };
  nodes.push(node); return node;
};
const hubs = [make(0, 3, 3), make(1, 3, 3)];
const edges = [];
for (let c = 0; c < 2; c++) for (let i = 0; i < 80; i++) edges.push({ a: make(c, 1, 1), b: hubs[c], kind: 'project' });
const loose = [...Array(10)].map(() => make(-1, 1, 1));
const layout = createGraphLayout(nodes, edges, { repel: 1, link: 1, center: 1, distance: 30 });
let ticks = 0;
while (layout.tick()) ticks++;
const centre = c => {
  const members = nodes.filter(n => n.cluster === c);
  return { x: members.reduce((s, n) => s + n.x, 0) / members.length, y: members.reduce((s, n) => s + n.y, 0) / members.length };
};
const a = centre(0), b = centre(1);
const spoke = edges.map(e => Math.hypot(e.a.x - e.b.x, e.a.y - e.b.y));
console.log(JSON.stringify({
  ticks, settled: layout.settled, finite: nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y)),
  meanSpoke: spoke.reduce((s, d) => s + d, 0) / spoke.length,
  centreGap: Math.hypot(a.x - b.x, a.y - b.y),
  looseRadius: Math.max(...loose.map(n => Math.hypot(n.x, n.y))),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestGraphLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        done = subprocess.run(
            ["node", "-e", SCRIPT, str(LAYOUT)], capture_output=True, text=True, timeout=120
        )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.result = json.loads(done.stdout)

    def test_layout_settles_with_finite_positions(self):
        self.assertTrue(self.result["settled"])
        self.assertTrue(self.result["finite"])
        self.assertGreater(self.result["ticks"], 10)

    def test_connected_memories_gather_around_their_hub(self):
        # Springs pull members to their hub, while repulsion keeps them from collapsing onto it.
        self.assertLess(self.result["meanSpoke"], 400)
        self.assertGreater(self.result["meanSpoke"], 5)

    def test_separate_communities_do_not_collapse_together(self):
        self.assertGreater(self.result["centreGap"], 20)

    def test_unconnected_memories_stay_bounded(self):
        self.assertLess(self.result["looseRadius"], 2000)


SCENE_SCRIPT = r"""
const fs = require('fs');
const noop = () => {};
const ctx = new Proxy({}, {
  get: (target, key) => key in target ? target[key]
    : key === 'createRadialGradient' ? () => ({ addColorStop: noop })
    : key === 'measureText' ? () => ({ width: 10 }) : noop,
  set: (target, key, value) => { target[key] = value; return true; },
});
const makeCanvas = () => ({
  getContext: () => ctx, clientWidth: 800, clientHeight: 500, width: 0, height: 0, style: {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 500 }),
  setAttribute: noop, focus: noop, setPointerCapture: noop,
});
globalThis.document = { createElement: makeCanvas, documentElement: { dataset: {} }, getElementById: () => null, querySelector: () => null };
globalThis.window = globalThis;
globalThis.matchMedia = () => ({ matches: false });
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = noop;
globalThis.Path2D = class { moveTo() {} lineTo() {} };
globalThis.localStorage = { getItem: () => null, setItem: noop };
Object.assign(globalThis, { currentPage: 'graph', devicePixelRatio: 1, go: noop, openDetail: noop, api: async () => ({}) });
const source = process.argv.slice(1).map(file => fs.readFileSync(file, 'utf8')).join('\n');
eval(source + ';globalThis.createGraphScene = createGraphScene; globalThis.createAtlasScene = createAtlasScene; globalThis.GRAPH_DEFAULTS = GRAPH_DEFAULTS; globalThis.graphSettings = graphSettings;');

const data = {
  nodes: [
    { id: 'project:p', kind: 'project', label: 'Roadmap' },
    { id: 'tag:t', kind: 'tag', label: 'travel' },
    { id: 'a', kind: 'memory', source: 'claude', label: 'A', tags: ['travel'], project_ids: ['p'] },
    { id: 'b', kind: 'memory', source: 'whatsapp', label: 'B', tags: ['travel'], project_ids: ['p'] },
    { id: 'c', kind: 'memory', source: 'manual', label: 'C', tags: [], project_ids: [] },
  ],
  edges: [
    { source: 'a', target: 'project:p', kind: 'project' }, { source: 'b', target: 'project:p', kind: 'project' },
    { source: 'a', target: 'tag:t', kind: 'tag' }, { source: 'b', target: 'tag:t', kind: 'tag' },
    { source: 'a', target: 'b', kind: 'link', relation: 'caused_by' },
  ],
};
const panel = { replaceChildren: noop, append: noop, textContent: '' };
const full = createGraphScene(makeCanvas(), panel, data);
full.select(full.nodes[2]); full.find('roadmap'); full.zoomBy(1.2); full.restyle(); full.destroy();
const home = createGraphScene(makeCanvas(), null, data, { embedded: true, page: 'overview', settings: { ...GRAPH_DEFAULTS, aspect: 2 } });
home.select(home.nodes[2]); home.destroy();
const atlas = createAtlasScene(makeCanvas(), panel, data);
atlas.select(atlas.nodes[0]); atlas.find('a'); atlas.zoomBy(1.1); atlas.toggleGroup(atlas.groups[0].key, false);
atlas.restyle(); atlas.animate(); atlas.fit(); atlas.destroy();
graphSettings.colorBy = 'project';
const byProject = createAtlasScene(makeCanvas(), panel, data);
const projectGroups = byProject.groups.map(g => g.label); byProject.destroy();
const empty = createAtlasScene(makeCanvas(), panel, { nodes: [], edges: [] }); empty.fit(); empty.destroy();
console.log(JSON.stringify({ nodes: full.nodes.length, groups: full.groups.map(g => g.label), atlasOrbs: atlas.nodes.length, projectGroups }));
"""


# A well-connected vault (every memory on one of three projects) settles into a round cloud. On a wide,
# short Home banner it must still span the width; the full graph page keeps its true shape.
BANNER_SCRIPT = r"""
const fs = require('fs');
const noop = () => {};
const ctx = new Proxy({}, {
  get: (target, key) => key in target ? target[key]
    : key === 'createRadialGradient' ? () => ({ addColorStop: noop })
    : key === 'measureText' ? () => ({ width: 10 }) : noop,
  set: (target, key, value) => { target[key] = value; return true; },
});
const banner = () => ({
  getContext: () => ctx, clientWidth: 1100, clientHeight: 220, width: 0, height: 0, style: {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 1100, height: 220 }),
  setAttribute: noop, focus: noop, setPointerCapture: noop,
});
let pending = null;
globalThis.document = { createElement: banner, documentElement: { dataset: {} }, getElementById: () => null, querySelector: () => null };
globalThis.window = globalThis;
globalThis.matchMedia = () => ({ matches: false });
globalThis.ResizeObserver = class { observe() {} disconnect() {} };
globalThis.requestAnimationFrame = callback => { pending = callback; return 1; };
globalThis.cancelAnimationFrame = noop;
globalThis.Path2D = class { moveTo() {} lineTo() {} };
globalThis.localStorage = { getItem: () => null, setItem: noop };
Object.assign(globalThis, { currentPage: 'overview', devicePixelRatio: 1, go: noop, openDetail: noop, api: async () => ({}) });
const source = process.argv.slice(1).map(file => fs.readFileSync(file, 'utf8')).join('\n');
eval(source + ';globalThis.createGraphScene = createGraphScene; globalThis.GRAPH_DEFAULTS = GRAPH_DEFAULTS;');

function vault(size) {
  const nodes = [0, 1, 2].map(p => ({ id: 'project:' + p, kind: 'project', label: 'P' + p }));
  const edges = [];
  for (let i = 0; i < size; i++) {
    nodes.push({ id: 'm' + i, kind: 'memory', source: 'claude', label: 'M' + i });
    edges.push({ source: 'm' + i, target: 'project:' + (i % 3), kind: 'project' });
  }
  return { nodes, edges };
}
// Returns the share of the banner width the middle 90% of memories cover, and how far their middle is from centre.
function measure(data, embedded) {
  const scene = createGraphScene(banner(), null, data, {
    embedded, page: 'overview', settings: { ...GRAPH_DEFAULTS, colorBy: 'source', aspect: 5 },
  });
  for (let i = 0; i < 600 && pending; i++) { const run = pending; pending = null; run(performance.now() + i * 16); }
  const xs = scene.nodes.filter(n => n.kind === 'memory').map(n => n.sx).sort((a, b) => a - b);
  scene.destroy();
  const at = q => xs[Math.floor(q * (xs.length - 1))];
  return { spread: (at(0.95) - at(0.05)) / 1100, offCentre: Math.abs((at(0.95) + at(0.05)) / 2 - 550) / 1100 };
}
// 150 memories are still settling on the first frame; 24 settle during the warm-up, before any frame runs.
console.log(JSON.stringify({
  banner: measure(vault(150), true), small: measure(vault(24), true), page: measure(vault(150), false),
}));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestHomeBannerFillsWidth(unittest.TestCase):
    def test_round_vault_still_spans_the_banner(self):
        graph = LAYOUT.with_name("dashboard-graph.js")
        done = subprocess.run(
            ["node", "-e", BANNER_SCRIPT, str(LAYOUT), str(graph)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        for name in ("banner", "small"):
            self.assertGreater(result[name]["spread"], 0.6, result)
            self.assertLess(result[name]["spread"], 1.0, result)
            self.assertLess(result[name]["offCentre"], 0.1, result)
        self.assertLess(result["page"]["spread"], result["banner"]["spread"] * 0.6, result)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class TestGraphSceneRuns(unittest.TestCase):
    """Runs the real scene code with a stubbed canvas, so a runtime error in it cannot ship unnoticed."""

    def test_full_and_embedded_scenes_build_and_respond(self):
        graph = LAYOUT.with_name("dashboard-graph.js")
        done = subprocess.run(
            [
                "node",
                "-e",
                SCENE_SCRIPT,
                str(LAYOUT),
                str(graph),
                str(graph.with_name("dashboard-graph-atlas.js")),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result["nodes"], 5)
        self.assertIn("claude", result["groups"])
        self.assertIn("whatsapp", result["groups"])
        self.assertEqual(result["atlasOrbs"], 3)  # claude, whatsapp, manual
        self.assertIn("Roadmap", result["projectGroups"])
