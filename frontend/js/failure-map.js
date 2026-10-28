// Failure propagation map: deployment components as 3D modules whose signal lamps replay the failure.
// A 2D SVG view uses the same layout for small screens, reduced motion, or no WebGL.
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";
import { CSS2DRenderer, CSS2DObject } from "three/addons/CSS2DRenderer.js";

export const STATE_COLORS = { ok: "#3E8A5E", degraded: "#C08419", failed: "#C2362B" };
export const STATE_TEXT = { ok: "Healthy", degraded: "Degraded", failed: "Failed" };

// Fixed positions follow the deployment flow left to right; dependencies branch off the application.
const LAYOUT = {
  pipeline: [-9.1, 0], build: [-6.5, 0], registry: [-3.9, 0], orchestrator: [-1.3, 0],
  container: [1.3, 0], app: [3.9, 0], config: [2.6, -3.4], database: [6.6, -1.8], "load-balancer": [6.6, 1.8],
};

export function nodeStateAt(node, t) {
  let s = "ok";
  for (const c of node.changes) if (c.t <= t) s = c.state;
  return s;
}

export function layout(nodes) {
  const pts = nodes.map((n) => LAYOUT[n.id] || [0, 0]);
  const xs = pts.map((p) => p[0]), zs = pts.map((p) => p[1]);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cz = (Math.min(...zs) + Math.max(...zs)) / 2;
  const out = {};
  nodes.forEach((n, i) => { out[n.id] = [pts[i][0] - cx, pts[i][1] - cz]; });
  return { pos: out, width: Math.max(...xs) - Math.min(...xs), depth: Math.max(...zs) - Math.min(...zs) };
}

export function webglAvailable() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch { return false; }
}

export class FailureMap3D {
  constructor(container, map, { onSelect } = {}) {
    this.container = container;
    this.map = map;
    this.onSelect = onSelect;
    this.t = 0;
    this.selected = null;
    this.pulses = [];
    this.disposed = false;
    this._build();
    this._loop = this._loop.bind(this);
    this._raf = requestAnimationFrame(this._loop);
  }

  _build() {
    const { clientWidth: w, clientHeight: h } = this.container;
    const scene = (this.scene = new THREE.Scene());
    scene.background = new THREE.Color("#F6F8FA");

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(w, h);
    this.renderer.domElement.setAttribute("aria-hidden", "true");
    this.container.appendChild(this.renderer.domElement);

    this.labels = new CSS2DRenderer();
    this.labels.setSize(w, h);
    this.labels.domElement.className = "map-labels";
    this.container.appendChild(this.labels.domElement);

    const { pos, width, depth } = layout(this.map.nodes);
    this.pos = pos;
    this.camera = new THREE.PerspectiveCamera(32, w / h, 0.1, 200);
    this._frame(width, depth);

    this.controls = new OrbitControls(this.camera, this.labels.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.08;
    this.controls.minPolarAngle = 0.25;
    this.controls.maxPolarAngle = 1.25;
    this.controls.minDistance = 6;
    this.controls.maxDistance = 45;
    this.controls.enablePan = false;

    scene.add(new THREE.HemisphereLight(0xffffff, 0xd8dee4, 1.6));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(4, 10, 7);
    scene.add(sun);

    const grid = new THREE.GridHelper(40, 40, 0xd3dae1, 0xe4e9ee);
    grid.position.y = -0.001;
    scene.add(grid);

    this.nodeObjs = {};
    const body = new THREE.BoxGeometry(1.7, 0.5, 1.15);
    const lampGeo = new THREE.BoxGeometry(1.25, 0.08, 0.7);
    const edgeGeo = new THREE.EdgesGeometry(body);
    for (const n of this.map.nodes) {
      const [x, z] = pos[n.id];
      const g = new THREE.Group();
      g.position.set(x, 0.25, z);
      const mesh = new THREE.Mesh(body, new THREE.MeshStandardMaterial({ color: "#E7ECF0", roughness: 0.85 }));
      mesh.userData.nodeId = n.id;
      const outline = new THREE.LineSegments(edgeGeo, new THREE.LineBasicMaterial({ color: "#8E9BA8" }));
      const lamp = new THREE.Mesh(lampGeo, new THREE.MeshStandardMaterial({ color: STATE_COLORS.ok, emissive: STATE_COLORS.ok, emissiveIntensity: 0.25, roughness: 0.5 }));
      lamp.position.y = 0.29;
      g.add(mesh, outline, lamp);

      const ring = new THREE.Mesh(new THREE.RingGeometry(1.25, 1.36, 64), new THREE.MeshBasicMaterial({ color: STATE_COLORS.failed, side: THREE.DoubleSide, transparent: true }));
      ring.rotation.x = -Math.PI / 2;
      ring.position.set(x, 0.01, z);
      ring.visible = false;
      scene.add(ring);

      const el = document.createElement("div");
      el.className = "map-label";
      el.innerHTML = `<span class="map-label-name"></span><span class="map-label-state"></span>`;
      el.querySelector(".map-label-name").textContent = n.label;
      const label = new CSS2DObject(el);
      label.position.set(0, 0.85, 0);
      g.add(label);
      scene.add(g);
      this.nodeObjs[n.id] = { group: g, mesh, outline, lamp, ring, el, state: null, bump: 0 };
    }

    this.edgeObjs = [];
    for (const e of this.map.edges) {
      const a = pos[e.from], b = pos[e.to];
      const from = new THREE.Vector3(a[0], 0.05, a[1]), to = new THREE.Vector3(b[0], 0.05, b[1]);
      const len = from.distanceTo(to);
      const bar = new THREE.Mesh(new THREE.BoxGeometry(len, 0.04, 0.07), new THREE.MeshBasicMaterial({ color: "#AEB9C4" }));
      bar.position.copy(from).lerp(to, 0.5);
      bar.rotation.y = -Math.atan2(to.z - from.z, to.x - from.x);
      scene.add(bar);
      this.edgeObjs.push({ ...e, from3: from, to3: to, bar });
    }

    this.raycaster = new THREE.Raycaster();
    this.pointer = new THREE.Vector2();
    let down = null;
    this.labels.domElement.addEventListener("pointerdown", (ev) => { down = [ev.clientX, ev.clientY]; });
    this.labels.domElement.addEventListener("pointerup", (ev) => {
      if (!down || Math.hypot(ev.clientX - down[0], ev.clientY - down[1]) > 5) return;
      const r = this.renderer.domElement.getBoundingClientRect();
      this.pointer.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
      this.raycaster.setFromCamera(this.pointer, this.camera);
      const hit = this.raycaster.intersectObjects(Object.values(this.nodeObjs).map((o) => o.mesh))[0];
      if (hit) this.select(hit.object.userData.nodeId, true);
    });
    this.labels.domElement.addEventListener("pointermove", (ev) => {
      const r = this.renderer.domElement.getBoundingClientRect();
      this.pointer.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
      this.raycaster.setFromCamera(this.pointer, this.camera);
      const hit = this.raycaster.intersectObjects(Object.values(this.nodeObjs).map((o) => o.mesh))[0];
      this.labels.domElement.style.cursor = hit ? "pointer" : "grab";
    });

    this._resize = new ResizeObserver(() => this.resize());
    this._resize.observe(this.container);
    this.setTime(0, false);
  }

  _frame(width, depth) {
    const aspect = this.camera.aspect;
    const span = Math.max(width + 2.6, (depth + 3) * aspect);
    const dist = span / (2 * Math.tan((this.camera.fov * Math.PI) / 360) * aspect) * 0.92;
    this.camera.position.set(0, dist * 0.55, dist * 0.78);
    this.camera.lookAt(0, 0, 0);
  }

  resize() {
    const { clientWidth: w, clientHeight: h } = this.container;
    if (!w || !h) return;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
    this.labels.setSize(w, h);
  }

  select(id, fromClick = false) {
    this.selected = id;
    for (const [nid, o] of Object.entries(this.nodeObjs)) {
      o.outline.material.color.set(nid === id ? "#2B5D8A" : "#8E9BA8");
      o.el.classList.toggle("is-selected", nid === id);
    }
    if (fromClick && this.onSelect) this.onSelect(id);
  }

  setTime(t, animate = true) {
    const prev = this.t;
    this.t = t;
    for (const n of this.map.nodes) {
      const o = this.nodeObjs[n.id];
      const s = nodeStateAt(n, t);
      if (s !== o.state) {
        if (animate && o.state !== null && t > prev) this._spawnPulses(n.id, s);
        o.state = s;
        const c = STATE_COLORS[s];
        o.lamp.material.color.set(c);
        o.lamp.material.emissive.set(c);
        o.lamp.material.emissiveIntensity = s === "ok" ? 0.2 : 0.55;
        o.el.querySelector(".map-label-state").textContent = STATE_TEXT[s];
        o.el.dataset.state = s;
        if (animate && s !== "ok") o.bump = 1;
      }
      const rootShown = n.root_cause && (n.changes.length === 0 || n.changes[0].t <= t);
      o.ring.visible = !!rootShown;
      o.el.classList.toggle("is-root", !!rootShown);
    }
    for (const e of this.edgeObjs) {
      const a = this.nodeObjs[e.from].state, b = this.nodeObjs[e.to].state;
      e.bar.material.color.set(a !== "ok" && b !== "ok" ? "#D9A39C" : "#AEB9C4");
    }
  }

  _spawnPulses(targetId, state) {
    for (const e of this.edgeObjs) {
      let start = null, end = null;
      if (e.to === targetId && this.nodeObjs[e.from].state !== "ok") { start = e.from3; end = e.to3; }
      else if (e.from === targetId && this.nodeObjs[e.to].state !== "ok") { start = e.to3; end = e.from3; }
      if (!start) continue;
      const m = new THREE.Mesh(new THREE.SphereGeometry(0.14, 16, 12), new THREE.MeshBasicMaterial({ color: STATE_COLORS[state] }));
      m.position.copy(start);
      this.scene.add(m);
      this.pulses.push({ mesh: m, start: start.clone(), end: end.clone(), life: 0 });
    }
  }

  _loop(now) {
    if (this.disposed) return;
    const dt = this._last ? Math.min(0.05, (now - this._last) / 1000) : 0;
    this._last = now;
    if (this.tick) this.tick(dt);
    for (const p of this.pulses) {
      p.life += dt / 0.7;
      p.mesh.position.lerpVectors(p.start, p.end, Math.min(1, p.life)).setY(0.12 + Math.sin(Math.min(1, p.life) * Math.PI) * 0.35);
    }
    this.pulses = this.pulses.filter((p) => {
      if (p.life < 1) return true;
      this.scene.remove(p.mesh); p.mesh.geometry.dispose(); p.mesh.material.dispose();
      return false;
    });
    for (const o of Object.values(this.nodeObjs)) {
      if (o.bump > 0) {
        o.bump = Math.max(0, o.bump - dt * 2.5);
        o.group.scale.setScalar(1 + Math.sin(o.bump * Math.PI) * 0.08);
      }
    }
    this.controls.update();
    this.renderer.render(this.scene, this.camera);
    this.labels.render(this.scene, this.camera);
    this._raf = requestAnimationFrame(this._loop);
  }

  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this._raf);
    this._resize.disconnect();
    this.controls.dispose();
    this.scene.traverse((o) => { o.geometry?.dispose(); o.material?.dispose?.(); });
    this.renderer.dispose();
    this.container.innerHTML = "";
  }
}

// ---------- 2D view ----------
export class FailureMap2D {
  constructor(container, map, { onSelect } = {}) {
    this.container = container;
    this.map = map;
    this.onSelect = onSelect;
    this.selected = null;
    const { pos: flat } = layout(map.nodes);
    // On narrow screens the flow runs top to bottom so labels stay readable.
    const vertical = container.clientWidth < 560;
    container.classList.toggle("is-vertical", vertical);
    const pos = vertical ? Object.fromEntries(Object.entries(flat).map(([k, [x, z]]) => [k, [z * 1.1, x * 0.72]])) : flat;
    const xs = Object.values(pos).map((p) => p[0]), zs = Object.values(pos).map((p) => p[1]);
    const S = 46, W = 120, H = 54, pad = 24;
    const minX = Math.min(...xs), minZ = Math.min(...zs);
    const width = (Math.max(...xs) - minX) * S + W + pad * 2;
    const height = (Math.max(...zs) - minZ) * S + H + pad * 2 + 18;
    const px = (id) => [(pos[id][0] - minX) * S + pad + W / 2, (pos[id][1] - minZ) * S + pad + H / 2 + 18];
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("class", "map2d");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Failure map of deployment components");
    for (const e of map.edges) {
      const [x1, y1] = px(e.from), [x2, y2] = px(e.to);
      const l = document.createElementNS(NS, "line");
      Object.entries({ x1, y1, x2, y2, class: "map2d-edge" }).forEach(([k, v]) => l.setAttribute(k, v));
      svg.appendChild(l);
    }
    this.nodes = {};
    for (const n of map.nodes) {
      const [cx, cy] = px(n.id);
      const g = document.createElementNS(NS, "g");
      g.setAttribute("class", "map2d-node");
      g.setAttribute("tabindex", "0");
      g.setAttribute("role", "button");
      g.setAttribute("aria-label", n.label);
      g.innerHTML = `<rect class="map2d-root" x="${cx - W / 2 - 5}" y="${cy - H / 2 - 5}" width="${W + 10}" height="${H + 10}" rx="5"/>
        <rect class="map2d-body" x="${cx - W / 2}" y="${cy - H / 2}" width="${W}" height="${H}" rx="3"/>
        <rect class="map2d-lamp" x="${cx - W / 2 + 10}" y="${cy - H / 2 + 9}" width="10" height="10" rx="2"/>
        <text class="map2d-name" x="${cx - W / 2 + 26}" y="${cy - H / 2 + 18}"></text>
        <text class="map2d-state" x="${cx - W / 2 + 10}" y="${cy + H / 2 - 12}"></text>`;
      g.querySelector(".map2d-name").textContent = n.label;
      const pick = () => { this.select(n.id); this.onSelect && this.onSelect(n.id); };
      g.addEventListener("click", pick);
      g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
      svg.appendChild(g);
      this.nodes[n.id] = g;
    }
    container.appendChild(svg);
    this.setTime(0);
  }

  select(id) {
    this.selected = id;
    for (const [nid, g] of Object.entries(this.nodes)) g.classList.toggle("is-selected", nid === id);
  }

  setTime(t) {
    for (const n of this.map.nodes) {
      const g = this.nodes[n.id], s = nodeStateAt(n, t);
      g.dataset.state = s;
      g.querySelector(".map2d-state").textContent = STATE_TEXT[s];
      g.classList.toggle("is-root", n.root_cause && (n.changes.length === 0 || n.changes[0].t <= t));
    }
  }

  dispose() { this.container.classList.remove("is-vertical"); this.container.innerHTML = ""; }
}
