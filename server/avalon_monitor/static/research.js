// Research results pages: renders the latest document a project pushed with `avm results push`
// (schema "<name>-results/<n>") and per-metric history across pushes. Everything is rendered as
// text/SVG from the pushed document only - nothing is ever fetched from the project itself.
import { $, h, fmt, state, api, post, guard, on, navigate, toast, canChange, roTip } from "./core.js?v=1.17.0";

const NS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, ...kids) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const c of kids.flat()) if (c != null) el.append(c.nodeType ? c : document.createTextNode(String(c)));
  return el;
}
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const fv = (v) => (num(v) == null ? "—" : Math.abs(v) < 10 ? v.toFixed(3) : v.toPrecision(4));
// Per-figure options a document may set (all optional; absent = the old behaviour):
//   decimals      0-6: value labels and tooltips with that many decimals (else 3 below 10)
//   points_label  dot-bars: what the dots are, in the legend (else "test users")
//   baseline      dot-bars: bars grow up or down from this value, e.g. 0.5 for an AUC (else 0)
export const decOf = (fig) => (Number.isInteger(fig?.decimals) && fig.decimals >= 0 && fig.decimals <= 6 ? fig.decimals : null);
export const fvd = (v, d) => (d == null || num(v) == null ? fv(v) : v.toFixed(d));
export const pointsLabel = (fig) => "dots: " + (typeof fig?.points_label === "string" && fig.points_label.trim() ? fig.points_label.trim() : "test users");
export const baseOf = (fig) => (num(fig?.baseline) != null ? fig.baseline : 0);
const fci = (ci) => (Array.isArray(ci) && num(ci[0]) != null && num(ci[1]) != null ? `[${fv(ci[0])}, ${fv(ci[1])}]` : null);
const clip = (t, n) => { t = String(t ?? ""); if (t.length <= n) return t; const a = Math.ceil((n - 1) / 2), b = Math.floor((n - 1) / 2); return t.slice(0, a) + "…" + (b ? t.slice(-b) : ""); };
// Categorical order (validated against both surfaces); identity is always also carried by a text label.
const SERIES = ["--c-blue", "--c-orange", "--c-aqua", "--c-violet", "--c-magenta", "--c-yellow"];

// ------------------------------------------------------------- tooltip
let tipEl;
function tip(el, text) {
  const show = (e) => {
    if (!tipEl) { tipEl = h("div", { class: "viz-tip" }); document.body.append(tipEl); }
    const t = typeof text === "function" ? text(e) : text;
    if (!t) { tipEl.style.display = "none"; return; }
    tipEl.textContent = t; tipEl.style.display = "block";
    const x = Math.min(e.clientX + 14, window.innerWidth - tipEl.offsetWidth - 8);
    const y = e.clientY + 16 + tipEl.offsetHeight > window.innerHeight ? e.clientY - tipEl.offsetHeight - 10 : e.clientY + 16;
    tipEl.style.left = x + "px"; tipEl.style.top = y + "px";
  };
  el.addEventListener("mousemove", show);
  el.addEventListener("mouseleave", () => { if (tipEl) tipEl.style.display = "none"; });
}

function domainOf(values, preferUnit) {
  const vs = values.filter((v) => num(v) != null);
  if (preferUnit || (vs.length && vs.every((v) => v >= 0 && v <= 1))) return [0, 1];
  if (!vs.length) return [0, 1];
  let lo = Math.min(0, ...vs), hi = Math.max(...vs);
  if (hi === lo) hi = lo + 1;
  return [lo, hi + (hi - lo) * 0.04];
}
function ticksFor([lo, hi]) {
  if (lo === 0 && hi === 1) return [0, 0.25, 0.5, 0.75, 1];
  const raw = (hi - lo) / 4, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  const step = (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
  const out = [];
  for (let t = Math.ceil(lo / step - 1e-9) * step; t <= hi + step * 1e-9; t += step) out.push(Math.abs(t) < step * 1e-9 ? 0 : +t.toPrecision(12));
  return out;
}

// A figure redraws itself whenever its card changes width (phone <-> desktop, sidebar toggles).
function responsive(draw) {
  const box = h("div", { class: "viz" });
  let w = 0;
  let raf = 0;
  const ro = new ResizeObserver(() => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => { const nw = Math.floor(box.clientWidth); if (nw && nw !== w) { w = nw; box.replaceChildren(draw(w)); } });
  });
  ro.observe(box);
  return box;
}

// ------------------------------------------------------------ headline
function intervalPlot(row, width, scale) {
  const H = 28, pad = 8, [lo, hi] = scale;
  const x = (v) => pad + ((Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * (width - pad * 2);
  const g = s("svg", { width, height: H, class: "ivl" });
  for (const t of ticksFor(scale)) g.append(s("line", { x1: x(t), x2: x(t), y1: 6, y2: H - 6, class: "grid" }));
  g.append(s("line", { x1: x(lo), x2: x(hi), y1: H / 2, y2: H / 2, class: "grid" }));
  if (num(row.chance) != null) g.append(s("line", { x1: x(row.chance), x2: x(row.chance), y1: 5, y2: H - 5, class: "chance" }));
  const cmp = row.comparator && num(row.comparator.value) != null ? row.comparator : null;
  if (cmp) g.append(s("rect", { x: x(cmp.value) - 1.5, y: 4, width: 3, height: H - 8, rx: 1.5, class: "cmp" }));
  const ci = Array.isArray(row.ci) && num(row.ci[0]) != null && num(row.ci[1]) != null ? row.ci : null;
  if (ci) {
    g.append(s("line", { x1: x(ci[0]), x2: x(ci[1]), y1: H / 2, y2: H / 2, class: "whisk" }));
    for (const c of ci) g.append(s("line", { x1: x(c), x2: x(c), y1: H / 2 - 4, y2: H / 2 + 4, class: "whisk" }));
  }
  if (num(row.value) != null) g.append(s("circle", { cx: x(row.value), cy: H / 2, r: 5, class: "pt" }));
  tip(g, () => [
    `${row.result || row.id}: ${fv(row.value)}${ci ? "  95% CI " + fci(ci) : "  (no interval recorded)"}`,
    num(row.chance) != null ? `chance ${fv(row.chance)}` : null,
    cmp ? `${cmp.label || "comparator"} ${fv(cmp.value)}` : null,
  ].filter(Boolean).join("\n"));
  return g;
}

function scaleAxis(width, scale) {
  const pad = 8, [lo, hi] = scale;
  const x = (v) => pad + ((v - lo) / (hi - lo)) * (width - pad * 2);
  return s("svg", { width, height: 14, class: "ivl-axis" }, ticksFor(scale).map((t, i, a) =>
    s("text", { x: x(t), y: 11, "text-anchor": i === 0 ? "start" : i === a.length - 1 ? "end" : "middle" }, String(+t.toFixed(2)))));
}

function sparkline(points) {
  const W = 96, H = 26, pad = 4;
  const pts = points.filter((p) => num(p.value) != null);
  const g = s("svg", { width: W, height: H, class: "spark" });
  if (!pts.length) return g;
  const vs = pts.map((p) => p.value);
  let lo = Math.min(...vs), hi = Math.max(...vs);
  if (hi - lo < 1e-9) { lo -= 0.01; hi += 0.01; }
  const x = (i) => (pts.length === 1 ? W - pad : pad + (i / (pts.length - 1)) * (W - pad * 2));
  const y = (v) => H - pad - ((v - lo) / (hi - lo)) * (H - pad * 2);
  if (pts.length > 1) g.append(s("polyline", { points: pts.map((p, i) => `${x(i)},${y(p.value)}`).join(" "), class: "line" }));
  const end = s("circle", { cx: x(pts.length - 1), cy: y(vs[vs.length - 1]), r: 3, class: "pt" });
  const hover = s("circle", { r: 3.5, class: "pt hover", style: "display:none" });
  g.append(end, hover, s("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent" }));
  tip(g, (e) => {
    const r = g.getBoundingClientRect();
    const i = pts.length === 1 ? 0 : Math.max(0, Math.min(pts.length - 1, Math.round(((e.clientX - r.left - pad) / (W - pad * 2)) * (pts.length - 1))));
    hover.setAttribute("cx", x(i)); hover.setAttribute("cy", y(pts[i].value)); hover.style.display = "";
    const p = pts[i];
    return `${fv(p.value)}${fci(p.ci) ? " " + fci(p.ci) : ""}\npush #${p.push} · ${fmt.when(p.received)}${p.commit ? " · " + p.commit : ""}`;
  });
  g.addEventListener("mouseleave", () => { hover.style.display = "none"; });
  return g;
}

function headlineTable(doc, history, viewing) {
  const rows = (doc.headline || []).filter((r) => r && typeof r === "object");
  if (!rows.length) return null;
  // per-id history up to (and including) the push being viewed
  const upto = history.filter((p) => p.id <= viewing);
  const series = new Map();
  for (const p of upto) for (const hl of p.headline || []) {
    if (!series.has(hl.id)) series.set(hl.id, []);
    series.get(hl.id).push({ ...hl, push: p.id, received: p.received, commit: p.commit });
  }
  const IW = 240;
  const tbody = h("tbody");
  let group = null;
  for (const r of rows) {
    if ((r.group || "") !== group) {
      group = r.group || "";
      if (group) tbody.append(h("tr", { class: "grp" }, h("td", { colspan: 6 }, group)));
    }
    const scale = Array.isArray(r.scale) && num(r.scale[0]) != null && num(r.scale[1]) != null && r.scale[1] > r.scale[0] ? r.scale : [0, 1];
    const hist = series.get(r.id) || [];
    const prev = hist.length > 1 ? hist[hist.length - 2].value : null;
    const delta = num(prev) != null && num(r.value) != null && Math.abs(r.value - prev) >= 0.0005 ? r.value - prev : null;
    tbody.append(h("tr", {},
      h("td", { class: "res" }, h("div", { class: "res-name" }, r.result || r.id), h("div", { class: "res-sub" }, [r.metric, r.compliance].filter(Boolean).join(" · "))),
      h("td", { class: "r num" }, r.n == null ? "—" : String(r.n)),
      h("td", { class: "r num val" }, h("b", {}, fv(r.value)), h("div", { class: "res-sub" }, fci(r.ci) || "no CI"),
        delta != null ? h("div", { class: "delta " + (delta > 0 ? "up" : "down") }, `${delta > 0 ? "▲ +" : "▼ "}${fv(delta)} vs previous`) : null),
      h("td", { class: "plot" }, intervalPlot(r, IW, scale)),
      h("td", { class: "r num muted" }, r.comparator && num(r.comparator.value) != null ? h("span", { title: r.comparator.label || "" }, fv(r.comparator.value)) : "—"),
      h("td", { class: "trend" }, hist.length ? sparkline(hist) : null, h("div", { class: "res-sub" }, `${hist.length} push${hist.length === 1 ? "" : "es"}`)),
    ));
  }
  const legend = h("div", { class: "viz-legend" },
    h("span", {}, h("i", { class: "lg-pt" }), "result · 95% CI"),
    h("span", {}, h("i", { class: "lg-chance" }), "chance"),
    h("span", {}, h("i", { class: "lg-cmp" }), "comparator"));
  return h("div", { class: "card table-card research-headline" },
    h("div", { class: "fig-head" }, h("h3", {}, "Headline results"), legend),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Result"), h("th", { class: "r" }, "n"), h("th", { class: "r" }, "Value"),
        h("th", { class: "plot" }, scaleAxis(IW, [0, 1])), h("th", { class: "r" }, "Comparator"), h("th", {}, "Trend"))),
      tbody)));
}

// ---------------------------------------------------------- programmes
const STATUS_CLASS = { running: "run", closed: "", fail: "bad", failed: "bad", partial: "warn", reference: "outline", registered: "outline" };
function programmes(doc) {
  const rows = (doc.programmes || []).filter((r) => r && typeof r === "object");
  if (!rows.length) return null;
  const counts = {};
  for (const p of rows) counts[p.status] = (counts[p.status] || 0) + 1;
  const list = h("div", { class: "prog-list" });
  for (const p of rows) {
    list.append(h("div", { class: "prog-row" },
      h("span", { class: "chip " + (STATUS_CLASS[String(p.status || "").toLowerCase()] ?? "outline") }, p.status_text || p.status || "?"),
      h("div", { class: "prog-body" },
        h("div", { class: "res-name" }, p.title || p.id),
        p.key_result ? h("div", { class: "prog-key" }, p.key_result) : null,
        h("div", { class: "res-sub" }, [p.registration, p.updated ? "updated " + p.updated : null, p.eta ? "ETA " + p.eta : null].filter(Boolean).join(" · "))),
    ));
  }
  return h("div", { class: "card table-card" },
    h("div", { class: "fig-head" }, h("h3", {}, "Programmes"),
      h("div", { class: "viz-legend" }, Object.entries(counts).map(([k, n]) => h("span", { class: "chip " + (STATUS_CLASS[k] ?? "outline") }, `${n} ${k}`)))),
    list);
}

// ---------------------------------------------------------------- bars
function barFigure(fig) {
  const rows = (fig.rows || []).filter((r) => r && typeof r === "object");
  const dec = decOf(fig), fval = (v) => fvd(v, dec), fcid = (ci) => (fci(ci) ? `[${fval(ci[0])}, ${fval(ci[1])}]` : null);
  const dom = Array.isArray(fig.domain) && num(fig.domain[0]) != null && num(fig.domain[1]) != null && fig.domain[1] > fig.domain[0]
    ? fig.domain : domainOf(rows.flatMap((r) => [r.value, ...(Array.isArray(r.ci) ? r.ci : [])]), fig.unit === "rate");
  const draw = (W) => {
    const narrow = W < 480, BH = 12, top = 4, axisH = 18, RH = narrow ? 34 : 22, off = narrow ? 7 : 0;
    const labW = narrow ? 0 : Math.min(250, Math.max(110, W * 0.36)), valW = 54;
    const x0 = narrow ? 2 : labW + 8, x1 = W - valW;
    const x = (v) => x0 + ((Math.max(dom[0], Math.min(dom[1], v)) - dom[0]) / (dom[1] - dom[0])) * (x1 - x0);
    const H = top + rows.length * RH + axisH;
    const g = s("svg", { width: W, height: H, class: "bars" });
    const band = Array.isArray(fig.band) && num(fig.band[0]) != null && num(fig.band[1]) != null && fig.band[1] > fig.band[0] ? fig.band : null;
    if (band) {
      const bx = s("rect", { x: x(band[0]), y: top, width: Math.max(1, x(band[1]) - x(band[0])), height: H - axisH - top + 2, class: "band" });
      tip(bx, `${fig.band_label || "band"}: ${fval(band[0])} to ${fval(band[1])}`);
      g.append(bx);
    }
    for (const t of ticksFor(dom)) {
      g.append(s("line", { x1: x(t), x2: x(t), y1: top, y2: H - axisH + 2, class: t === 0 && dom[0] < 0 ? "zero" : "grid" }));
      g.append(s("text", { x: x(t), y: H - 4, "text-anchor": "middle", class: "tick" }, String(+t.toFixed(3))));
    }
    const maxChars = Math.floor((narrow ? W - valW : labW) / 6.4);
    rows.forEach((r, i) => {
      const yc = top + i * RH + RH / 2 + off;
      const v = num(r.value);
      g.append(narrow ? s("text", { x: x0, y: yc - 10, class: "lab" }, clip(r.label, maxChars))
                      : s("text", { x: labW, y: yc + 4, "text-anchor": "end", class: "lab" }, clip(r.label, maxChars)));
      if (v != null) {
        // bars grow from 0 (or the domain edge nearest it) in either direction:
        // square at the baseline, 4px rounded data-end
        const xa = x(Math.min(dom[1], Math.max(dom[0], 0))), xb = x(v), wbar = Math.abs(xb - xa), rr = Math.min(4, wbar / 2, BH / 2), dir = xb >= xa ? 1 : -1;
        if (wbar > 0.5) g.append(s("path", { class: "bar", d: `M${xa},${yc - BH / 2}h${dir * (wbar - rr)}a${rr},${rr} 0 0 ${dir > 0 ? 1 : 0} ${dir * rr},${rr}v${BH - 2 * rr}a${rr},${rr} 0 0 ${dir > 0 ? 1 : 0} ${-dir * rr},${rr}h${-dir * (wbar - rr)}z` }));
        if (Array.isArray(r.ci) && num(r.ci[0]) != null && num(r.ci[1]) != null) {
          g.append(s("line", { x1: x(r.ci[0]), x2: x(r.ci[1]), y1: yc, y2: yc, class: "whisk-bar" }));
          for (const c of r.ci) g.append(s("line", { x1: x(c), x2: x(c), y1: yc - 4, y2: yc + 4, class: "whisk-bar" }));
        }
      }
      g.append(s("text", { x: W - 2, y: yc + 4, "text-anchor": "end", class: "val" }, fval(v)));
      const hit = s("rect", { x: 0, y: yc - RH / 2 - off, width: W, height: RH, fill: "transparent" });
      const extra = Object.entries(r).filter(([k, val]) => !["label", "value", "ci"].includes(k) && (typeof val === "number" || typeof val === "string")).map(([k, val]) => `${k} ${val}`);
      tip(hit, [`${r.label}: ${fval(v)}`, fcid(r.ci) ? "95% CI " + fcid(r.ci) : null, ...extra].filter(Boolean).join("\n"));
      g.append(hit);
    });
    return g;
  };
  const better = fig.better === "lower" ? "lower is better" : fig.better === "higher" ? "higher is better" : null;
  return h("div", { class: "card table-card fig" },
    h("div", { class: "fig-head" }, h("h3", {}, fig.title || fig.id), h("span", { class: "muted fig-cap" }, [fig.unit, better].filter(Boolean).join(" · ")),
      Array.isArray(fig.band) ? h("span", { class: "viz-legend" }, h("span", {}, h("i", { class: "lg-band" }), fig.band_label || "band")) : null),
    rows.length ? responsive(draw) : h("div", { class: "muted" }, "no rows"));
}

// ------------------------------------------------------- distributions
const quant = (sorted, q) => { const i = (sorted.length - 1) * q, a = Math.floor(i), b = Math.ceil(i); return sorted[a] + (sorted[b] - sorted[a]) * (i - a); };
function stats(values) {
  const v = values.filter((x) => num(x) != null).sort((a, b) => a - b);
  if (!v.length) return null;
  return { n: v.length, min: v[0], q1: quant(v, 0.25), med: quant(v, 0.5), q3: quant(v, 0.75), max: v[v.length - 1], mean: v.reduce((a, b) => a + b, 0) / v.length, v };
}

function stripFigure(fig) {
  const ser = (fig.series || []).filter((x) => x && Array.isArray(x.values)).map((x) => ({ ...x, st: stats(x.values) }));
  const dom = domainOf(ser.flatMap((x) => x.values), false);
  const draw = (W) => {
    const narrow = W < 480, top = 4, axisH = 18, RH = narrow ? 40 : 26, off = narrow ? 7 : 0;
    const labW = narrow ? 0 : Math.min(250, Math.max(110, W * 0.36)), valW = 54;
    const x0 = narrow ? 4 : labW + 8, x1 = W - valW;
    const x = (v) => x0 + ((v - dom[0]) / (dom[1] - dom[0])) * (x1 - x0);
    const H = top + ser.length * RH + axisH;
    const g = s("svg", { width: W, height: H, class: "dist" });
    for (const t of ticksFor(dom)) {
      g.append(s("line", { x1: x(t), x2: x(t), y1: top, y2: H - axisH + 2, class: "grid" }));
      g.append(s("text", { x: x(t), y: H - 4, "text-anchor": "middle", class: "tick" }, String(+t.toFixed(2))));
    }
    const maxChars = Math.floor((narrow ? W - valW : labW) / 6.4);
    ser.forEach((sr, i) => {
      const yc = top + i * RH + RH / 2 + off, st = sr.st;
      g.append(narrow ? s("text", { x: x0, y: yc - 12, class: "lab" }, clip(sr.label, maxChars))
                      : s("text", { x: labW, y: yc + 4, "text-anchor": "end", class: "lab" }, clip(sr.label, maxChars)));
      if (!st) return;
      g.append(s("line", { x1: x(st.min), x2: x(st.max), y1: yc, y2: yc, class: "whisk thin" }));
      g.append(s("rect", { x: x(st.q1), y: yc - 7, width: Math.max(1, x(st.q3) - x(st.q1)), height: 14, rx: 3, class: "box" }));
      // deterministic jitter so the strip doesn't dance between redraws
      st.v.forEach((v, j) => g.append(s("circle", { cx: x(v), cy: yc + (((j * 7919) % 11) - 5), r: 1.8, class: "dot" })));
      g.append(s("line", { x1: x(st.med), x2: x(st.med), y1: yc - 8, y2: yc + 8, class: "med" }));
      g.append(s("text", { x: W - 2, y: yc + 4, "text-anchor": "end", class: "val" }, fv(st.med)));
      const hit = s("rect", { x: 0, y: yc - RH / 2 - off, width: W, height: RH, fill: "transparent" });
      tip(hit, `${sr.label}\nn ${st.n} · median ${fv(st.med)} · mean ${fv(st.mean)}\nIQR ${fv(st.q1)}–${fv(st.q3)} · range ${fv(st.min)}–${fv(st.max)}`);
      g.append(hit);
    });
    return g;
  };
  return h("div", { class: "card table-card fig" },
    h("div", { class: "fig-head" }, h("h3", {}, fig.title || fig.id), h("span", { class: "muted fig-cap" }, "box = IQR · tick = median · value = median")),
    ser.length ? responsive(draw) : h("div", { class: "muted" }, "no series"));
}

// One row per labelled point (e.g. per test user), one coloured dot per series: joins the same unit across series.
function pointFigure(fig) {
  const labels = fig.point_labels.map(String);
  const ser = (fig.series || []).filter((x) => x && Array.isArray(x.values)).slice(0, SERIES.length);
  const dom = domainOf(ser.flatMap((x) => x.values), false);
  const draw = (W) => {
    const RH = 18, top = 4, axisH = 18, labW = 46, valW = 10;
    const x0 = labW + 8, x1 = W - valW;
    const x = (v) => x0 + ((v - dom[0]) / (dom[1] - dom[0])) * (x1 - x0);
    const H = top + labels.length * RH + axisH;
    const g = s("svg", { width: W, height: H, class: "dist" });
    for (const t of ticksFor(dom)) {
      g.append(s("line", { x1: x(t), x2: x(t), y1: top, y2: H - axisH + 2, class: "grid" }));
      g.append(s("text", { x: x(t), y: H - 4, "text-anchor": "middle", class: "tick" }, String(+t.toFixed(2))));
    }
    labels.forEach((lab, i) => {
      const yc = top + i * RH + RH / 2;
      const vals = ser.map((sr) => num(sr.values[i]));
      const present = vals.filter((v) => v != null);
      g.append(s("text", { x: labW, y: yc + 4, "text-anchor": "end", class: "lab" }, lab));
      if (present.length > 1) g.append(s("line", { x1: x(Math.min(...present)), x2: x(Math.max(...present)), y1: yc, y2: yc, class: "join" }));
      vals.forEach((v, k) => { if (v != null) g.append(s("circle", { cx: x(v), cy: yc, r: 4, class: "sdot", style: `fill:var(${SERIES[k]})` })); });
      const hit = s("rect", { x: 0, y: yc - RH / 2, width: W, height: RH, fill: "transparent" });
      tip(hit, [`${lab}`, ...ser.map((sr, k) => `${sr.label}: ${fv(vals[k])}`)].join("\n"));
      g.append(hit);
    });
    return g;
  };
  const legend = h("div", { class: "viz-legend" }, ser.map((sr, k) => {
    const st = stats(sr.values);
    return h("span", {}, h("i", { class: "lg-sw", style: `background:var(${SERIES[k]})` }), `${sr.label}${st ? " · median " + fv(st.med) : ""}`);
  }));
  return h("div", { class: "card table-card fig" },
    h("div", { class: "fig-head" }, h("h3", {}, fig.title || fig.id), legend),
    labels.length ? responsive(draw) : h("div", { class: "muted" }, "no points"));
}

// ------------------------------------------------------------ dot-bars
// Grouped vertical bars, one colour per model (group) and solid/hatched per variant, each with a 95% CI
// whisker and one dot per test user. Colours come from the documents' series_colors, merged page-wide so
// one model keeps one colour on every figure; the dark theme lifts each hue toward white in CSS.
const HEX = /^#[0-9a-f]{3}([0-9a-f]{3})?$/i;
let patSeq = 0;
function dotColors(figs) {
  const map = new Map();
  for (const f of figs) for (const [k, c] of Object.entries(f.series_colors && typeof f.series_colors === "object" ? f.series_colors : {}))
    if (typeof c === "string" && HEX.test(c) && !map.has(k)) map.set(k, c);
  let i = 0;
  for (const f of figs) for (const k of [...(f.groups || []).map((g) => g?.color_key ?? g?.label), ...(f.reference_lines || []).map((r) => r?.color_key)])
    if (typeof k === "string" && !map.has(k)) map.set(k, `var(${SERIES[i++ % SERIES.length]})`);
  return map;
}
// Greedy word wrap (also breaking after hyphens) into at most `lines` lines of ~`chars` characters;
// the last line is cut with an ellipsis.
function wrapText(t, chars, lines) {
  const out = [];
  let cur = "";
  for (const w of String(t ?? "").split(/\s+/).filter(Boolean).flatMap((w) => w.split(/(?<=-)(?=.)/).map((p, i) => (i ? "\u0000" : "") + p))) {
    const join = w.startsWith("\u0000"), p = join ? w.slice(1) : w, sep = join ? "" : " ";
    if (!cur) cur = p; else if ((cur + sep + p).length <= chars) cur += sep + p; else { out.push(cur); cur = p; }
  }
  if (cur) out.push(cur);
  if (out.length > lines) out.splice(lines - 1, out.length, out.slice(lines - 1).join(" "));
  return out.map((l) => (l.length > chars ? l.slice(0, Math.max(1, chars - 1)) + "…" : l));
}
function hatch(id, color) {
  return s("pattern", { id, class: "db-c db-hatch", style: `--sc:${color}`, width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" },
    s("rect", { width: 6, height: 6 }), s("line", { x1: 3, y1: 0, x2: 3, y2: 6 }));
}
function swatch(hatched) {
  const id = `dbp${++patSeq}`;
  return s("svg", { width: 16, height: 11, class: "lg-db" }, hatched ? s("defs", {}, hatch(id, "var(--ink-2)")) : null,
    s("rect", { x: 0.75, y: 0.75, width: 14.5, height: 9.5, rx: 1.5, class: "db-c db-bar " + (hatched ? "hatched" : "solid"), style: "--sc:var(--ink-2)", fill: hatched ? `url(#${id})` : null }));
}

function dotbarFigure(fig, colors) {
  const groups = (fig.groups || []).filter((g) => g && typeof g === "object")
    .map((g) => ({ ...g, color: colors.get(g.color_key ?? g.label) || "var(--accent)", bars: (Array.isArray(g.bars) ? g.bars : []).filter((b) => b && typeof b === "object") }))
    .filter((g) => g.bars.length);
  const variants = new Map((fig.variants || []).filter((v) => v && typeof v.key === "string").map((v) => [v.key, v]));
  const isHatched = (b) => (variants.get(b.variant)?.fill ?? (b.variant === "without" ? "hatched" : "solid")) === "hatched";
  const vLabel = (b) => variants.get(b.variant)?.label || b.variant || "";
  const okCi = (ci) => Array.isArray(ci) && num(ci[0]) != null && num(ci[1]) != null;
  const ptsOf = (b) => (Array.isArray(b.points) ? b.points.map((v, i) => ({ v: num(v), i })).filter((p) => p.v != null) : []);
  const dec = decOf(fig), fval = (v) => fvd(v, dec), fcid = (ci) => (fci(ci) ? `[${fval(ci[0])}, ${fval(ci[1])}]` : null);
  const base = baseOf(fig);
  const refs = (fig.reference_lines || []).filter((r) => r && num(r.value) != null)
    .map((r) => ({ ...r, dotted: r.style ? r.style === "dotted" : !r.color_key, color: colors.get(r.color_key) || "var(--ink-3)", lines: wrapText(r.label || fval(r.value), 19, 2) }));
  const dom = Array.isArray(fig.domain) && num(fig.domain[0]) != null && num(fig.domain[1]) != null && fig.domain[1] > fig.domain[0]
    ? fig.domain : domainOf([...groups.flatMap((g) => g.bars.flatMap((b) => [b.value, ...(okCi(b.ci) ? b.ci : []), ...ptsOf(b).map((p) => p.v)])), ...refs.map((r) => r.value),
      ...(num(fig.baseline) != null ? [base] : [])], fig.unit === "rate");
  const nb = groups.reduce((a, g) => a + g.bars.length, 0);

  const draw = (W) => {
    const narrow = W < 520, top = 22, plotH = narrow ? 220 : 260, axW = 34;
    // reference labels sit in a right gutter so they never overlap a bar; on narrow screens they become a key below
    const gut = narrow || !refs.length ? 8 : Math.min(130, 12 + Math.max(...refs.flatMap((r) => r.lines.map((l) => l.length))) * 6.2);
    const x0 = axW, x1 = W - gut, gapU = 0.7, inU = 0.15;
    const u = (x1 - x0) / (nb + inU * (nb - groups.length) + gapU * groups.length);
    const bw = Math.min(u, 96);
    const y = (v) => top + plotH - ((Math.max(dom[0], Math.min(dom[1], v)) - dom[0]) / (dom[1] - dom[0])) * plotH;
    const yb = y(Math.max(dom[0], Math.min(dom[1], base)));   // bars grow up or down from the baseline (0 unless set)
    // horizontal layout: bar centres and group extents
    let cur = x0 + (u * gapU) / 2;
    const lay = groups.map((g) => {
      const g0 = cur;
      const cx = g.bars.map((_, j) => { const c = cur + u / 2; cur += u + (j < g.bars.length - 1 ? u * inU : 0); return c; });
      const g1 = cur; cur += u * gapU;
      return { g, cx, g0, g1 };
    });
    // a single-bar group's labels may use the gap around it; bars in a pair only the gap between them
    const subs = lay.map((l) => l.g.bars.map((b) => wrapText(b.sublabel, Math.max(4, Math.floor((u * (1 + (l.g.bars.length === 1 ? gapU * 0.9 : inU * 0.8))) / 5.9)), 3)));
    const grps = lay.map((l) => wrapText(l.g.label, Math.max(4, Math.floor((l.g1 - l.g0 + u * gapU * 0.9) / 6.6)), 3));
    const subN = Math.max(0, ...subs.flat().map((x) => x.length)), grpN = Math.max(1, ...grps.map((x) => x.length));
    const yLab = top + plotH + 8, yGrp = yLab + subN * 13 + 4, yKey = yGrp + grpN * 14 + 8;
    const H = yKey + (narrow ? refs.length * 16 : 0);
    const g = s("svg", { width: W, height: H, class: "dotbars" });
    const defs = s("defs");
    g.append(defs);
    const pat = new Map();
    const patFor = (color) => { if (!pat.has(color)) { const id = `dbp${++patSeq}`; pat.set(color, id); defs.append(hatch(id, color)); } return pat.get(color); };

    for (const t of ticksFor(dom)) {
      g.append(s("line", { x1: x0, x2: x1, y1: y(t), y2: y(t), class: "grid" }));
      g.append(s("text", { x: x0 - 6, y: y(t) + 3.5, "text-anchor": "end", class: "tick" }, String(+t.toFixed(3))));
    }
    const fore = [];  // drawn after the reference lines: hit areas, whiskers, dots, value labels
    lay.forEach(({ g: grp, cx }, gi) => {
      const col = s("g", { class: "db-c", style: `--sc:${grp.color}` });
      g.append(col);
      grp.bars.forEach((b, j) => {
        const v = num(b.value), c = cx[j], ci = okCi(b.ci) ? b.ci : null, pts = ptsOf(b), hat = isHatched(b);
        const r = bw < 30 || pts.length > 30 ? 3.5 : 5;
        if (v != null) {
          const yv = y(v);
          col.append(s("rect", { x: c - bw / 2 + (hat ? 0.75 : 0), y: Math.min(yb, yv), width: bw - (hat ? 1.5 : 0), height: Math.max(0.5, Math.abs(yb - yv)), rx: 1.5,
            class: "db-bar " + (hat ? "hatched" : "solid"), fill: hat ? `url(#${patFor(grp.color)})` : null }));
        }
        const hit = s("rect", { x: c - u / 2, y: top, width: u, height: plotH, fill: "transparent" });
        tip(hit, [`${grp.label || ""} · ${vLabel(b)}${b.sublabel ? " (" + b.sublabel + ")" : ""}`, `value ${fval(v)}${ci ? " · 95% CI " + fcid(ci) : ""}`,
          pts.length ? `${pts.length} point${pts.length === 1 ? "" : "s"}` : null].filter(Boolean).join("\n"));
        fore.push(hit);
        if (ci) {
          const cw = Math.min(16, bw * 0.35);
          fore.push(s("line", { x1: c, x2: c, y1: y(ci[0]), y2: y(ci[1]), class: "db-whisk" }));
          for (const e of ci) fore.push(s("line", { x1: c - cw / 2, x2: c + cw / 2, y1: y(e), y2: y(e), class: "db-whisk" }));
        }
        // dots spread evenly across the bar in data order (deterministic, so redraws don't move them)
        const span = Math.max(0, bw - 2 * r - 6);
        pts.forEach((p, k) => {
          const dot = s("circle", { cx: c - span / 2 + (pts.length === 1 ? span / 2 : (span * k) / (pts.length - 1)), cy: y(p.v), r, class: "db-dot" });
          const lab = Array.isArray(b.point_labels) && b.point_labels[p.i] != null ? String(b.point_labels[p.i]) : `#${p.i + 1}`;
          tip(dot, `${lab}: ${fval(p.v)}\n${grp.label || ""} · ${vLabel(b)}`);
          fore.push(dot);
        });
        if (v != null && v < base) {   // a bar below the baseline: its label goes under its lowest mark
          const yLow = Math.max(y(v), ci ? y(Math.min(...ci)) : -Infinity, ...pts.map((p) => y(p.v) + r));
          fore.push(s("text", { x: c, y: Math.min(top + plotH - 2, yLow + 13), "text-anchor": "middle", class: "val db-val" }, fval(v)));
        } else if (v != null) {
          const yTop = Math.min(y(v), ci ? y(Math.max(...ci)) : Infinity, ...pts.map((p) => y(p.v) - r));
          fore.push(s("text", { x: c, y: Math.max(11, yTop - 6), "text-anchor": "middle", class: "val db-val" }, fval(v)));
        }
        subs[gi][j].forEach((line, k) => g.append(s("text", { x: c, y: yLab + 10 + k * 13, "text-anchor": "middle", class: "db-sub" }, line)));
      });
      const gx = (lay[gi].g0 + lay[gi].g1) / 2;
      grps[gi].forEach((line, k) => col.append(s("text", { x: gx, y: yGrp + 11 + k * 14, "text-anchor": "middle", class: "db-grp" }, line)));
    });
    g.append(s("line", { x1: x0, x2: x1, y1: yb, y2: yb, class: "zero" }));
    // reference lines over the bars; gutter labels nudged apart when two lines are close
    const placed = [];
    [...refs].sort((a, b) => b.value - a.value).forEach((rf) => {
      const yr = y(rf.value);
      g.append(s("line", { x1: x0, x2: x1, y1: yr, y2: yr, class: "db-c db-ref" + (rf.dotted ? " dotted" : ""), style: `--sc:${rf.color}` }));
      if (!narrow) {
        // nudge down past labels already placed (each occupies [y, y + 12 * lines))
        let ly = yr + 3.5;
        for (const p of placed) if (ly < p.end && ly + 12 * rf.lines.length > p.start - 12) ly = p.end;
        placed.push({ start: ly, end: ly + 12 * rf.lines.length });
        const t = s("text", { x: W - 2, y: ly, "text-anchor": "end", class: "db-ref-lab" },
          rf.lines.map((l, k) => s("tspan", { x: W - 2, dy: k ? 12 : 0 }, l)));
        tip(t, `${rf.label || "reference"}: ${fval(rf.value)}`);
        g.append(t);
      }
    });
    if (narrow) refs.forEach((rf, k) => {
      const yy = yKey + k * 16 + 8;
      g.append(s("line", { x1: x0, x2: x0 + 22, y1: yy, y2: yy, class: "db-c db-ref" + (rf.dotted ? " dotted" : ""), style: `--sc:${rf.color}` }),
        s("text", { x: x0 + 28, y: yy + 4, class: "db-ref-lab" }, `${rf.label || "reference"} · ${fval(rf.value)}`));
    });
    g.append(...fore);
    return g;
  };

  const used = [...new Set(groups.flatMap((g) => g.bars.map((b) => b.variant)))];
  const anyPts = groups.some((g) => g.bars.some((b) => ptsOf(b).length)), anyCi = groups.some((g) => g.bars.some((b) => okCi(b.ci)));
  const legend = h("div", { class: "viz-legend" },
    used.map((k) => h("span", {}, swatch(isHatched({ variant: k })), variants.get(k)?.label || k || "value")),
    anyPts || anyCi ? h("span", { class: "muted" }, [anyPts ? pointsLabel(fig) : null, anyCi ? "whiskers: 95% intervals" : null].filter(Boolean).join("; ")) : null);
  const notes = (fig.notes || []).filter((n) => typeof n === "string" && n.trim());
  return h("div", { class: "card table-card fig" },
    h("div", { class: "fig-head" }, h("h3", {}, fig.title || fig.id), fig.unit ? h("span", { class: "muted fig-cap" }, fig.unit) : null),
    fig.subtitle ? h("div", { class: "muted fig-sub" }, fig.subtitle) : null,
    legend,
    nb ? responsive(draw) : h("div", { class: "muted" }, "no bars"),
    notes.length ? h("ul", { class: "fig-notes" }, notes.map((n) => h("li", {}, n))) : null);
}

// ------------------------------------------------------------- gallery
// Image sets a project pushed with tools/gallery_push.py (routes_gallery.py): a grid of thumbnails with the
// boxes drawn over each, filters, a score threshold, and a full-size view with every detail. Images come only
// from the hub's copy of what was pushed. 2,000+ items stay smooth: tiles are built CHUNK at a time as the
// grid scrolls (IntersectionObserver on a sentinel, plus a "show more" button), and a tile is built once and
// kept; a threshold change only redraws the overlays of the tiles shown.
// Galleries are login-only (the hub refuses trusted-network viewers): signed out, the page says so and fetches nothing.
// Per-box review: a box with an "id" can be marked by an operator, in the grid and in the full-size view: left-click
// inside a box = good, right-click = bad; the same click again clears it, the other one flips it. The smallest drawn
// box under the pointer is the one meant. Reviewed boxes are always drawn (green ✓ / red ✗), whatever the slider says.
// Drawn boxes: the teacher misses objects, so an operator can add a box by hand in the full-size view ("Draw box" or D,
// then drag on the image; a label from the set's, or a new one). They come from .../added (not the index), carry a
// hub-made box_id (never an `id`, so the good/bad hit-test never picks one), have no score and are always drawn: solid,
// with a dark outline and "✎" before the label. The box list gives them their own rows (relabel, delete).
const CHUNK = 120;
const encPath = (f) => String(f).split("/").map(encodeURIComponent).join("/");
export const galleryUrl = (project, slug, kind, file) => `/api/v1/projects/${encodeURIComponent(project)}/galleries/${encodeURIComponent(slug)}/${kind}/${encPath(file)}`;
export const boxesAbove = (item, thr) => (Array.isArray(item?.boxes) ? item.boxes : []).filter((b) => num(b?.score) != null && b.score >= thr);
const keptOf = (item) => (item?.tags && typeof item.tags.kept === "boolean" ? item.tags.kept : null);
const fScore = (v) => (num(v) == null ? "—" : v.toFixed(2));
const bboxOf = (b) => (Array.isArray(b?.bbox) && b.bbox.length === 4 && b.bbox.every((v) => num(v) != null) ? b.bbox : null);
const noVerdict = () => null;
const noAdded = () => [];
export const NO_BOX = Symbol("no box");   // never a label (a label can be any string)
export const signedIn = () => state.config?.user?.method === "access";
export const VERDICT_MARK = { good: "✓", bad: "✗" };
export const DRAWN_MARK = "✎";
export const MIN_DRAG_PX = 4;   // a smaller drag (either side, in image pixels) is a slip, not a box

/** The key of one box's verdict (file names have no control characters, so "\n" can't occur in one). */
export const vKey = (file, id) => `${file}\n${id}`;
/** What a click does to a box's verdict: the same verdict again clears it, the other one replaces it. */
export const nextVerdict = (current, clicked) => (current === clicked ? null : clicked);

/** The boxes drawn for an item: those scoring at/above the threshold, plus every reviewed box whatever its score
 *  (slider-independent: a verdict must never hide). vOf(file, id) -> "good" | "bad" | null. */
export function drawnBoxes(item, thr, vOf = noVerdict) {
  return (Array.isArray(item?.boxes) ? item.boxes : [])
    .filter((b) => (num(b?.score) != null && b.score >= thr) || (typeof b?.id === "string" && vOf(item.file, b.id) != null));
}

/** A client point -> image pixel space, from the overlay's on-screen rect and its "xMidYMid meet" scale (the image
 *  is letterboxed inside the rect). null outside the image itself, or with `clamp` the nearest point on it (a drag
 *  that ends outside the image). */
export function toImagePx(rect, W, H, clientX, clientY, clamp = false) {
  const k = Math.min(rect.width / W, rect.height / H);
  if (!(k > 0)) return null;
  const x = (clientX - rect.left - (rect.width - W * k) / 2) / k, y = (clientY - rect.top - (rect.height - H * k) / 2) / k;
  if (clamp) return [Math.min(Math.max(x, 0), W), Math.min(Math.max(y, 0), H)];
  return x >= 0 && y >= 0 && x <= W && y <= H ? [x, y] : null;
}

/** A drag from image point p0 to p1 (either way round) -> [x, y, w, h] clamped to the W x H image and rounded to
 *  0.1 px; null when either side is under `min` px (a click or a slip, not a box) or a point is missing. */
export function dragToBox(p0, p1, W, H, min = MIN_DRAG_PX) {
  if (!Array.isArray(p0) || !Array.isArray(p1) || ![...p0, ...p1, W, H].every((v) => num(v) != null)) return null;
  const cx = (v) => Math.min(Math.max(v, 0), W), cy = (v) => Math.min(Math.max(v, 0), H);
  const x0 = Math.min(cx(p0[0]), cx(p1[0])), x1 = Math.max(cx(p0[0]), cx(p1[0]));
  const y0 = Math.min(cy(p0[1]), cy(p1[1])), y1 = Math.max(cy(p0[1]), cy(p1[1]));
  if (x1 - x0 < min || y1 - y0 < min) return null;
  const r = (v) => Math.round(v * 10) / 10;
  return [r(x0), r(y0), r(x1 - x0), r(y1 - y0)];
}

/** The labels a drawn box can take in the editor: the set's own (index.labels), then any new ones already drawn. */
export function drawLabels(index, added = []) {
  const out = (Array.isArray(index?.labels) ? index.labels : []).filter((l) => typeof l === "string" && l);
  for (const a of added) if (typeof a?.label === "string" && a.label && !out.includes(a.label)) out.push(a.label);
  return out;
}

/** The smallest box (by area) containing image point (x, y), among `boxes` that have an id; null if none. */
export function hitBox(boxes, x, y) {
  let best = null, area = Infinity;
  for (const b of boxes) {
    const bb = bboxOf(b);
    if (!bb || typeof b.id !== "string") continue;
    const [bx, by, w, bh] = bb, a = Math.max(0, w) * Math.max(0, bh);
    if (x >= bx && x <= bx + w && y >= by && y <= by + bh && a < area) { best = b; area = a; }
  }
  return best;
}

/** One item's review: n boxes with an id, how many reviewed, good, bad. */
export function reviewOf(item, vOf = noVerdict) {
  const r = { n: 0, reviewed: 0, good: 0, bad: 0 };
  for (const b of Array.isArray(item?.boxes) ? item.boxes : []) {
    if (typeof b?.id !== "string") continue;
    r.n++;
    const v = vOf(item.file, b.id);
    if (v === "good" || v === "bad") { r.reviewed++; r[v]++; }
  }
  return r;
}
export function reviewCounts(items, vOf = noVerdict) {
  const t = { n: 0, reviewed: 0, good: 0, bad: 0 };
  for (const it of items) { const r = reviewOf(it, vOf); for (const k in t) t[k] += r[k]; }
  return t;
}

/** Items passing the filters: label ("" any, NO_BOX = no box at/above the threshold, else a box with that label
 *  at/above it), session ("" any), kept ("" any, "kept", "dropped"; from tags.kept), review ("" any, "unreviewed" =
 *  a box with an id has no verdict, "bad" = a box marked bad, "done" = has boxes with ids and all are reviewed,
 *  "drawn" = has boxes drawn by hand; verdicts from f.vOf, drawn boxes from f.addedOf(file)). */
export function filterItems(items, f) {
  const vOf = f.vOf || noVerdict, addedOf = f.addedOf || noAdded;
  return items.filter((it) => {
    if (f.review === "drawn" && !addedOf(it.file).length) return false;
    if (f.session !== "" && String(it.session ?? "") !== f.session) return false;
    if (f.kept === "kept" && keptOf(it) !== true) return false;
    if (f.kept === "dropped" && keptOf(it) !== false) return false;
    if (f.review) {
      const r = reviewOf(it, vOf);
      if (f.review === "unreviewed" && r.reviewed >= r.n) return false;
      if (f.review === "bad" && !r.bad) return false;
      if (f.review === "done" && (!r.n || r.reviewed < r.n)) return false;   // ("drawn" was checked above)
    }
    if (f.label === "" || f.label == null) return true;
    const above = boxesAbove(it, f.thr);
    return f.label === NO_BOX ? above.length === 0 : above.some((b) => b.label === f.label);
  });
}

/** One item's boxes as an SVG in the image's own pixel space (viewBox = width x height, top-left origin), laid over
 *  the image and scaled with it; boxes scoring below `thr` are left out unless reviewed (drawnBoxes). A reviewed box
 *  is green with ✓ (good) or red with ✗ (bad): colour and mark, never colour alone. `big` = the full-size view.
 *  `added`: the item's drawn boxes ({box_id, label, bbox}), all of them whatever `thr` (they have no score), drawn
 *  solid over a dark outline in the label's colour with ✎ before the label. */
export function boxOverlay(item, thr, colorOf, big = false, vOf = noVerdict, added = []) {
  const W = item.width, H = item.height;
  const g = s("svg", { class: "gal-ovl", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true" });
  const fs = Math.max(W, H) / (big ? 40 : 16);
  for (const b of drawnBoxes(item, thr, vOf)) {
    const bb = bboxOf(b);
    if (!bb) continue;
    const [x, y, w, bh] = bb;
    const v = typeof b.id === "string" ? vOf(item.file, b.id) : null;
    const col = v === "good" ? "var(--c-green)" : v === "bad" ? "var(--c-red)" : colorOf(b.label);
    g.append(s("rect", { x, y, width: Math.max(0, w), height: Math.max(0, bh), class: "gal-box" + (v ? " gal-" + v : ""), style: `--bc:${col}`,
      "data-bid": typeof b.id === "string" ? b.id : null }));   // lets the full-size view's box list highlight it
    const text = `${v ? VERDICT_MARK[v] + " " : ""}${b.label} ${fScore(b.score)}`;
    const ty = y > fs * 1.2 ? y - fs * 0.3 : y + fs;   // above the box, or inside it at the top edge
    const right = x + text.length * fs * 0.62 > W;      // would run off the right edge: end it at the box's right side
    g.append(s("text", { x: right ? Math.min(W, x + w) - fs * 0.15 : x + fs * 0.15, y: ty, "text-anchor": right ? "end" : null,
      class: "gal-lab", style: `--bc:${col};font-size:${fs}px` }, text));
  }
  for (const a of added) {   // drawn by hand: never filtered by the slider
    const bb = bboxOf(a);
    if (!bb) continue;
    const [x, y, w, bh] = bb, col = colorOf(a.label);
    g.append(s("rect", { x, y, width: Math.max(0, w), height: Math.max(0, bh), class: "gal-drawn-halo" }),
      s("rect", { x, y, width: Math.max(0, w), height: Math.max(0, bh), class: "gal-box gal-drawn", style: `--bc:${col}`, "data-bid": a.box_id }));
    const text = `${DRAWN_MARK} ${a.label}`;
    const ty = y > fs * 1.2 ? y - fs * 0.3 : y + fs;
    const right = x + text.length * fs * 0.62 > W;
    g.append(s("text", { x: right ? Math.min(W, x + w) - fs * 0.15 : x + fs * 0.15, y: ty, "text-anchor": right ? "end" : null,
      class: "gal-lab gal-drawn-lab", style: `--bc:${col};font-size:${fs}px` }, text));
  }
  return g;
}

// Full-size view of list[start]: the image with its boxes and every detail. Esc closes, arrow keys step, D toggles
// draw mode (operators). In draw mode a drag on the image draws a box (click/right-click review is suspended); on
// release an inline editor beside the image asks for its label. While that editor is open (or a drag is under way)
// Esc cancels the pending box only, never the view, Enter saves, and the arrow / D keys are left to the editor.
function galleryModal(ctx, list, start) {
  let i = start, fig = null, boxesEl = null, edSlot = null, drawBtn = null;
  let drag = null, edit = null, justDrew = 0;   // drag: {p0, id}; edit: the pending box's editor ({save, cancel})
  const body = h("div", { class: "gal-modal-body" });
  const close = () => { document.removeEventListener("keydown", onKey); ctx.listeners.delete(paint); wrap.remove(); };
  const go = (d) => { if (list.length) { i = (i + d + list.length) % list.length; draw(); } };
  const typing = (t) => !!t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || "");
  const onKey = (e) => {
    if (edit || drag) {   // a pending drawn box owns Esc / Enter; Esc must not close the view while editing
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancelPending(); }
      else if (e.key === "Enter" && edit) { e.preventDefault(); if (e.target?.classList?.contains("gal-ed-cancel")) cancelPending(); else edit.save(); }
      return;
    }
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); go(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); go(1); }
    else if ((e.key === "d" || e.key === "D") && !e.ctrlKey && !e.metaKey && !e.altKey && !typing(e.target) && canChange()) { e.preventDefault(); toggleDraw(); }
  };
  const row = (k, v) => h("tr", {}, h("th", {}, k), h("td", {}, v == null || v === "" ? "—" : String(v)));
  const KNOWN = ["file", "width", "height", "text", "seed", "session", "tags", "boxes", "_i", "_vrev"];
  const fmtBox = (bb) => (Array.isArray(bb) ? bb.map((x) => (num(x) == null ? "?" : +x.toFixed(1))).join(", ") : "—");
  function paint() {   // the overlay and the box table: redrawn on every verdict / drawn-box change, the image is kept
    const it = list[i], thr = ctx.thr(), added = ctx.addedOf(it.file);
    fig.querySelector(".gal-ovl")?.remove();
    fig.append(boxOverlay(it, thr, ctx.colorOf, true, ctx.vOf, added));
    const boxes = (it.boxes || []).slice().sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
    const r = reviewOf(it, ctx.vOf);
    boxesEl.replaceChildren(
      h("h4", {}, `Boxes (${boxes.length}${added.length ? ` + ${added.length} drawn` : ""}; dimmed = below ${thr.toFixed(2)}${r.n ? `; reviewed ${r.reviewed} / ${r.n}` : ""})`),
      boxes.length || added.length ? h("table", { class: "gal-kv gal-boxes" },
        h("thead", {}, h("tr", {}, h("th", {}, "label"), h("th", { class: "r" }, "score"), h("th", {}, "bbox [x, y, w, h]"), h("th", {}, "source"), h("th", {}, "review"))),
        h("tbody", {}, boxes.map((b) => {
          const v = typeof b.id === "string" ? ctx.vOf(it.file, b.id) : null;
          const id = typeof b.id === "string" ? b.id : null;
          return h("tr", { class: (num(b.score) != null && b.score >= thr) || v ? "" : "below",
            onmouseenter: id ? () => highlight(id, true) : null, onmouseleave: id ? () => highlight(id, false) : null },
            h("td", {}, h("i", { class: "lg-sw", style: `background:${ctx.colorOf(b.label)}` }), " ", b.label),
            h("td", { class: "r num" }, fScore(b.score)),
            h("td", { class: "num" }, fmtBox(b.bbox)),
            h("td", { class: "muted" }, "model"),
            reviewCell(it, b, v));
        }), added.map((a) => h("tr", { class: "gal-drawn-row", onmouseenter: () => highlight(a.box_id, true), onmouseleave: () => highlight(a.box_id, false) },
          h("td", {}, h("i", { class: "lg-sw", style: `background:${ctx.colorOf(a.label)}` }), " ", `${DRAWN_MARK} ${a.label}`),
          h("td", { class: "r num muted" }, "—"),
          h("td", { class: "num" }, fmtBox(a.bbox)),
          h("td", { title: a.by ? `drawn by ${a.by}` : "drawn by hand" }, "drawn"),
          addedCell(it, a)))))
        : h("div", { class: "muted" }, "no boxes"),
      r.n ? h("div", { class: "muted gal-hint" }, canChange()
        ? "Click inside a box: good · right-click: bad · the same click again clears it. Boxes hidden under others: use the ✓ / ✗ buttons in this list."
        : "Sign in to review boxes.") : null,
      canChange() ? h("div", { class: "muted gal-hint" }, "Missed an object? Draw box (D), then drag on the image; pick its label and Save (Enter) or Cancel (Esc).") : null);
  }
  // The review cell of one box row. Operators get ✓ good / ✗ bad buttons, so every box can be reviewed, including one
  // whose area is covered by a smaller box (the image click picks the smallest box under the pointer). Hovering the row
  // highlights its box on the image.
  function reviewCell(it, b, v) {
    const id = typeof b.id === "string" ? b.id : null;
    if (!id) return h("td", { class: "muted", title: "no id: can't be reviewed" }, "n/a");
    if (!canChange()) return h("td", { class: v ? "gal-v-" + v : "muted", title: `box id ${id}` }, v ? `${VERDICT_MARK[v]} ${v}` : "—");
    const btn = (which, label) => h("button", {
      class: "btn small gal-vbtn gal-vbtn-" + which + (v === which ? " active" : ""), "aria-pressed": v === which ? "true" : "false",
      title: v === which ? `marked ${which}: click to clear` : `mark this box ${which}`,
      onclick: (e) => { e.stopPropagation(); ctx.setVerdict(it, id, nextVerdict(v, which)); } }, label);
    return h("td", { class: "gal-vcell", title: `box id ${id}` }, btn("good", `${VERDICT_MARK.good} good`), " ", btn("bad", `${VERDICT_MARK.bad} bad`));
  }
  // A drawn box's cell: operators re-label it (a small select; "other…" opens the editor) or delete it. A box whose
  // POST is still in flight has no hub id yet, so it can't be changed until it lands.
  function addedCell(it, a) {
    if (!canChange()) return h("td", { class: "muted", title: `box id ${a.box_id}` }, a.by ? `by ${a.by}` : "—");
    const saved = !a._tmp;
    const labels = ctx.labels();
    if (!labels.includes(a.label)) labels.push(a.label);
    const sel = h("select", { class: "gal-relabel", "aria-label": "label of this drawn box", title: "re-label", disabled: saved ? null : "" },
      ...labels.map((l, k) => h("option", { value: String(k), selected: l === a.label ? "" : null }, l)), h("option", { value: "other" }, "other…"));
    sel.addEventListener("change", () => {
      if (sel.value === "other") { openEditor(it, a.bbox, a); return; }
      const l = labels[Number(sel.value)];
      if (l != null && l !== a.label) ctx.editAdded(it, a, { label: l });
    });
    const del = h("button", { class: "btn small danger gal-del-drawn", title: "delete this drawn box", disabled: saved ? null : "",
      onclick: (e) => { e.stopPropagation(); ctx.deleteAdded(it, a); } }, "delete");
    return h("td", { class: "gal-vcell", title: `box id ${a.box_id}` }, sel, " ", del);
  }
  function highlight(id, on) {   // the box list's row hover: outline that box on the image (if it is drawn)
    for (const el of fig?.querySelectorAll(".gal-ovl .gal-box") || []) el.classList.toggle("gal-hl", on && el.getAttribute("data-bid") === id);
  }

  // ---- drawing: drag on the image (image pixel space via toImagePx, clamped when the pointer leaves the image)
  function pendLayer() {   // the pending box's own layer (paint() only replaces .gal-ovl)
    const it = list[i];
    return fig.querySelector(".gal-pend") || fig.appendChild(s("svg", { class: "gal-pend", viewBox: `0 0 ${it.width} ${it.height}`, preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true" }));
  }
  function showPending(bb) {
    const layer = pendLayer();
    const r = layer.querySelector("rect") || layer.appendChild(s("rect", { class: "gal-pend-box" }));
    const [x, y, w, bh] = bb;
    for (const [k, v] of Object.entries({ x, y, width: w, height: bh })) r.setAttribute(k, v);
  }
  function cancelPending() {   // drops the drag / the editor's box only; the view and draw mode stay
    const relabel = !!edit?.row;
    drag = null; edit = null;
    fig?.querySelector(".gal-pend")?.remove();
    edSlot?.replaceChildren();
    if (relabel) paint();   // the row's select still says "other…"
  }
  function toggleDraw(on = !ctx.draw.on) {
    if (!canChange()) return;
    ctx.draw.on = on;
    if (!on) cancelPending();
    fig?.classList.toggle("gal-drawing", on);
    if (drawBtn) { drawBtn.classList.toggle("active", on); drawBtn.setAttribute("aria-pressed", on ? "true" : "false"); }
  }
  const imgPx = (e, clamp) => {
    const it = list[i], svg = fig.querySelector(".gal-ovl");
    return svg && toImagePx(svg.getBoundingClientRect(), it.width, it.height, e.clientX, e.clientY, clamp);
  };
  function onDown(e) {
    if (!ctx.draw.on || edit || drag || e.button !== 0 || !canChange()) return;
    const p0 = imgPx(e, false);
    if (!p0) return;   // a drag starts on the image itself
    e.preventDefault();
    try { fig.setPointerCapture(e.pointerId); } catch { /* without capture, moves outside the image still clamp */ }
    drag = { p0, id: e.pointerId };
  }
  function onMove(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const it = list[i], bb = dragToBox(drag.p0, imgPx(e, true), it.width, it.height, 0);
    if (bb) showPending(bb);
  }
  function onUp(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const it = list[i], bb = dragToBox(drag.p0, imgPx(e, true), it.width, it.height);
    drag = null; justDrew = Date.now();
    if (!bb) { cancelPending(); return; }   // a click or a tiny slip: no box
    showPending(bb);
    openEditor(it, bb, null);
  }

  // ---- the inline editor (beside the image, never a browser prompt): label select + "other…" text, Save / Cancel
  function openEditor(it, bbox, row) {   // row: re-label that drawn box; null: a new box at bbox
    const labels = ctx.labels();
    const startLabel = row ? row.label : ctx.draw.last ?? labels[0] ?? "";
    const custom = !labels.includes(startLabel);
    const sel = h("select", { class: "gal-ed-sel", "aria-label": "label of the drawn box" },
      ...labels.map((l, k) => h("option", { value: String(k), selected: !custom && l === startLabel ? "" : null }, l)),
      h("option", { value: "other", selected: custom ? "" : null }, "other…"));
    const txt = h("input", { type: "text", class: "gal-ed-txt", maxlength: "40", placeholder: "new label", "aria-label": "new label", value: custom ? startLabel : "" });
    const syncTxt = () => { txt.hidden = sel.value !== "other"; };
    sel.addEventListener("change", () => { syncTxt(); if (!txt.hidden) txt.focus(); });
    syncTxt();
    const save = () => {
      const label = sel.value === "other" ? txt.value.trim() : labels[Number(sel.value)];
      if (!label || label.length > 40) { txt.hidden = false; txt.classList.add("bad"); txt.focus(); return; }
      ctx.draw.last = label;
      cancelPending();
      if (row) { if (label !== row.label) ctx.editAdded(it, row, { label }); }
      else ctx.addAdded(it, label, bbox);
    };
    edit = { save, cancel: cancelPending, row };
    edSlot.replaceChildren(h("div", { class: "gal-ed", role: "group", "aria-label": row ? "re-label the drawn box" : "label the new box" },
      h("span", { class: "gal-ed-what" }, `${DRAWN_MARK} ${row ? "re-label" : "new box"} ${fmtBox(bbox)}`),
      sel, txt,
      h("button", { class: "btn small primary gal-ed-save", onclick: save, title: "save (Enter)" }, "Save"),
      h("button", { class: "btn small gal-ed-cancel", onclick: cancelPending, title: "cancel (Esc)" }, "Cancel")));
    (txt.hidden ? sel : txt).focus();
  }

  function draw() {
    const it = list[i];
    drag = null; edit = null;   // a pending box belongs to the image it was drawn on
    fig = h("div", { class: "gal-full" + (ctx.draw.on && canChange() ? " gal-drawing" : ""), style: `aspect-ratio:${it.width} / ${it.height}` },
      h("img", { src: galleryUrl(ctx.project, ctx.slug(), "image", it.file), alt: it.file, decoding: "async", draggable: "false" }));
    // review clicks are suspended while drawing (the click that ends a drag must not mark a box)
    fig.addEventListener("click", (e) => { if (ctx.draw.on || edit) { e.preventDefault(); return; } ctx.review(e, it, fig, "good", true); });
    fig.addEventListener("contextmenu", (e) => { if (ctx.draw.on || edit) { e.preventDefault(); return; } ctx.review(e, it, fig, "bad", true); });
    fig.addEventListener("pointerdown", onDown);
    fig.addEventListener("pointermove", onMove);
    fig.addEventListener("pointerup", onUp);
    fig.addEventListener("pointercancel", (e) => { if (drag && e.pointerId === drag.id) cancelPending(); });
    boxesEl = h("div", {});
    edSlot = h("div", { class: "gal-ed-slot" });
    drawBtn = canChange() ? h("button", { class: "btn small gal-draw-btn" + (ctx.draw.on ? " active" : ""), "aria-pressed": ctx.draw.on ? "true" : "false",
      title: "draw a box the model missed: drag on the image (D)", onclick: () => toggleDraw() }, `${DRAWN_MARK} Draw box`) : null;
    body.replaceChildren(
      h("div", { class: "gal-full-wrap" }, fig),
      h("div", { class: "gal-detail" },
        h("div", { class: "gal-nav" },
          h("button", { class: "btn small", onclick: () => go(-1), title: "previous (←)" }, "←"),
          h("span", { class: "muted num" }, `${i + 1} / ${list.length}`),
          h("button", { class: "btn small", onclick: () => go(1), title: "next (→)" }, "→"),
          drawBtn,
          h("button", { class: "btn small gal-close", onclick: close, title: "close (Esc)" }, "Close")),
        edSlot,
        h("table", { class: "gal-kv" }, h("tbody", {},
          row("file", it.file), row("size", `${it.width} × ${it.height}`), row("text", it.text), row("seed", it.seed), row("session", it.session),
          ...Object.entries(it.tags || {}).map(([k, v]) => row(`tags.${k}`, v)),
          ...Object.entries(it).filter(([k]) => !KNOWN.includes(k)).map(([k, v]) => row(k, v)))),
        boxesEl));
    paint();
  }
  // a drag released over the backdrop must not close the view (its click lands on the common ancestor)
  const wrap = h("div", { class: "gal-modal", role: "dialog", "aria-modal": "true", onclick: (e) => { if (e.target === wrap && !drag && Date.now() - justDrew > 400) close(); } }, body);
  document.addEventListener("keydown", onKey);
  ctx.listeners.add(paint);
  document.body.append(wrap);
  draw();
}

/** The Gallery card when there is nothing to browse: signed out (galleries need a login) or no sets pushed. */
export function galleryNote(project, sets) {
  const pub = state.config?.public_url;
  const text = !signedIn()
    ? h("span", {}, "Image galleries need a login: open this page through ",
        pub ? h("a", { href: `${pub.replace(/\/$/, "")}/#/research/${encodeURIComponent(project)}`, class: "ro-link" }, pub.replace(/^[a-z]+:\/\//i, "").replace(/\/.*$/, ""))
          : "the public dashboard address", ".")
    : sets.length ? null : "No image sets pushed yet (tools/gallery_push.py).";
  if (!text) return null;
  return h("div", { class: "card table-card gallery gal-empty" }, h("div", { class: "fig-head" }, h("h3", {}, "Gallery")), h("div", { class: "muted" }, text));
}

/** The Gallery section of a project's research page: always there; the browser when signed in with sets, else a note. */
export const galleryCard = (project, sets) => galleryNote(project, sets) || gallerySection(project, sets);

function gallerySection(project, sets) {
  const root = h("div", { class: "card table-card gallery" });
  const kinds = [...new Set(sets.map((x) => x.kind).filter(Boolean))];
  let verdicts = new Map();   // vKey(file, id) -> {verdict, at, by}: the current index's only (the hub filters)
  const vOf = (file, id) => verdicts.get(vKey(file, id))?.verdict ?? null;
  let added = new Map(), addedByFile = new Map();   // box_id -> drawn box {file, box_id, label, bbox, at, by}; file -> [boxes]
  const addedOf = (file) => addedByFile.get(file) || [];
  const f = { kind: "", slug: sets[0].slug, label: "", session: "", kept: "", review: "", thr: 0, vOf, addedOf };
  let index = null, colors = new Map(), shown = [], tiles = new Map(), upto = 0, raf = 0, loadSeq = 0, labelOpts = [""], byFile = new Map();
  const pending = new Map();   // vKey -> sequence number of the PUT in flight for it (a newer click wins)
  let vSeq = 0;
  const colorOf = (lab) => {
    if (!colors.has(lab)) colors.set(lab, `var(${SERIES[colors.size % SERIES.length]})`);
    return colors.get(lab);
  };
  const ctx = { project, slug: () => f.slug, thr: () => f.thr, colorOf, vOf, review, setVerdict, listeners: new Set(),
    addedOf, addAdded, editAdded, deleteAdded, labels: () => drawLabels(index, [...added.values()]),
    draw: { on: false, last: null } };   // draw mode (and the last label used) outlive stepping and reopening the view

  const opt = (v, label, sel = false) => h("option", { value: v, selected: sel ? "" : null }, label);
  const setSel = h("select", { title: "Image set" });
  const kindSel = h("select", { title: "Kind" }, opt("", "all kinds", true), kinds.map((k) => opt(k, k)));
  const labelSel = h("select", { title: "Label: items with a box of it at or above the threshold" });
  const sessSel = h("select", { title: "Session" });
  const keptSel = h("select", { title: "Kept / dropped (tags.kept)" }, opt("", "kept + dropped", true), opt("kept", "kept"), opt("dropped", "dropped"));
  const reviewSel = h("select", { title: "Review: per-box verdicts (boxes with an id) and boxes drawn by hand" }, opt("", "all (review)", true),
    opt("unreviewed", "has unreviewed boxes"), opt("bad", "has bad boxes"), opt("done", "fully reviewed"), opt("drawn", "has drawn boxes"));
  const thrVal = h("span", { class: "num gal-thr-val" }, "0.00");
  const slider = h("input", { type: "range", min: "0", max: "1", step: "0.01", value: "0", class: "gal-thr", "aria-label": "score threshold" });
  const counts = h("div", { class: "gal-counts muted" });
  const reviewLine = h("div", { class: "gal-counts gal-review muted" });
  const meta = h("div", { class: "research-meta gal-meta" });
  const grid = h("div", { class: "gal-grid" });
  const more = h("button", { class: "btn small gal-more", hidden: "" }, "show more");
  const sentinel = h("div", { class: "gal-sentinel" });
  const delBtn = guard(h("button", { class: "btn small danger", title: "Delete this set from the hub (and its review and drawn boxes)" }, "Delete set"));
  kindSel.hidden = kinds.length < 2;

  // ---- review: hit-test a click in image pixel space, toggle, PUT optimistically, roll back on error
  let roToast = 0;
  function review(e, it, el, which, inModal) {
    const svg = el.querySelector(".gal-ovl");
    const p = svg && toImagePx(svg.getBoundingClientRect(), it.width, it.height, e.clientX, e.clientY);
    if (!p) return false;   // outside the image: the usual behaviour (and the browser's own context menu)
    if (!canChange()) {     // read-only viewer: verdicts are shown, clicks change nothing; no context-menu suppression
      if (which !== "good" || !hitBox(drawnBoxes(it, f.thr, vOf), p[0], p[1])) return false;
      if (inModal && Date.now() - roToast > 4000) { roToast = Date.now(); toast(`Sign in to review boxes (${roTip()})`); }
      return inModal;
    }
    if (which === "bad") e.preventDefault();   // over the image only
    const b = hitBox(drawnBoxes(it, f.thr, vOf), p[0], p[1]);
    if (!b) return false;
    e.preventDefault(); e.stopPropagation();
    setVerdict(it, b.id, nextVerdict(vOf(it.file, b.id), which));
    return true;
  }
  function apply(it, id, rec) {   // one box's verdict changed: the tile, the counts and an open full-size view follow
    const k = vKey(it.file, id);
    if (rec) verdicts.set(k, rec); else verdicts.delete(k);
    it._vrev = (it._vrev || 0) + 1;
    const t = tiles.get(it._i);
    if (t) tileFor(it._i);
    updateReview();
    for (const fn of ctx.listeners) fn();
  }
  function setVerdict(it, id, verdict) {
    const k = vKey(it.file, id), prev = verdicts.get(k) || null, mine = ++vSeq, slug = f.slug;
    pending.set(k, mine);
    apply(it, id, verdict ? { verdict, at: Date.now() / 1000, by: state.config?.user?.subject ?? null } : null);
    post(`/api/v1/projects/${encodeURIComponent(project)}/galleries/${encodeURIComponent(slug)}/verdicts`, { file: it.file, box_id: id, verdict }, "PUT")
      .then((r) => { if (pending.get(k) === mine && slug === f.slug) { pending.delete(k); apply(it, id, r.verdict ? { verdict: r.verdict, at: r.at, by: r.by } : null); } })
      .catch((err) => {
        if (pending.get(k) === mine && slug === f.slug) { pending.delete(k); apply(it, id, prev); }   // roll back
        toast(`Review not saved: ${err.message}`, true);
      });
  }
  // ---- drawn boxes: POST / PATCH / DELETE optimistically, roll back (and say so) on error
  const addedUrl = (slug, id) => `/api/v1/projects/${encodeURIComponent(project)}/galleries/${encodeURIComponent(slug)}/added${id ? "/" + encodeURIComponent(id) : ""}`;
  const addedRec = (r) => ({ file: r.file, box_id: r.box_id, label: r.label, bbox: r.bbox, at: r.at, by: r.by });
  const pendingAdded = new Map();   // box_id -> sequence number of the PATCH / DELETE in flight for it (a newer one wins)
  function reindexAdded() {
    addedByFile = new Map();
    for (const a of added.values()) (addedByFile.get(a.file) || addedByFile.set(a.file, []).get(a.file)).push(a);
  }
  function applyAdded(it, id, rec) {   // one drawn box created / changed (rec) or gone (null): tile, counts and view follow
    if (rec) added.set(id, rec); else added.delete(id);
    reindexAdded();
    it._vrev = (it._vrev || 0) + 1;
    if (tiles.get(it._i)) tileFor(it._i);
    updateCounts();
    updateReview();
    for (const fn of ctx.listeners) fn();
  }
  function addAdded(it, label, bbox) {
    const tmp = `tmp-${++vSeq}`, slug = f.slug;   // shown at once under a temporary id; the hub's id replaces it
    applyAdded(it, tmp, { file: it.file, box_id: tmp, label, bbox, at: Date.now() / 1000, by: state.config?.user?.subject ?? null, _tmp: true });
    post(addedUrl(slug), { file: it.file, label, bbox })
      .then((r) => { if (slug === f.slug) { added.delete(tmp); applyAdded(it, r.box_id, addedRec(r)); } })
      .catch((err) => {
        if (slug === f.slug) applyAdded(it, tmp, null);   // roll back
        toast(`Drawn box not saved: ${err.message}`, true);
      });
  }
  function changeAdded(it, a, patch) {   // patch null: delete
    const id = a.box_id, prev = added.get(id) || null, mine = ++vSeq, slug = f.slug;
    if (!prev || prev._tmp) return;
    pendingAdded.set(id, mine);
    applyAdded(it, id, patch ? { ...prev, ...patch } : null);
    post(addedUrl(slug, id), patch || undefined, patch ? "PATCH" : "DELETE")
      .then((r) => { if (pendingAdded.get(id) === mine && slug === f.slug) { pendingAdded.delete(id); if (patch) applyAdded(it, id, addedRec(r)); } })
      .catch((err) => {
        if (pendingAdded.get(id) === mine && slug === f.slug) { pendingAdded.delete(id); applyAdded(it, id, prev); }   // roll back
        toast(`Drawn box not ${patch ? "changed" : "deleted"}: ${err.message}`, true);
      });
  }
  function editAdded(it, a, patch) { changeAdded(it, a, patch); }
  function deleteAdded(it, a) { changeAdded(it, a, null); }
  // live: drawn boxes created, changed or deleted by another operator (or echoes of this page's own)
  const offAdded = on("gallery_added", (m) => {
    if (!root.isConnected) { offAdded(); return; }
    if (m.project !== project || m.slug !== f.slug || !index) return;
    const it = byFile.get(m.file);
    if (!it || typeof m.box_id !== "string" || pendingAdded.has(m.box_id)) return;   // ours in flight: its answer settles it
    applyAdded(it, m.box_id, m.op === "delete" ? null : addedRec(m));
  });
  function updateCounts() {
    if (!index) return;
    let nBoxes = 0, nDrawn = 0;
    for (const j of shown) { nBoxes += boxesAbove(index.items[j], f.thr).length; nDrawn += addedOf(index.items[j].file).length; }
    counts.textContent = `showing ${shown.length} of ${index.items.length}, ${nBoxes} box${nBoxes === 1 ? "" : "es"} ≥ ${f.thr.toFixed(2)}, drawn ${nDrawn}`;
  }
  function updateReview() {
    if (!index) return;
    const c = reviewCounts(index.items, vOf);
    reviewSel.hidden = !c.n && !added.size;
    reviewLine.textContent = c.n ? `reviewed ${c.reviewed} / ${c.n} boxes (good ${c.good}, bad ${c.bad})` : "no box ids in this set: it can't be reviewed";
  }
  // live: another operator's (or this page's own) verdicts
  const off = on("gallery_verdict", (m) => {
    if (!root.isConnected) { off(); return; }   // the page was re-rendered or left (the section is appended in the same tick it is built)
    if (m.project !== project || m.slug !== f.slug || !index) return;
    const it = byFile.get(m.file);
    if (!it || pending.has(vKey(m.file, m.box_id))) return;   // a click of ours is in flight: its answer settles it
    if (!(it.boxes || []).some((b) => b.id === m.box_id)) return;
    apply(it, m.box_id, m.verdict ? { verdict: m.verdict, at: m.at, by: m.by } : null);
  });

  function fillSets() {
    const avail = sets.filter((x) => f.kind === "" || x.kind === f.kind);
    if (!avail.some((x) => x.slug === f.slug) && avail.length) f.slug = avail[0].slug;
    setSel.replaceChildren(...avail.map((x) => opt(x.slug, `${x.set} (${x.items})`, x.slug === f.slug)));
  }
  function tileFor(j) {
    let t = tiles.get(j);
    const it = index.items[j];
    if (!t) {
      const reviewable = (it.boxes || []).some((b) => typeof b.id === "string");
      t = h("button", { class: "gal-tile", title: it.file + (!reviewable ? "" : canChange() ? " · click a box: good · right-click: bad (again: clear)" : " · sign in to review boxes"),
        onclick: (e) => { if (!review(e, it, t, "good", false)) galleryModal(ctx, shown.map((k) => index.items[k]), shown.indexOf(j)); } },
        h("img", { src: galleryUrl(project, f.slug, "thumb", it.file), alt: it.file, loading: "lazy", decoding: "async" }));
      t.addEventListener("contextmenu", (e) => review(e, it, t, "bad", false));
      tiles.set(j, t);
    }
    if (t._thr !== f.thr || t._vrev !== (it._vrev || 0)) {   // (re)draw the overlay for the threshold and verdicts
      t.querySelector(".gal-ovl")?.remove();
      t.append(boxOverlay(it, f.thr, colorOf, false, vOf, addedOf(it.file)));
      t._thr = f.thr; t._vrev = it._vrev || 0;
    }
    return t;
  }
  function renderMore() {
    const next = Math.min(shown.length, upto + CHUNK);
    grid.append(...shown.slice(upto, next).map(tileFor));
    upto = next;
    more.hidden = upto >= shown.length;
  }
  function refresh(keep = false) {   // keep: as many tiles as were already shown (a threshold move keeps the scroll)
    const want = keep ? Math.max(upto, CHUNK) : CHUNK;
    shown = filterItems(index.items, f).map((it) => it._i);
    updateCounts();
    updateReview();
    grid.replaceChildren();
    upto = 0;
    while (upto < Math.min(want, shown.length)) renderMore();
    more.hidden = upto >= shown.length;
    if (!shown.length) grid.append(h("div", { class: "muted gal-none" }, "No items match these filters."));
  }
  async function loadSet() {
    const seq = ++loadSeq;
    index = null;
    grid.replaceChildren(h("div", { class: "muted gal-none" }, "Loading…"));
    const base = `/api/v1/projects/${encodeURIComponent(project)}/galleries/${encodeURIComponent(f.slug)}`;
    let doc, vs, ad;
    try {
      [doc, vs, ad] = await Promise.all([api(base), api(base + "/verdicts").catch((e) => { toast(`Cannot load the review: ${e.message}`, true); return { verdicts: [] }; }),
        api(base + "/added").catch((e) => { toast(`Cannot load the drawn boxes: ${e.message}`, true); return { added: [] }; })]);
    } catch (e) { if (seq === loadSeq) grid.replaceChildren(h("div", { class: "muted gal-none" }, `Cannot load this set: ${e.message}`)); return; }
    if (seq !== loadSeq) return;   // another set was picked meanwhile
    doc.items = (Array.isArray(doc.items) ? doc.items : []).filter((it) => it && num(it.width) > 0 && num(it.height) > 0);
    doc.items.forEach((it, j) => { it._i = j; });
    index = doc; tiles = new Map(); colors = new Map(); pending.clear(); pendingAdded.clear();
    byFile = new Map(doc.items.map((it) => [it.file, it]));
    added = new Map((ad?.added || []).filter((a) => typeof a?.box_id === "string" && byFile.has(a.file)).map((a) => [a.box_id, addedRec(a)]));
    reindexAdded();
    verdicts = new Map((vs?.verdicts || []).filter((v) => v.verdict === "good" || v.verdict === "bad").map((v) => [vKey(v.file, v.box_id), { verdict: v.verdict, at: v.at, by: v.by }]));
    for (const l of [...(doc.labels || []), ...doc.items.flatMap((it) => (it.boxes || []).map((b) => b.label)), ...[...added.values()].map((a) => a.label)]) colorOf(l);
    const sessions = [...new Set(doc.items.map((it) => it.session).filter((v) => v != null).map(String))].sort();
    f.label = ""; f.session = ""; f.review = ""; reviewSel.value = "";
    // option values index labelOpts (labels are arbitrary strings; NO_BOX is not a string at all)
    labelOpts = ["", ...colors.keys(), NO_BOX];
    labelSel.replaceChildren(...labelOpts.map((l, k) => opt(String(k), l === "" ? "any label" : l === NO_BOX ? "no box ≥ threshold" : l, k === 0)));
    sessSel.replaceChildren(opt("", "all sessions", true), ...sessions.map((x) => opt(x, x)));
    sessSel.hidden = !sessions.length;
    keptSel.hidden = !doc.items.some((it) => keptOf(it) != null);
    const m = sets.find((x) => x.slug === f.slug) || {};
    meta.replaceChildren(...[
      m.kind ? h("span", { class: "chip" }, m.kind) : null,
      h("span", {}, `${doc.items.length} items · ${fmt.bytes(m.bytes)}`),
      h("span", {}, `pushed ${m.pushed_at ? fmt.ago(Date.now() / 1000 - m.pushed_at) : "?"}${m.host ? " from " + m.host : ""}`),
      m.thumbs === false ? h("span", {}, "no thumbnails: tiles load full images") : null,
      h("span", { class: "viz-legend" }, [...colors.keys()].map((l) => h("span", {}, h("i", { class: "lg-sw", style: `background:${colorOf(l)}` }), l)),
        h("span", { class: "gal-v-good" }, `${VERDICT_MARK.good} good`), h("span", { class: "gal-v-bad" }, `${VERDICT_MARK.bad} bad`),
        h("span", { class: "gal-drawn-key" }, `${DRAWN_MARK} drawn`)),
    ].filter(Boolean));
    refresh();
  }

  setSel.addEventListener("change", () => { f.slug = setSel.value; loadSet(); });
  kindSel.addEventListener("change", () => { f.kind = kindSel.value; const before = f.slug; fillSets(); if (f.slug !== before) loadSet(); });
  labelSel.addEventListener("change", () => { f.label = labelOpts[Number(labelSel.value)] ?? ""; if (index) refresh(); });
  sessSel.addEventListener("change", () => { f.session = sessSel.value; if (index) refresh(); });
  keptSel.addEventListener("change", () => { f.kept = keptSel.value; if (index) refresh(); });
  reviewSel.addEventListener("change", () => { f.review = reviewSel.value; if (index) refresh(); });
  slider.addEventListener("input", () => {
    f.thr = Number(slider.value) || 0; thrVal.textContent = f.thr.toFixed(2);
    cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { if (index) refresh(true); });
  });
  more.addEventListener("click", renderMore);
  delBtn.addEventListener("click", async () => {
    const m = sets.find((x) => x.slug === f.slug);
    if (!m || !confirm(`Delete the image set "${m.set}" (${m.items} items), its review and its drawn boxes from the hub?`)) return;
    try { await post(`/api/v1/projects/${encodeURIComponent(project)}/galleries/${encodeURIComponent(m.slug)}`, undefined, "DELETE"); toast(`Deleted ${m.set}`); renderResearch({ project }); }
    catch (e) { toast(e.message, true); }
  });
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver((es) => {
      if (!sentinel.isConnected) { io.disconnect(); return; }   // the page was left
      if (es.some((e) => e.isIntersecting) && index && upto < shown.length) renderMore();
    }, { rootMargin: "600px" });
    io.observe(sentinel);
  }

  fillSets();
  root.append(
    h("div", { class: "fig-head" }, h("h3", {}, "Gallery"), h("span", { class: "muted fig-cap" }, `${sets.length} image set${sets.length === 1 ? "" : "s"} · visible to signed-in dashboard viewers`)),
    h("div", { class: "gal-controls" }, setSel, kindSel, labelSel, sessSel, keptSel, reviewSel,
      h("label", { class: "gal-thr-wrap", title: "Hide boxes scoring below this (reviewed boxes always show)" }, "score ≥", slider, thrVal), delBtn),
    meta, counts, reviewLine, grid, more, sentinel);
  loadSet();
  return root;
}

// ---------------------------------------------------------------- page
const view = { project: null, push: null };

export async function renderResearch(params = {}) {
  const app = $("#app"); app.innerHTML = "";
  state.view = "research";
  let projects;
  try { projects = (await api("/api/v1/projects")).projects; } catch (e) { app.append(h("div", { class: "empty" }, `Cannot load projects: ${e.message}`)); return; }
  if (!projects.length) {
    app.append(h("div", { class: "page-head" }, h("h2", {}, "Research")),
      h("div", { class: "empty" }, "No results pushed yet. From any machine with the agent: ", h("code", {}, "avm results push <project> results.json")));
    return;
  }
  const project = params.project && projects.some((p) => p.project === params.project) ? params.project : projects[0].project;
  view.project = project; view.push = params.push ? Number(params.push) : null;
  const pinfo = projects.find((p) => p.project === project) || {};
  const projSel = projects.length > 1 ? h("select", { onchange: (e) => navigate(`#/research/${e.target.value}`) },
    projects.map((p) => h("option", { value: p.project, selected: p.project === project ? "" : null }, p.project))) : null;
  // galleries are login-only: signed out, nothing is fetched and the section says how to sign in
  const galleries = signedIn() && pinfo.galleries ? api(`/api/v1/projects/${project}/galleries`).then((r) => r.galleries || []).catch(() => []) : Promise.resolve([]);
  if (!pinfo.pushes) {   // a project with image galleries only
    const sets = await galleries;
    if (state.view !== "research" || view.project !== project) return;
    app.append(h("div", { class: "page-head" }, h("h2", {}, "Research"), h("span", { class: "muted" }, project), h("div", { class: "filters" }, projSel)),
      galleryCard(project, sets));
    return;
  }
  let cur, hist, sets;
  try {
    [cur, hist, sets] = await Promise.all([api(`/api/v1/projects/${project}/results${view.push ? "?push=" + view.push : ""}`), api(`/api/v1/projects/${project}/results/history`), galleries]);
  } catch (e) { app.append(h("div", { class: "empty" }, `Cannot load results: ${e.message}`)); return; }
  if (state.view !== "research" || view.project !== project) return;  // navigated away meanwhile
  const pushes = hist.pushes;
  const latestId = pushes.length ? pushes[pushes.length - 1].id : cur.id;
  const doc = cur.doc || {};

  const pushSel = h("select", { title: "View an earlier push", onchange: (e) => navigate(`#/research/${project}${Number(e.target.value) === latestId ? "" : "?push=" + e.target.value}`) },
    [...pushes].reverse().map((p) => h("option", { value: p.id, selected: p.id === cur.id ? "" : null },
      `#${p.id} · ${fmt.when(p.received)}${p.commit ? " · " + p.commit : ""}${p.id === latestId ? " (latest)" : ""}`)));
  const meta = h("div", { class: "research-meta" },
    h("span", { class: "chip" + (cur.id === latestId ? " run" : " warn") }, cur.id === latestId ? "latest" : "earlier push"),
    h("span", {}, `generated ${doc.generated_at ? new Date(doc.generated_at).toLocaleString() : "?"}`),
    cur.commit ? h("span", { class: "num" }, `commit ${cur.commit}`) : null,
    h("span", {}, `pushed ${fmt.ago(Date.now() / 1000 - cur.received)}${cur.host ? " from " + cur.host : ""}`),
    h("span", {}, `${pushes.length} push${pushes.length === 1 ? "" : "es"} in history`));

  const notes = (doc.notes || []).filter((n) => typeof n === "string" && n.trim());
  const notesEl = notes.length ? h("div", { class: "card research-notes" }, h("h3", {}, "Read before quoting any number"), h("ul", {}, notes.map((n) => h("li", {}, n)))) : null;

  const dots = (doc.dotbars || []).filter((f) => f && typeof f === "object");
  const dotColorMap = dotColors(dots);
  const figs = [
    ...(doc.bars || []).filter((f) => f && typeof f === "object").map(barFigure),
    ...(doc.distributions || []).filter((f) => f && typeof f === "object").map((f) => (Array.isArray(f.point_labels) && f.point_labels.length ? pointFigure(f) : stripFigure(f))),
  ];
  app.append(...[
    h("div", { class: "page-head" }, h("h2", {}, "Research"), h("span", { class: "muted" }, project), h("div", { class: "filters" }, projSel, pushSel)),
    meta,
    headlineTable(doc, pushes, cur.id) || h("div", { class: "empty" }, "This push has no headline results."),
    dots.length ? h("div", { class: "research-dotbars dotbars-sec" }, dots.map((f) => dotbarFigure(f, dotColorMap))) : null,
    notesEl,
    programmes(doc),
    figs.length ? h("div", { class: "research-figs" }, figs) : null,
    galleryCard(project, sets),
  ].filter(Boolean));
}

on("results", (m) => {
  if (state.view !== "research") return;
  if (m.project === view.project && !view.push) { toast(`New ${m.project} results (#${m.id})`); renderResearch({ project: view.project }); }
  else if (!view.project) renderResearch({});
});
// a gallery push or delete: say so, without reloading the page under someone browsing it
on("gallery", (m) => {
  if (state.view === "research" && m.project === view.project && !document.querySelector(".gal-modal"))
    toast(`${m.project} gallery ${m.deleted ? "set deleted" : "updated"}: ${m.slug} (reopen the page to see it)`);
});
