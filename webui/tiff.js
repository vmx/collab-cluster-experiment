// Draws a GeoTIFF the browser can't show itself: ?src=<file URL>&name=<label>.
// One band is shown as grey, three or more as RGB from the first three; samples
// wider than 8 bits are stretched between the 2nd and 98th percentile.
import { fromArrayBuffer } from "/geotiff.js";

const MAX_SIDE = 2048;
const info = document.getElementById("info");
const canvas = document.getElementById("img");
const dl = document.getElementById("dl");

function stretch(band, noData) {
  const valid = [];
  for (let i = 0; i < band.length; i += Math.max(1, band.length >> 16)) {
    const v = band[i];
    if (v !== noData && Number.isFinite(v)) valid.push(v);
  }
  valid.sort((a, b) => a - b);
  const lo = valid[Math.floor(valid.length * 0.02)] ?? 0;
  const hi = valid[Math.floor(valid.length * 0.98)] ?? lo;
  return [lo, hi > lo ? hi : lo + 1];
}

async function show() {
  const q = new URLSearchParams(location.search);
  const src = q.get("src");
  document.title = q.get("name") || "TIFF viewer";
  dl.href = src;
  dl.download = (q.get("name") || "image.tif").split("/").pop();
  dl.hidden = false;
  const res = await fetch(src);
  if (!res.ok) throw new Error(`${src}: HTTP ${res.status}`);
  const tiff = await fromArrayBuffer(await res.arrayBuffer());
  const image = await tiff.getImage();
  const scale = Math.min(1, MAX_SIDE / Math.max(image.getWidth(), image.getHeight()));
  const width = Math.round(image.getWidth() * scale);
  const height = Math.round(image.getHeight() * scale);
  const n = image.getSamplesPerPixel();
  const bands = n >= 3 ? [0, 1, 2] : [0];
  const rasters = await image.readRasters({ samples: bands, width, height, interleave: false });
  const noData = image.getGDALNoData();
  const eight = image.getBitsPerSample() === 8;
  const rgba = new ImageData(width, height);
  const out = rgba.data;
  const ranges = rasters.map((r) => (eight ? [0, 255] : stretch(r, noData)));
  for (let i = 0; i < width * height; i++) {
    for (let c = 0; c < 3; c++) {
      const [lo, hi] = ranges[bands.length === 1 ? 0 : c];
      const v = rasters[bands.length === 1 ? 0 : c][i];
      out[i * 4 + c] = ((v - lo) / (hi - lo)) * 255;
    }
    out[i * 4 + 3] = rasters[0][i] === noData ? 0 : 255;
  }
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d").putImageData(rgba, 0, 0);
  canvas.hidden = false;
  info.textContent = `${q.get("name") || ""} — ${image.getWidth()}×${image.getHeight()}, ${n} band${n === 1 ? "" : "s"}, ${image.getBitsPerSample()} bit` +
    (scale < 1 ? `, shown at ${width}×${height}` : "");
}

show().catch((e) => {
  info.textContent = `cannot show this file: ${e.message}`;
  info.className = "err";
});
