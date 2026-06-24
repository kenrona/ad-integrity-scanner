// progress.js — reusable job-polling + dependency-free SVG chart helpers.
// Shared across sources.html, profile.html, dashboard.html. No external deps.

// ---------------------------------------------------------------------------
// Job polling
// ---------------------------------------------------------------------------
// pollJob(jobId, {onProgress, onDone, onError, intervalMs=1000})
//   Polls GET /jobs/{jobId} every intervalMs. Calls onProgress(job) each tick,
//   then onDone(job) when status==='done' or onError(job) when status==='error'.
//   Stops polling on done/error. Returns a function that cancels polling.
function pollJob(jobId, opts) {
  opts = opts || {};
  const intervalMs = opts.intervalMs || 1000;
  let stopped = false;
  let timer = null;
  async function tick() {
    if (stopped) return;
    try {
      const r = await fetch('/jobs/' + jobId);
      if (!r.ok) {
        if (r.status === 404) {
          if (opts.onError) opts.onError({ status: 'error', error: 'job not found', id: jobId });
          return;
        }
        throw new Error('http ' + r.status);
      }
      const job = await r.json();
      if (opts.onProgress) opts.onProgress(job);
      if (job.status === 'done') {
        if (opts.onDone) opts.onDone(job);
        return;
      }
      if (job.status === 'error') {
        if (opts.onError) opts.onError(job);
        return;
      }
    } catch (e) {
      // transient fetch error — keep polling
    }
    if (!stopped) timer = setTimeout(tick, intervalMs);
  }
  tick();
  return function cancel() { stopped = true; if (timer) clearTimeout(timer); };
}

// fillBar(barInnerEl, done, total): set width on the inner <i id="prog"> element.
function fillBar(barInnerEl, done, total) {
  if (!barInnerEl) return;
  barInnerEl.style.width = total ? (Math.min(100, done / total * 100)) + '%' : '0';
}

// ---------------------------------------------------------------------------
// SVG chart helpers (dependency-free). Colors pulled from CSS vars.
// ---------------------------------------------------------------------------
function _cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name);
  return (v && v.trim()) || fallback;
}
function _svgEl(tag, attrs) {
  const el = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}
function _fmt(v) {
  if (v === null || v === undefined || isNaN(v)) return '';
  if (Math.abs(v) >= 1000 || (v !== 0 && Math.abs(v) < 0.01)) return Number(v).toPrecision(3);
  return (Math.round(v * 100) / 100).toString();
}

// svgBarChart(container, {labels, values, color}): horizontal bar chart.
function svgBarChart(container, opts) {
  container.innerHTML = '';
  const labels = opts.labels || [];
  const values = opts.values || [];
  if (!labels.length) { container.innerHTML = '<span class="muted">no data</span>'; return; }
  const ink = _cssVar('--ink', '#e7ecf5');
  const mut = _cssVar('--mut', '#8a97b3');
  const color = opts.color || _cssVar('--acc', '#5b8cff');
  const width = container.clientWidth || 600;
  const rowH = 24, gap = 6, padL = 140, padR = 60, padT = 8;
  const height = padT * 2 + labels.length * (rowH + gap);
  const maxV = Math.max(1, ...values.map(v => Math.abs(v) || 0));
  const barW = width - padL - padR;
  const svg = _svgEl('svg', { width: width, height: height, viewBox: '0 0 ' + width + ' ' + height });
  labels.forEach((lab, i) => {
    const y = padT + i * (rowH + gap);
    const v = values[i] || 0;
    const w = Math.max(1, Math.abs(v) / maxV * barW);
    const lt = _svgEl('text', { x: padL - 8, y: y + rowH / 2 + 4, fill: mut, 'font-size': 11, 'text-anchor': 'end' });
    lt.textContent = String(lab).length > 22 ? String(lab).slice(0, 21) + '…' : lab;
    svg.appendChild(lt);
    svg.appendChild(_svgEl('rect', { x: padL, y: y, width: w, height: rowH, rx: 3, fill: color }));
    const vt = _svgEl('text', { x: padL + w + 6, y: y + rowH / 2 + 4, fill: ink, 'font-size': 11 });
    vt.textContent = _fmt(v);
    svg.appendChild(vt);
  });
  container.appendChild(svg);
}

// svgGroupedBars(container, {labels, seriesA, seriesB, labelA, labelB}): grouped horizontal bars.
function svgGroupedBars(container, opts) {
  container.innerHTML = '';
  const labels = opts.labels || [];
  const seriesA = opts.seriesA || [];
  const seriesB = opts.seriesB || [];
  if (!labels.length) { container.innerHTML = '<span class="muted">no data</span>'; return; }
  const ink = _cssVar('--ink', '#e7ecf5');
  const mut = _cssVar('--mut', '#8a97b3');
  const colA = _cssVar('--acc', '#5b8cff');
  const colB = _cssVar('--ok', '#3ecf8e');
  const width = container.clientWidth || 600;
  const barH = 12, innerGap = 3, groupGap = 12, padL = 150, padR = 60, padT = 24;
  const groupH = barH * 2 + innerGap;
  const height = padT + labels.length * (groupH + groupGap);
  const allVals = seriesA.concat(seriesB).map(v => Math.abs(v) || 0);
  const maxV = Math.max(1, ...allVals);
  const barW = width - padL - padR;
  const svg = _svgEl('svg', { width: width, height: height, viewBox: '0 0 ' + width + ' ' + height });
  // legend
  svg.appendChild(_svgEl('rect', { x: padL, y: 4, width: 10, height: 10, rx: 2, fill: colA }));
  const la = _svgEl('text', { x: padL + 16, y: 13, fill: mut, 'font-size': 11 }); la.textContent = opts.labelA || 'A';
  svg.appendChild(la);
  svg.appendChild(_svgEl('rect', { x: padL + 90, y: 4, width: 10, height: 10, rx: 2, fill: colB }));
  const lb = _svgEl('text', { x: padL + 106, y: 13, fill: mut, 'font-size': 11 }); lb.textContent = opts.labelB || 'B';
  svg.appendChild(lb);
  labels.forEach((lab, i) => {
    const y = padT + i * (groupH + groupGap);
    const lt = _svgEl('text', { x: padL - 8, y: y + groupH / 2 + 4, fill: mut, 'font-size': 11, 'text-anchor': 'end' });
    lt.textContent = String(lab).length > 24 ? String(lab).slice(0, 23) + '…' : lab;
    svg.appendChild(lt);
    const a = seriesA[i] || 0, b = seriesB[i] || 0;
    const wA = Math.max(1, Math.abs(a) / maxV * barW);
    const wB = Math.max(1, Math.abs(b) / maxV * barW);
    svg.appendChild(_svgEl('rect', { x: padL, y: y, width: wA, height: barH, rx: 2, fill: colA }));
    const ta = _svgEl('text', { x: padL + wA + 5, y: y + barH - 2, fill: ink, 'font-size': 10 }); ta.textContent = _fmt(a);
    svg.appendChild(ta);
    svg.appendChild(_svgEl('rect', { x: padL, y: y + barH + innerGap, width: wB, height: barH, rx: 2, fill: colB }));
    const tb = _svgEl('text', { x: padL + wB + 5, y: y + barH + innerGap + barH - 2, fill: ink, 'font-size': 10 }); tb.textContent = _fmt(b);
    svg.appendChild(tb);
  });
  container.appendChild(svg);
}

// svgHistogram(container, {buckets:[{bucket,n}...], color, title}): vertical histogram.
function svgHistogram(container, opts) {
  container.innerHTML = '';
  const buckets = opts.buckets || [];
  if (!buckets.length) { container.innerHTML = '<span class="muted">no data</span>'; return; }
  const ink = _cssVar('--ink', '#e7ecf5');
  const mut = _cssVar('--mut', '#8a97b3');
  const color = opts.color || _cssVar('--acc', '#5b8cff');
  const width = container.clientWidth || 400;
  const padT = 14, padB = 40, padL = 30, padR = 10;
  const height = 200;
  const plotH = height - padT - padB;
  const maxN = Math.max(1, ...buckets.map(b => b.n || 0));
  const slot = (width - padL - padR) / buckets.length;
  const barW = Math.max(2, slot * 0.7);
  const svg = _svgEl('svg', { width: width, height: height, viewBox: '0 0 ' + width + ' ' + height });
  // baseline axis
  svg.appendChild(_svgEl('line', { x1: padL, y1: padT + plotH, x2: width - padR, y2: padT + plotH, stroke: mut, 'stroke-width': 1 }));
  buckets.forEach((b, i) => {
    const n = b.n || 0;
    const h = n / maxN * plotH;
    const x = padL + i * slot + (slot - barW) / 2;
    const y = padT + plotH - h;
    svg.appendChild(_svgEl('rect', { x: x, y: y, width: barW, height: Math.max(0, h), rx: 2, fill: color }));
    if (n > 0) {
      const vt = _svgEl('text', { x: x + barW / 2, y: y - 3, fill: ink, 'font-size': 10, 'text-anchor': 'middle' });
      vt.textContent = n;
      svg.appendChild(vt);
    }
    const lt = _svgEl('text', { x: x + barW / 2, y: height - padB + 14, fill: mut, 'font-size': 9, 'text-anchor': 'middle' });
    lt.textContent = b.bucket;
    svg.appendChild(lt);
  });
  container.appendChild(svg);
}
