// BitTorrent swarm dashboard — a Tutuca SPA over the collector's tiered API.
//
// Two screens, hash-routed so each is a shareable URL and the back button works:
//   #            the Overview — one light row per dataset from /overview
//                (size, copy counts, a per-node "spread" strip, live activity).
//                No per-piece bitfields are fetched here, so it stays cheap to
//                poll no matter how many datasets/nodes there are.
//   #<info_hash> the drill-down — full detail for one dataset from
//                /torrent/<info_hash> (per-node piece maps, availability
//                histogram, per-file replication, copies summary).
//
// Aggregation lives server-side (swarm_stats) so the web view and the terminal
// piece_map never drift; this file only does presentation — colouring cells and
// formatting the numbers each endpoint already computed.
import { component, html, tutuca } from "./tutuca.js";

// Machine endpoints are namespaced under /api/ so they never collide with the
// SPA's page routes (/, /dataset/<hash>, /transfers, /nodes); the collector serves
// the app shell for any non-/api path so those routes deep-link and reload.
const OVERVIEW_URL = "/api/overview";
const DETAIL_URL = "/api/torrent/"; // + info_hash
const TRANSFERS_URL = "/api/transfers";
const NODES_URL = "/api/nodes";
const POLL_MS = 1000; // refresh once a second

// Which collector endpoint each screen polls. routeTo + tick look the request up
// here so adding a screen is one map entry plus its response handler.
const REQ_FOR_ROUTE = {
  list: "fetchOverview",
  detail: "fetchDetail",
  transfers: "fetchTransfers",
  nodes: "fetchNodes",
};

// --- pure presentation helpers -------------------------------------------------

function human(n) {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let i = 0;
  n = Number(n) || 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return i === 0 ? `${Math.round(n)} B` : `${n.toFixed(1)} ${units[i]}`;
}

function formatEta(secs) {
  if (secs == null) return "stalled";
  const s = Math.round(secs);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
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

// One node's tile in a dataset's spread strip: how much of the dataset it holds.
function cellForNode(label, frac) {
  const base = cellForFrac(frac);
  return { style: base.style, title: `n${label}: ${Math.round(frac * 100)}% of dataset` };
}

// Copy count colouring. No replication policy is configured, so this is relative,
// not an SLA verdict: red only when data is genuinely missing from the swarm
// (no holder for some piece), amber when present but not yet a single full copy,
// green once at least one complete copy exists.
function copiesClass(durableCopies, minAvail) {
  if (minAvail <= 0) return "num bad";
  if (durableCopies < 1) return "num warn";
  return "num ok";
}

function histogramLines(histogram) {
  return histogram.map((h) => {
    const tag = h.holders === 0 ? "  ← MISSING from swarm" : "";
    return `${h.holders} node(s): ${h.pieces} pieces${tag}`;
  });
}

// --- detail-view components (unchanged: backed by /torrent/<info_hash>) ---------

// One coloured square in a piece map / availability / spread row.
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
    fromData(r, numPieces) {
      const cells = r.cells.map((f) => Cell.make(cellForFrac(f)));
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
// the histogram, and the per-file table. This is the drill-down detail view.
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
      const rows = t.rows.map((r) => NodeRow.Class.fromData(r, t.num_pieces));
      const availCells = t.avail_cells.map((c) => Cell.make(cellForAvail(c, t.nodes_seen)));
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
        histLines: histogramLines(t.histogram),
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

// --- overview-view components (backed by /overview) -----------------------------

// One dataset row in the list. The whole row is an anchor to #<info_hash>, so a
// click is plain navigation (no event wiring) and the URL is shareable.
const DatasetRow = component({
  name: "DatasetRow",
  fields: {
    href: "#",
    name: "",
    subText: "",
    sizeText: "",
    durable: 0,
    copiesClass: "num",
    copiesSub: "",
    cells: [],
    stateText: "",
    stateClass: "dstate",
  },
  statics: {
    fromData(d) {
      const cells = d.spread.map((s) => Cell.make(cellForNode(s.label, s.frac)));
      const replicating = d.downloading > 0 || d.download_rate > 0;
      const stateText = replicating
        ? `replicating · ${d.downloading} node(s) · ▲${human(d.download_rate)}/s`
        : `complete · ${d.seeding} seed(s)`;
      return this.make({
        href: `/dataset/${encodeURIComponent(d.info_hash)}`,
        name: d.name,
        subText: `${d.info_hash.slice(0, 12)}…  ·  ${d.nodes_seen} nodes  ·  ${human(d.total_stored)} stored`,
        sizeText: human(d.total_size),
        durable: d.durable_copies,
        copiesClass: copiesClass(d.durable_copies, d.min_avail),
        copiesSub: `full ${d.full_copies} · rarest piece ×${d.min_avail}`,
        cells,
        stateText,
        stateClass: replicating ? "dstate replicating" : "dstate complete",
      });
    },
  },
  view: html`<a class="datasetrow" data-link="1" :href=".href">
    <span>
      <div class="dname" @text=".name"></div>
      <div class="dsub" @text=".subText"></div>
    </span>
    <span class="dsize" @text=".sizeText"></span>
    <span class="dcopies">
      <span :class=".copiesClass" @text=".durable"></span>
      <div class="dsub" @text=".copiesSub"></div>
    </span>
    <span class="map"><x render-each=".cells"></x></span>
    <span :class=".stateClass" @text=".stateText"></span>
  </a>`,
});

// One in-flight transfer (a (node, dataset) pair that isn't complete yet): a
// progress bar, percent, live rate and ETA. Backed by /transfers.
const TransferRow = component({
  name: "TransferRow",
  fields: {
    node: "",
    name: "",
    href: "#",
    pctText: "",
    barStyle: "",
    barClass: "pbar-fill",
    rateText: "",
    etaText: "",
    peersText: "",
  },
  statics: {
    fromData(tr) {
      const pct = Math.round(tr.progress * 100);
      const active = tr.download_rate > 0;
      return this.make({
        node: `n${tr.node}`,
        name: tr.name,
        href: `/dataset/${encodeURIComponent(tr.info_hash)}`,
        pctText: `${pct}%`,
        barStyle: `width:${pct}%`,
        barClass: active ? "pbar-fill" : "pbar-fill stalled",
        rateText: active ? `▼${human(tr.download_rate)}/s` : "—",
        etaText: formatEta(tr.eta),
        peersText: String(tr.num_peers),
      });
    },
  },
  view: html`<div class="transferrow">
    <a class="tlink" data-link="1" :href=".href" @text=".name"></a>
    <span @text=".node"></span>
    <span class="pbar"><span :class=".barClass" :style=".barStyle"></span></span>
    <span class="nnum" @text=".pctText"></span>
    <span class="nnum" @text=".rateText"></span>
    <span class="nnum" @text=".etaText"></span>
    <span class="nnum" @text=".peersText"></span>
  </div>`,
});

// One node's storage + activity line. Backed by /nodes.
const NodeStatRow = component({
  name: "NodeStatRow",
  fields: { label: "", datasetsText: "", storedText: "", dlText: "", ulText: "", peersText: "" },
  statics: {
    fromData(n) {
      return this.make({
        label: `n${n.label}`,
        datasetsText: `${n.complete}/${n.datasets}`,
        storedText: human(n.stored),
        dlText: n.download_rate ? `▼${human(n.download_rate)}/s` : "—",
        ulText: n.upload_rate ? `▲${human(n.upload_rate)}/s` : "—",
        peersText: String(n.num_peers),
      });
    },
  },
  view: html`<div class="noderow2">
    <span @text=".label"></span>
    <span class="nnum" @text=".datasetsText"></span>
    <span class="nnum" @text=".storedText"></span>
    <span class="nnum" @text=".dlText"></span>
    <span class="nnum" @text=".ulText"></span>
    <span class="nnum" @text=".peersText"></span>
  </div>`,
});

// Root: routes between the Overview, the per-dataset drill-down, Transfers and
// Nodes via the History API. It polls whichever endpoint the current screen needs.
const Dashboard = component({
  name: "Dashboard",
  fields: {
    route: "list", // "list" | "detail"
    status: "connecting",
    error: "",
    ts: 0,
    // overview (list) state
    datasets: [],
    totDatasets: 0,
    totStoredText: "0 B",
    totNodes: 0,
    totDlText: "0 B/s",
    replicatingText: "0",
    rarest: 0,
    rarestClass: "num",
    // detail state: 0 or 1 Torrent vm so render-each shows nothing when empty
    detail: [],
    // transfers screen
    transfers: [],
    transfersCount: 0,
    transfersDlText: "0 B/s",
    // nodes screen
    nodes: [],
    nodesCount: 0,
    nodesStoredText: "0 B",
  },
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
    isList() {
      return this.route === "list";
    },
    isDetail() {
      return this.route === "detail";
    },
    isTransfers() {
      return this.route === "transfers";
    },
    isNodes() {
      return this.route === "nodes";
    },
    isEmptyList() {
      return this.route === "list" && this.status === "live" && this.datasets.size === 0;
    },
    detailMissing() {
      return this.route === "detail" && this.status === "live" && this.detail.size === 0;
    },
    isEmptyTransfers() {
      return this.route === "transfers" && this.status === "live" && this.transfers.size === 0;
    },
    isEmptyNodes() {
      return this.route === "nodes" && this.status === "live" && this.nodes.size === 0;
    },
    // Nav-tab highlighting. The drill-down lives under the Overview tab.
    navOverview() {
      return this.route === "list" || this.route === "detail" ? "tab active" : "tab";
    },
    navTransfers() {
      return this.route === "transfers" ? "tab active" : "tab";
    },
    navNodes() {
      return this.route === "nodes" ? "tab active" : "tab";
    },
    // Apply a parsed route: switch screen and kick an immediate fetch so it
    // doesn't wait for the next tick. Off-screen data stays put (hidden), which
    // each is*/empty predicate guards against by checking the active route.
    routeTo(ctx, parsed) {
      ctx.request(REQ_FOR_ROUTE[parsed.route]);
      return this.setRoute(parsed.route).setError("");
    },
  },
  receive: {
    init(ctx) {
      return this.routeTo(ctx, matchRoute());
    },
    navigate(ctx) {
      return this.routeTo(ctx, matchRoute());
    },
    tick(ctx) {
      ctx.request(REQ_FOR_ROUTE[this.route]);
      return this;
    },
  },
  response: {
    fetchOverview(res, err) {
      if (err) return this.setStatus("error").setError(String((err && err.message) || err));
      const datasets = res.datasets
        .slice()
        // rarest (weakest-link) copies first, so the least-replicated float up.
        .sort((a, b) => a.durable_copies - b.durable_copies || a.name.localeCompare(b.name));
      const nodes = new Set();
      let stored = 0;
      let dl = 0;
      let replicating = 0;
      let rarest = datasets.length ? Infinity : 0;
      for (const d of res.datasets) {
        stored += d.total_stored;
        dl += d.download_rate;
        if (d.downloading > 0 || d.download_rate > 0) replicating++;
        rarest = Math.min(rarest, d.durable_copies);
        for (const s of d.spread) nodes.add(s.label);
      }
      return this.setError("")
        .setStatus("live")
        .setTs(res.ts)
        .setDatasets(datasets.map((d) => DatasetRow.Class.fromData(d)))
        .setTotDatasets(datasets.length)
        .setTotStoredText(human(stored))
        .setTotNodes(nodes.size)
        .setTotDlText(`${human(dl)}/s`)
        .setReplicatingText(String(replicating))
        .setRarest(rarest)
        .setRarestClass(datasets.length ? copiesClass(rarest, rarest) : "num");
    },
    fetchDetail(res, err) {
      // A 404 (dataset no longer reported) surfaces as an error; show the
      // "not found" notice rather than a stale panel.
      if (err) return this.setStatus("live").setDetail([]);
      return this.setError("")
        .setStatus("live")
        .setTs(res.ts)
        .setDetail([Torrent.Class.fromData(res)]);
    },
    fetchTransfers(res, err) {
      if (err) return this.setStatus("error").setError(String((err && err.message) || err));
      let dl = 0;
      for (const tr of res.transfers) dl += tr.download_rate;
      return this.setError("")
        .setStatus("live")
        .setTs(res.ts)
        .setTransfers(res.transfers.map((tr) => TransferRow.Class.fromData(tr)))
        .setTransfersCount(res.transfers.length)
        .setTransfersDlText(`${human(dl)}/s`);
    },
    fetchNodes(res, err) {
      if (err) return this.setStatus("error").setError(String((err && err.message) || err));
      let stored = 0;
      for (const n of res.nodes) stored += n.stored;
      return this.setError("")
        .setStatus("live")
        .setTs(res.ts)
        .setNodes(res.nodes.map((n) => NodeStatRow.Class.fromData(n)))
        .setNodesCount(res.nodes.length)
        .setNodesStoredText(human(stored));
    },
  },
  view: html`<div class="dash">
    <header class="topbar">
      <h1>BitTorrent swarm</h1>
      <div class="status">
        <span :class="$statusClass" @text="$statusText"></span>
        <span class="muted small" @text="$updatedText"></span>
      </div>
    </header>
    <nav class="tabs">
      <a :class="$navOverview" data-link="1" href="/">Overview</a>
      <a :class="$navTransfers" data-link="1" href="/transfers">Transfers</a>
      <a :class="$navNodes" data-link="1" href="/nodes">Nodes</a>
    </nav>
    <div class="banner" @show="truthy? .error" @text=".error"></div>

    <div @show="$isList">
      <div class="summary">
        <div class="stat"><span class="num" @text=".totDatasets"></span><span class="lbl">datasets</span></div>
        <div class="stat"><span class="num" @text=".totStoredText"></span><span class="lbl">stored across swarm</span></div>
        <div class="stat"><span class="num" @text=".totNodes"></span><span class="lbl">nodes</span></div>
        <div class="stat"><span class="num" @text=".replicatingText"></span><span class="lbl">replicating now</span></div>
        <div class="stat"><span :class=".rarestClass" @text=".rarest"></span><span class="lbl">rarest copies</span></div>
      </div>

      <h3>Datasets</h3>
      <div class="dataset-head">
        <span>dataset</span><span class="tnum">size</span><span class="tnum">copies</span>
        <span>spread (per node)</span><span class="tnum">activity</span>
      </div>
      <x render-each=".datasets"></x>
      <div class="empty" @show="$isEmptyList">
        No datasets reported yet. Start the tracker, the nodes, and assign torrents
        (see the README); this view updates on its own.
      </div>
    </div>

    <div @show="$isDetail">
      <a class="back" data-link="1" href="/">← all datasets</a>
      <div class="empty" @show="$detailMissing">
        This dataset is no longer reported by any node.
      </div>
      <x render-each=".detail"></x>
    </div>

    <div @show="$isTransfers">
      <div class="summary">
        <div class="stat"><span class="num" @text=".transfersCount"></span><span class="lbl">in-flight transfers</span></div>
        <div class="stat"><span class="num" @text=".transfersDlText"></span><span class="lbl">total download</span></div>
      </div>
      <h3>Transfers in flight</h3>
      <div class="transfer-head">
        <span>dataset</span><span>node</span><span>progress</span><span class="nnum">%</span>
        <span class="nnum">rate</span><span class="nnum">eta</span><span class="nnum">peers</span>
      </div>
      <x render-each=".transfers"></x>
      <div class="empty" @show="$isEmptyTransfers">
        Nothing transferring right now — every node holds a complete copy of what it
        was assigned.
      </div>
    </div>

    <div @show="$isNodes">
      <div class="summary">
        <div class="stat"><span class="num" @text=".nodesCount"></span><span class="lbl">nodes</span></div>
        <div class="stat"><span class="num" @text=".nodesStoredText"></span><span class="lbl">stored across swarm</span></div>
      </div>
      <h3>Nodes</h3>
      <div class="node-head">
        <span>node</span><span class="nnum">complete/held</span><span class="nnum">stored</span>
        <span class="nnum">download</span><span class="nnum">upload</span><span class="nnum">peers</span>
      </div>
      <x render-each=".nodes"></x>
      <div class="empty" @show="$isEmptyNodes">No nodes reporting yet.</div>
    </div>
  </div>`,
});

// --- routing (History API + URLPattern) ----------------------------------------

// Real paths, not hashes: "/" is the overview, "/dataset/<info_hash>" the
// drill-down. The dataset's detail JSON still lives at the collector's
// "/torrent/<info_hash>" API — the page route is a distinct noun so the two never
// collide — and the collector serves index.html for "/dataset/..." so deep links
// and reloads resolve to the SPA. URLPattern does the matching; the first match
// wins, so the specific detail route is listed before the catch-all.
const ROUTES = [
  { pattern: new URLPattern({ pathname: "/dataset/:hash" }), route: "detail" },
  { pattern: new URLPattern({ pathname: "/transfers" }), route: "transfers" },
  { pattern: new URLPattern({ pathname: "/nodes" }), route: "nodes" },
  { pattern: new URLPattern({ pathname: "/*" }), route: "list" },
];

function matchRoute() {
  for (const { pattern, route } of ROUTES) {
    const m = pattern.exec({ pathname: location.pathname });
    if (m) {
      const hash = m.pathname.groups.hash;
      return { route, hash: hash ? decodeURIComponent(hash) : "" };
    }
  }
  return { route: "list", hash: "" };
}

function main() {
  const app = tutuca("#app");
  const scope = app.registerComponents([
    Dashboard,
    DatasetRow,
    TransferRow,
    NodeStatRow,
    Torrent,
    NodeRow,
    FileRow,
    Cell,
  ]);
  scope.registerRequestHandlers({
    async fetchOverview() {
      const r = await fetch(OVERVIEW_URL, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    async fetchTransfers() {
      const r = await fetch(TRANSFERS_URL, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    async fetchNodes() {
      const r = await fetch(NODES_URL, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
    // Reads the live route itself (like fetchOverview reads its fixed URL), so the
    // request needs no argument and always targets the currently-open dataset.
    async fetchDetail() {
      const hash = matchRoute().hash;
      if (!hash) throw new Error("no dataset selected");
      const r = await fetch(DETAIL_URL + encodeURIComponent(hash), { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    },
  });
  app.state.set(Dashboard.make({}));
  app.start();
  app.sendAtRoot("init");

  // Intercept same-origin link clicks (marked data-link) and route them through
  // the History API instead of a full page load. Modified clicks (new tab, etc.)
  // fall through to the browser.
  document.addEventListener("click", (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest("a[data-link]");
    if (!a) return;
    e.preventDefault();
    const href = a.getAttribute("href");
    if (href !== location.pathname) {
      history.pushState({}, "", href);
      app.sendAtRoot("navigate"); // re-route + fetch immediately
    }
  });
  // Back/forward buttons.
  window.addEventListener("popstate", () => app.sendAtRoot("navigate"));

  setInterval(() => app.sendAtRoot("tick"), POLL_MS);
}

// Only mount in the browser. The tutuca CLI imports this module for
// lint/render/test and must not trigger the live mount + polling.
if (typeof document !== "undefined" && document.getElementById("app")) {
  main();
}

// --- module exports for the tutuca CLI (lint / render / test) ------------------

export function getComponents() {
  return [Dashboard, DatasetRow, TransferRow, NodeStatRow, Torrent, NodeRow, FileRow, Cell];
}

export function getRoot() {
  return Dashboard.make({});
}

export function getExamples() {
  const dataset = {
    info_hash: "abcdef0123456789cafef00d",
    name: "media-2026-0612",
    total_size: 2000000,
    num_pieces: 8,
    piece_length: 262144,
    nodes_seen: 3,
    full_copies: 1,
    durable_copies: 1,
    min_avail: 1,
    redundancy: 1.5,
    total_stored: 3145728,
    download_rate: 50000,
    upload_rate: 1000,
    downloading: 1,
    seeding: 2,
    spread: [
      { label: "0", frac: 1.0 },
      { label: "1", frac: 0.5 },
      { label: "2", frac: 0.0 },
    ],
  };
  return {
    title: "Swarm overview",
    description: "A dataset row from the Overview list: one full copy, one node still replicating.",
    items: [{ title: "Replicating dataset", value: DatasetRow.Class.fromData(dataset) }],
  };
}
