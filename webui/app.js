// BitTorrent swarm dashboard — a Tutuca SPA that polls the collector's /summary
// and renders the same views as piece_map.py: per-node piece ownership, per-piece
// availability, an availability histogram, the "copies of the dataset" summary,
// and per-file replication. No build step: the collector serves this file and the
// vendored ./tutuca.js next to it.
//
// Aggregation lives server-side (swarm_stats, via /summary) so the web view and
// the terminal piece_map never drift; this file only does presentation —
// bucketing pieces into columns and colouring cells.
import { component, html, tutuca } from "./tutuca.js";

const SUMMARY_URL = "/summary"; // same origin: the collector serves both
const POLL_MS = 1000; // refresh once a second
const MAX_COLS = 120; // widest piece map; more pieces than this get bucketed

// --- pure presentation helpers -------------------------------------------------

function human(n) {
  const units = ["B", "KiB", "MiB", "GiB"];
  let i = 0;
  n = Number(n) || 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(1)} ${units[i]}`;
}

// Bucket a piece bitfield into `cols` columns, returning the held-fraction of
// each column (mirrors piece_map.render_bits). One column per piece when they fit.
function bucketFracs(bits, numPieces, cols) {
  if (cols >= numPieces) return bits.map((b) => (b ? 1 : 0));
  const out = [];
  for (let c = 0; c < cols; c++) {
    const lo = Math.floor((c * numPieces) / cols);
    const hi = Math.floor(((c + 1) * numPieces) / cols);
    const seg = bits.slice(lo, hi);
    const have = seg.reduce((a, b) => a + (b ? 1 : 0), 0);
    out.push(seg.length ? have / seg.length : 0);
  }
  return out;
}

// Bucket per-piece holder counts into `cols` columns, taking the worst (min)
// availability in each bucket — same rule piece_map.render_avail uses.
function bucketAvail(avail, numPieces, cols) {
  if (cols >= numPieces) return avail.slice();
  const out = [];
  for (let c = 0; c < cols; c++) {
    const lo = Math.floor((c * numPieces) / cols);
    const hi = Math.floor(((c + 1) * numPieces) / cols);
    out.push(Math.min(...avail.slice(lo, hi)));
  }
  return out;
}

function cellForFrac(frac) {
  if (frac <= 0) return { style: "background:#21262d", title: "missing" };
  if (frac >= 1) return { style: "background:#3fb950", title: "complete" };
  const a = (0.25 + 0.75 * frac).toFixed(3);
  return { style: `background:rgba(63,185,80,${a})`, title: `${Math.round(frac * 100)}% of bucket` };
}

function cellForAvail(count, maxNodes) {
  if (count <= 0) return { style: "background:#f85149", title: "0 holders — MISSING" };
  const t = maxNodes > 0 ? Math.min(1, count / maxNodes) : 1;
  const a = (0.3 + 0.7 * t).toFixed(3);
  return { style: `background:rgba(63,185,80,${a})`, title: `${count} holder(s)` };
}

function histogramLines(avail, nodes) {
  const lines = [];
  for (let k = nodes; k >= 0; k--) {
    const pieces = avail.filter((a) => a === k).length;
    if (pieces) {
      const tag = k === 0 ? "  ← MISSING from swarm" : "";
      lines.push(`${k} node(s): ${pieces} pieces${tag}`);
    }
  }
  return lines;
}

// --- components ----------------------------------------------------------------

// One coloured square in a piece map / availability row. The background colour is
// precomputed (cellForFrac / cellForAvail); the component just paints it.
const Cell = component({
  name: "Cell",
  fields: { style: "", title: "" },
  view: html`<span class="cell" :style=".style" :title=".title"></span>`,
});

// One node's ownership line: label/role/percent, the piece map, and bytes stored.
const NodeRow = component({
  name: "NodeRow",
  fields: { label: "", role: "", have: 0, numPieces: 0, stored: 0, cells: [] },
  methods: {
    headText() {
      const pct = this.numPieces ? (100 * this.have) / this.numPieces : 0;
      return `n${this.label} ${this.role.padEnd(5)} ${pct.toFixed(1).padStart(5)}%  ${this.have}/${this.numPieces}`;
    },
    storedText() {
      return human(this.stored);
    },
  },
  statics: {
    fromData(r, numPieces, cols) {
      const cells = bucketFracs(r.bits, numPieces, cols).map((f) => Cell.make(cellForFrac(f)));
      return this.make({
        label: r.label,
        role: r.role,
        have: r.have,
        numPieces,
        stored: r.stored,
        cells,
      });
    },
  },
  view: html`<div class="noderow">
    <span class="rowlabel" @text="$headText"></span>
    <span class="map"><x render-each=".cells"></x></span>
    <span class="stored" @text="$storedText"></span>
  </div>`,
});

// One file's replication line.
const FileRow = component({
  name: "FileRow",
  fields: { name: "", size: 0, fullCopies: 0, reconCopies: 0, holders: "", partial: "" },
  methods: {
    sizeText() {
      return human(this.size);
    },
    holdText() {
      return this.partial ? `${this.holders}    partial: ${this.partial}` : this.holders;
    },
  },
  statics: {
    fromData(f, torrentName) {
      let disp = f.path;
      const prefix = `${torrentName}/`;
      if (torrentName && disp.startsWith(prefix)) disp = disp.slice(prefix.length);
      const holders = f.full_holders.length ? f.full_holders.map((l) => `n${l}`).join(",") : "-";
      const partial = f.partial.map((p) => `n${p.label}=${Math.round(p.pct)}%`).join(" ");
      return this.make({
        name: disp,
        size: f.size,
        fullCopies: f.full_copies,
        reconCopies: f.recon_copies,
        holders,
        partial,
      });
    },
  },
  view: html`<div class="filerow">
    <span class="fname" @text=".name"></span>
    <span class="fnum" @text="$sizeText"></span>
    <span class="fnum" @text=".fullCopies"></span>
    <span class="fnum" @text=".reconCopies"></span>
    <span class="fhold" @text="$holdText"></span>
  </div>`,
});

// One torrent panel: meta, the copies summary, per-node maps + availability row,
// the histogram, and the per-file table.
const Torrent = component({
  name: "Torrent",
  fields: {
    name: "",
    infoHash: "",
    numPieces: 0,
    pieceLength: 0,
    totalSize: 0,
    nodesSeen: 0,
    fullCopies: 0,
    minAvail: 0,
    redundancy: 0,
    fullyAvailable: false,
    totalStored: 0,
    rows: [],
    availCells: [],
    histLines: [],
    files: [],
  },
  methods: {
    metaText() {
      return `${human(this.totalSize)} · ${this.numPieces} pieces × ${human(this.pieceLength)}`;
    },
    subMetaText() {
      return `info ${this.infoHash.slice(0, 16)}…  ·  ${this.nodesSeen} node(s) reporting`;
    },
    redundancyText() {
      return `${this.redundancy.toFixed(2)}×`;
    },
    storedText() {
      return human(this.totalStored);
    },
    availabilityText() {
      return this.fullyAvailable ? "complete" : "INCOMPLETE";
    },
    availabilityClass() {
      return this.fullyAvailable ? "num ok" : "num bad";
    },
    hasFiles() {
      return this.files.size > 0;
    },
  },
  statics: {
    fromData(t) {
      const cols = Math.min(t.num_pieces, MAX_COLS);
      const rows = t.rows.map((r) => NodeRow.Class.fromData(r, t.num_pieces, cols));
      const availCells = bucketAvail(t.avail, t.num_pieces, cols).map((c) =>
        Cell.make(cellForAvail(c, t.nodes_seen)),
      );
      const files = t.files.map((f) => FileRow.Class.fromData(f, t.name));
      const s = t.summary;
      return this.make({
        name: t.name,
        infoHash: t.info_hash,
        numPieces: t.num_pieces,
        pieceLength: t.piece_length,
        totalSize: t.total_size,
        nodesSeen: t.nodes_seen,
        fullCopies: s.full_copies,
        minAvail: s.min_avail,
        redundancy: s.redundancy,
        fullyAvailable: s.fully_available,
        totalStored: s.total_stored,
        rows,
        availCells,
        histLines: histogramLines(t.avail, t.nodes_seen),
        files,
      });
    },
  },
  view: html`<section class="torrent">
    <h2><span @text=".name"></span> <span class="muted" @text="$metaText"></span></h2>
    <div class="muted small" @text="$subMetaText"></div>

    <div class="summary">
      <div class="stat"><span class="num" @text=".fullCopies"></span><span class="lbl">full copies</span></div>
      <div class="stat"><span class="num" @text=".minAvail"></span><span class="lbl">complete copies (incl. partial)</span></div>
      <div class="stat"><span class="num" @text="$redundancyText"></span><span class="lbl">avg copies / piece</span></div>
      <div class="stat"><span :class="$availabilityClass" @text="$availabilityText"></span><span class="lbl">availability</span></div>
      <div class="stat"><span class="num" @text="$storedText"></span><span class="lbl">stored across swarm</span></div>
    </div>

    <h3>Per-node ownership</h3>
    <div class="rows">
      <x render-each=".rows"></x>
      <div class="noderow avail">
        <span class="rowlabel">availability  (#holders)</span>
        <span class="map"><x render-each=".availCells"></x></span>
        <span class="stored"></span>
      </div>
    </div>

    <h3>Availability histogram</h3>
    <ul class="hist"><li @each=".histLines"><x text="@value"></x></li></ul>

    <div @show="$hasFiles">
      <h3>Per-file replication</h3>
      <div class="filerow filehead">
        <span class="fname">file</span><span class="fnum">size</span>
        <span class="fnum">full</span><span class="fnum">recon</span>
        <span class="fhold">holders / partial</span>
      </div>
      <x render-each=".files"></x>
    </div>
  </section>`,
});

// Root: connection status, error banner, and one Torrent panel per torrent.
const Dashboard = component({
  name: "Dashboard",
  fields: { torrents: [], status: "connecting", error: "", ts: 0 },
  methods: {
    statusText() {
      return { live: "live", connecting: "connecting…", error: "disconnected" }[this.status] || this.status;
    },
    statusClass() {
      return `pill ${this.status}`;
    },
    updatedText() {
      return this.ts ? `updated ${new Date(this.ts * 1000).toLocaleTimeString()}` : "";
    },
    isEmpty() {
      return this.status === "live" && this.torrents.size === 0;
    },
  },
  receive: {
    init(ctx) {
      ctx.request("fetchSummary");
      return this;
    },
    tick(ctx) {
      ctx.request("fetchSummary");
      return this;
    },
  },
  response: {
    fetchSummary(res, err) {
      if (err) return this.setStatus("error").setError(String((err && err.message) || err));
      return this.setError("")
        .setStatus("live")
        .setTs(res.ts)
        .setTorrents(res.torrents.map((t) => Torrent.Class.fromData(t)));
    },
  },
  view: html`<div class="dash">
    <header class="topbar">
      <h1>BitTorrent swarm — piece map</h1>
      <div class="status">
        <span :class="$statusClass" @text="$statusText"></span>
        <span class="muted small" @text="$updatedText"></span>
      </div>
    </header>
    <div class="banner" @show="truthy? .error" @text=".error"></div>
    <div class="empty" @show="$isEmpty">
      No torrents reported yet. Start the tracker, the nodes, and assign torrents
      (see the README); this view updates on its own.
    </div>
    <x render-each=".torrents"></x>
  </div>`,
});

function main() {
  const app = tutuca("#app");
  const scope = app.registerComponents([Dashboard, Torrent, NodeRow, FileRow, Cell]);
  scope.registerRequestHandlers({
    async fetchSummary() {
      const r = await fetch(SUMMARY_URL, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
  });
  app.state.set(Dashboard.make({}));
  app.start();
  app.sendAtRoot("init");
  setInterval(() => app.sendAtRoot("tick"), POLL_MS);
}

// Only mount in the browser. The tutuca CLI imports this module for
// lint/render/test and must not trigger the live mount + polling.
if (typeof document !== "undefined" && document.getElementById("app")) {
  main();
}

// --- module exports for the tutuca CLI (lint / render / test) ------------------

export function getComponents() {
  return [Dashboard, Torrent, NodeRow, FileRow, Cell];
}

export function getRoot() {
  return Dashboard.make({});
}

export function getExamples() {
  const fixture = {
    name: "media",
    info_hash: "abcdef0123456789cafef00d",
    num_pieces: 8,
    piece_length: 262144,
    total_size: 2000000,
    nodes_seen: 2,
    avail: [2, 2, 1, 1, 0, 1, 2, 2],
    rows: [
      { label: "0", role: "seed", have: 8, stored: 2000000, bits: [1, 1, 1, 1, 1, 1, 1, 1] },
      { label: "1", role: "leech", have: 4, stored: 1000000, bits: [1, 1, 0, 0, 0, 0, 1, 1] },
    ],
    files: [
      {
        path: "media/clip.bin",
        size: 1000000,
        full_copies: 1,
        full_holders: ["0"],
        recon_copies: 1,
        partial: [{ label: "1", pct: 50 }],
      },
    ],
    summary: {
      full_copies: 1,
      full_holders: ["0"],
      min_avail: 0,
      redundancy: 1.375,
      fully_available: false,
      total_stored: 3000000,
    },
  };
  return {
    title: "Swarm piece map",
    description: "One torrent across two nodes, with a piece missing from the swarm.",
    items: [{ title: "Two nodes, a missing piece", value: Torrent.Class.fromData(fixture) }],
  };
}
