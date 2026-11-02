/* Training Dashboard front end. Plain JS + Chart.js; all data comes from /api/dashboard. */
(() => {
  "use strict";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const state = { data: null, range: 90, efSport: "run", charts: {}, conv: null, chatBusy: false };

  // ---------- utilities ----------
  async function api(path, opts = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" }, ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    });
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.detail ? (typeof json.detail === "string" ? json.detail : JSON.stringify(json.detail)) : res.statusText);
    return json;
  }
  let toastTimer;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 5000);
  }
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const fmtDate = (s) => new Date(s + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const fmtClock = (m) => { if (m == null) return ""; m = Math.round(m) % 1440; return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; };
  const fmtDur = (s) => { if (!s) return "–"; const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60); return h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m}m`; };
  const rolling = (arr, n) => arr.map((_, i) => {
    const w = arr.slice(Math.max(0, i - n + 1), i + 1).filter((v) => v != null);
    return w.length >= Math.ceil(n / 2) ? +(w.reduce((a, b) => a + b, 0) / w.length).toFixed(1) : null;
  });
  const el = (tag, props = {}, ...kids) => {
    const n = document.createElement(tag);
    Object.entries(props).forEach(([k, v]) => (k === "class" ? (n.className = v) : k === "text" ? (n.textContent = v) : n.setAttribute(k, v)));
    kids.forEach((k) => k != null && n.append(k));
    return n;
  };

  // ---------- Chart.js setup ----------
  const crosshair = {
    id: "crosshair",
    afterDraw(chart) {
      const act = chart.tooltip?.getActiveElements?.();
      if (!act?.length || chart.config.type !== "line") return;
      const x = act[0].element.x, { top, bottom } = chart.chartArea, ctx = chart.ctx;
      ctx.save(); ctx.strokeStyle = css("--text-muted"); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bottom); ctx.stroke(); ctx.restore();
    },
  };
  // Shaded horizontal bands + reference lines: options.plugins.refs = {bands:[{from,to,color}], lines:[{y,label}]}
  const refs = {
    id: "refs",
    beforeDatasetsDraw(chart, _a, o) {
      const { ctx, chartArea: ca, scales: { y } } = chart;
      if (!o || !y) return;
      ctx.save();
      (o.bands || []).forEach((b) => {
        const y1 = y.getPixelForValue(Math.min(b.to, y.max)), y2 = y.getPixelForValue(Math.max(b.from, y.min));
        ctx.fillStyle = b.color; ctx.fillRect(ca.left, Math.min(y1, y2), ca.width, Math.abs(y2 - y1));
      });
      (o.lines || []).forEach((l) => {
        if (l.y == null || l.y < y.min || l.y > y.max) return;
        const py = y.getPixelForValue(l.y);
        ctx.strokeStyle = css("--text-muted"); ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(ca.left, py); ctx.lineTo(ca.right, py); ctx.stroke(); ctx.setLineDash([]);
        if (l.label) { ctx.fillStyle = css("--text-secondary"); ctx.font = "11px system-ui"; ctx.textAlign = "right"; ctx.fillText(l.label, ca.right - 4, py - 4); }
      });
      ctx.restore();
    },
  };
  // Direct label at the last point of each line series (selective labelling: endpoints only).
  const endLabels = {
    id: "endLabels",
    afterDatasetsDraw(chart, _a, o) {
      if (!o?.enabled) return;
      const { ctx } = chart;
      ctx.save(); ctx.font = "600 11px system-ui"; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      chart.data.datasets.forEach((ds, i) => {
        if (ds.endLabel === false) return;
        const meta = chart.getDatasetMeta(i);
        if (meta.hidden) return;
        for (let j = meta.data.length - 1; j >= 0; j--) {
          const v = ds.data[j];
          if (v != null && !Number.isNaN(v)) {
            const p = meta.data[j];
            ctx.fillStyle = css("--text-primary");
            ctx.fillText(`${ds.short || ds.label} ${Math.round(v)}`, p.x + 6, p.y);
            break;
          }
        }
      });
      ctx.restore();
    },
  };
  Chart.register(crosshair, refs, endLabels);

  function baseOptions(extra = {}) {
    const grid = css("--grid"), text = css("--text-secondary");
    Chart.defaults.color = text;
    Chart.defaults.font.family = "system-ui, -apple-system, Segoe UI, Roboto, sans-serif";
    Chart.defaults.font.size = 11;
    return deepMerge({
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      layout: { padding: { right: 8 } },
      scales: {
        x: { grid: { display: false }, border: { color: grid }, ticks: { maxRotation: 0, autoSkipPadding: 16, color: text } },
        y: { grid: { color: grid }, border: { display: false }, ticks: { color: text, maxTicksLimit: 6 } },
      },
      plugins: {
        legend: { display: false, align: "start", labels: { boxWidth: 14, boxHeight: 2, color: text, usePointStyle: false } },
        tooltip: {
          backgroundColor: css("--surface-1"), titleColor: css("--text-secondary"), bodyColor: css("--text-primary"),
          borderColor: css("--border"), borderWidth: 1, padding: 10, boxWidth: 10, boxHeight: 2, bodyFont: { weight: "600" },
        },
      },
    }, extra);
  }
  function deepMerge(a, b) {
    for (const [k, v] of Object.entries(b)) {
      if (v && typeof v === "object" && !Array.isArray(v) && a[k] && typeof a[k] === "object") deepMerge(a[k], v);
      else a[k] = v;
    }
    return a;
  }
  function chart(id, config) {
    state.charts[id]?.destroy();
    const canvas = document.getElementById(id);
    if (!canvas) return;
    state.charts[id] = new Chart(canvas, config);
  }
  const line = (label, data, color, extra = {}) => ({
    label, data, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: 0,
    pointHoverRadius: 4, pointHoverBorderWidth: 2, pointHoverBorderColor: css("--surface-1"), tension: 0.25, spanGaps: true, ...extra,
  });
  const bars = (label, data, color, extra = {}) => ({
    label, data, backgroundColor: color, borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: "start",
    borderWidth: 0, maxBarThickness: 18, ...extra,
  });

  // ---------- routing ----------
  function showTab() {
    const tab = (location.hash.slice(1).split("?")[0]) || "overview";
    $$(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + tab));
    $$(".tabs a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
    const q = new URLSearchParams(location.hash.split("?")[1] || "");
    if (q.get("strava") === "connected") { toast("Strava connected — syncing…"); history.replaceState(null, "", "#settings"); doSync("strava"); }
    if (q.get("strava") === "denied") toast("Strava connection was cancelled.");
    if (q.get("strava") === "scope") toast("Please allow 'View data about your activities' when connecting Strava.");
    if (tab === "settings") loadStatus();
    if (tab === "habits") loadHabit($("#habit-form [name=date]").value);
    if (tab === "coach") { loadChatList(); loadCoachStatus(); setTimeout(() => $("#chat-input").focus(), 50); }
    // Charts in hidden tabs have zero size; redraw the visible ones.
    requestAnimationFrame(() => Object.values(state.charts).forEach((c) => c.resize()));
  }

  // ---------- data load ----------
  async function load() {
    document.querySelector("main").classList.add("loading");
    try {
      state.data = await api(`/api/dashboard?days=${state.range}`);
      render();
    } catch (e) {
      toast("Could not load dashboard: " + e.message);
    } finally {
      document.querySelector("main").classList.remove("loading");
    }
  }

  function render() {
    renderOverview();
    renderTraining();
    renderRecovery();
    renderHabits();
  }

  // ---------- overview ----------
  const SEV = {
    critical: ["!", "Critical"], serious: ["!", "Serious"], warning: ["▲", "Warning"], good: ["✓", "Good"], info: ["i", "Note"],
  };
  function renderOverview() {
    const d = state.data, t = d.tiles;
    $("#ready-score").textContent = d.readiness.score;
    $("#ready-label").textContent = d.readiness.label;
    $("#ready-bar").style.width = d.readiness.score + "%";

    const tiles = [
      ["Fitness", t.fitness?.toFixed(0), "", t.fitness_change != null ? `${t.fitness_change >= 0 ? "+" : ""}${t.fitness_change} this week` : ""],
      ["Fatigue", t.fatigue?.toFixed(0), "", "7-day load"],
      ["Form", t.form != null ? (t.form > 0 ? "+" : "") + t.form.toFixed(0) : null, "", formLabel(t.form)],
      ["Training this week", t.week_hours, "h", "last 7 days"],
      ["Resting HR", t.resting_hr?.toFixed(0), "bpm", "latest"],
      ["HRV", t.hrv?.toFixed(0), "ms", "last night"],
      ["Sleep", t.sleep_avg_h, "h", "7-day average"],
      ["Drinks", t.drinks_7d, "", "last 7 days"],
      ["VO₂ max", t.vo2max?.toFixed?.(1), "", "Garmin estimate"],
    ];
    const wrap = $("#tiles"); wrap.replaceChildren();
    tiles.forEach(([label, value, unit, delta]) => {
      const v = el("div", { class: "value" }, value ?? "–");
      if (value != null && unit) v.append(el("small", { text: unit }));
      wrap.append(el("div", { class: "tile" }, el("div", { class: "label", text: label }), v, el("div", { class: "delta", text: delta || "" })));
    });

    const ul = $("#findings"); ul.replaceChildren();
    if (!d.findings.length) ul.append(el("li", { class: "finding good" }, el("div", { class: "title", text: "No issues detected" })));
    d.findings.forEach((f) => {
      const [icon, word] = SEV[f.severity] || SEV.info;
      const li = el("li", { class: "finding " + f.severity },
        el("div", { class: "head" },
          el("span", { class: "badge" }, el("i", { text: icon, "aria-hidden": "true" }), word),
          el("span", { class: "title", text: f.title }),
          el("span", { class: "cat", text: f.category })),
        el("p", { text: f.detail }));
      if (f.action) li.append(el("p", { class: "action", text: "→ " + f.action }));
      ul.append(li);
    });

    fitnessChart("c-fitness-overview");
  }
  const formLabel = (tsb) => tsb == null ? "" : tsb < -30 ? "high risk" : tsb < -10 ? "training" : tsb < 5 ? "neutral" : tsb < 25 ? "fresh" : "very fresh";

  function fitnessChart(id) {
    const L = state.data.load;
    chart(id, {
      type: "line",
      data: {
        labels: L.map((r) => fmtDate(r.date)),
        datasets: [
          line("Fitness (CTL)", L.map((r) => r.ctl), css("--series-1"), { short: "Fitness" }),
          line("Fatigue (ATL)", L.map((r) => r.atl), css("--series-2"), { short: "Fatigue" }),
        ],
      },
      options: baseOptions({
        layout: { padding: { right: 78 } },
        plugins: { legend: { display: true }, endLabels: { enabled: true } },
      }),
    });
  }

  // ---------- training ----------
  const SPORTS = [["run", "Run", "--series-1"], ["ride", "Ride", "--series-2"], ["swim", "Swim", "--series-3"], ["strength", "Strength", "--series-4"]];
  function renderTraining() {
    const d = state.data;
    fitnessChart("c-fitness");

    const L = d.load;
    chart("c-form", {
      type: "bar",
      data: { labels: L.map((r) => fmtDate(r.date)), datasets: [bars("Form (TSB)", L.map((r) => r.tsb), css("--series-1"), { borderRadius: 2, borderSkipped: false, maxBarThickness: 8 })] },
      options: baseOptions({
        interaction: { mode: "index", intersect: false },
        plugins: { refs: { bands: [{ from: -30, to: -10, color: css("--band") }, { from: -999, to: -30, color: css("--band-risk") }], lines: [{ y: 0 }] } },
      }),
    });

    // Weekly volume, stacked by sport (fixed colour per sport; everything else folds into "Other").
    const W = d.weekly;
    const known = new Set(SPORTS.map((s) => s[0]));
    const ds = SPORTS.map(([k, label, c]) => bars(label, W.map((w) => +(w.by_sport[k] || 0).toFixed(2)), css(c), { stack: "s", borderRadius: 2, borderSkipped: false, borderColor: css("--surface-1"), borderWidth: { top: 2 } }))
      .filter((s) => s.data.some((v) => v > 0));
    const other = W.map((w) => +Object.entries(w.by_sport).filter(([k]) => !known.has(k)).reduce((a, [, v]) => a + v, 0).toFixed(2));
    if (other.some((v) => v > 0)) ds.push(bars("Other", other, css("--series-other"), { stack: "s", borderRadius: 2, borderSkipped: false, borderColor: css("--surface-1"), borderWidth: { top: 2 } }));
    chart("c-weekly", {
      type: "bar",
      data: { labels: W.map((w) => fmtDate(w.week)), datasets: ds },
      options: baseOptions({
        scales: { x: { stacked: true }, y: { stacked: true, title: { display: true, text: "hours" } } },
        plugins: {
          legend: { display: ds.length > 1, labels: { boxWidth: 10, boxHeight: 10 } },
          tooltip: { callbacks: { footer: (items) => `Total ${items.reduce((a, i) => a + i.parsed.y, 0).toFixed(1)} h` } },
        },
      }),
    });

    renderEfficiency();

    const vo2 = state.data.days;
    const hasRun = vo2.some((r) => r.vo2max_run), hasRide = vo2.some((r) => r.vo2max_ride);
    const vds = [];
    if (hasRun) vds.push(line("Running", vo2.map((r) => r.vo2max_run), css("--series-1"), { stepped: true, tension: 0 }));
    if (hasRide) vds.push(line("Cycling", vo2.map((r) => r.vo2max_ride), css("--series-2"), { stepped: true, tension: 0 }));
    chart("c-vo2", { type: "line", data: { labels: vo2.map((r) => fmtDate(r.date)), datasets: vds },
      options: baseOptions({ plugins: { legend: { display: vds.length > 1 } } }) });

    // Intensity mix
    const secs = { easy: 0, moderate: 0, hard: 0 };
    d.activities.forEach((a) => { if (a.intensity && !["strength", "yoga", "walk"].includes(a.sport)) secs[a.intensity] += a.moving_s || a.duration_s || 0; });
    const total = secs.easy + secs.moderate + secs.hard;
    const box = $("#intensity"); box.replaceChildren();
    if (!total) box.append(el("p", { class: "muted", text: "No heart-rate data in this range." }));
    else [["easy", "Easy (<77%)"], ["moderate", "Moderate (77–87%)"], ["hard", "Hard (>87%)"]].forEach(([k, label]) => {
      const pct = Math.round((secs[k] / total) * 100);
      const fill = el("div", { class: "fill" }); fill.style.width = pct + "%";
      box.append(el("div", { class: "bar-row" }, el("span", { text: label }), el("div", { class: "track" }, fill), el("span", { class: "pct", text: pct + "%" })));
    });
    if (total) box.append(el("p", { class: "muted small-print", text: "Target for endurance athletes: roughly 80% easy, 20% hard, little in the middle." }));

    // Activities table
    const tb = $("#activities tbody"); tb.replaceChildren();
    d.activities.forEach((a) => {
      const src = [a.garmin_id && "Garmin", a.strava_id && "Strava"].filter(Boolean).join(" + ");
      tb.append(el("tr", {},
        el("td", { text: fmtDate(a.date) }), el("td", {}, a.name || "", a.event ? el("span", { class: "tag", text: a.event }) : null), el("td", { text: a.sport }),
        el("td", { class: "num", text: fmtDur(a.moving_s || a.duration_s) }),
        el("td", { class: "num", text: a.distance_m ? (a.distance_m / 1000).toFixed(1) + " km" : "–" }),
        el("td", { class: "num", text: a.avg_hr ? Math.round(a.avg_hr) : "–" }),
        el("td", { class: "num", text: Math.round(a.load) }),
        el("td", { text: a.intensity || "–" }), el("td", { text: src })));
    });
  }

  function renderEfficiency() {
    const pts = state.data.efficiency.filter((p) => p.sport === state.efSport);
    const vals = pts.map((p) => p.ef);
    $("#ef-sub").textContent = state.efSport === "run"
      ? "Easy & steady runs: metres per minute ÷ heart rate. Up = fitter."
      : "Easy & steady rides with power: watts ÷ heart rate. Up = fitter.";
    chart("c-ef", {
      type: "line",
      data: {
        labels: pts.map((p) => fmtDate(p.date)),
        datasets: [
          line("Session", vals, css("--series-1"), { showLine: false, pointRadius: 4, pointHoverRadius: 6, pointHitRadius: 12, endLabel: false }),
          line("Trend (8-session avg)", rolling(vals, 8), css("--text-secondary"), { borderWidth: 2, short: "Trend", pointHitRadius: 0 }),
        ],
      },
      options: baseOptions({
        layout: { padding: { right: 64 } },
        plugins: { legend: { display: true }, endLabels: { enabled: false } },
        scales: { y: { ticks: { callback: (v) => (+v).toFixed(2) } } },
      }),
    });
  }

  // ---------- recovery ----------
  function renderRecovery() {
    const D = state.data.days, labels = D.map((r) => fmtDate(r.date)), S = state.data.settings;
    const hrv = D.map((r) => r.hrv);
    const lo = D.map((r) => r.hrv_baseline_low), hi = D.map((r) => r.hrv_baseline_high);
    const hasBand = lo.some((v) => v != null);
    chart("c-hrv", {
      type: "line",
      data: {
        labels,
        datasets: [
          ...(hasBand ? [
            line("Baseline low", lo, "transparent", { pointRadius: 0, endLabel: false, borderWidth: 0, pointHitRadius: 0 }),
            line("Baseline high", hi, "transparent", { pointRadius: 0, endLabel: false, borderWidth: 0, fill: "-1", backgroundColor: css("--band"), pointHitRadius: 0 }),
          ] : []),
          line("Nightly", hrv, css("--series-1"), { borderWidth: 1, pointRadius: 2, endLabel: false }),
          line("7-day avg", rolling(hrv, 7), css("--series-2"), { short: "7d" }),
        ],
      },
      options: baseOptions({
        layout: { padding: { right: 48 } },
        plugins: { legend: { display: true, labels: { filter: (i) => !i.text.startsWith("Baseline") } }, endLabels: { enabled: true } },
      }),
    });

    const rhr = D.map((r) => r.resting_hr);
    chart("c-rhr", {
      type: "line",
      data: { labels, datasets: [
        line("Daily", rhr, css("--series-1"), { borderWidth: 1, pointRadius: 2, endLabel: false }),
        line("7-day avg", rolling(rhr, 7), css("--series-2"), { short: "7d" }),
      ] },
      options: baseOptions({ layout: { padding: { right: 48 } }, plugins: { legend: { display: true }, endLabels: { enabled: true } } }),
    });

    const sleepH = D.map((r) => (r.sleep_s ? +(r.sleep_s / 3600).toFixed(2) : null));
    $("#sleep-sub").textContent = `Hours per night · target ${S.sleep_target_h} h`;
    chart("c-sleep", {
      type: "bar",
      data: { labels, datasets: [
        bars("Watch", sleepH.map((v, i) => (D[i].sleep_source === "manual log" ? null : v)), css("--series-1"), { stack: "s" }),
        bars("Logged bed/wake", sleepH.map((v, i) => (D[i].sleep_source === "manual log" ? v : null)), css("--series-2"), { stack: "s" }),
      ] },
      options: baseOptions({
        scales: { x: { stacked: true }, y: { stacked: true, suggestedMin: 4, suggestedMax: 10 } },
        interaction: { mode: "nearest", intersect: true },
        plugins: { refs: { lines: [{ y: S.sleep_target_h, label: `target ${S.sleep_target_h} h` }] },
          legend: { display: true, labels: { boxWidth: 10, boxHeight: 10 } },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtDur(c.parsed.y * 3600)}` } } },
      }),
    });

    // Bed→wake floating bars. y = minutes since midnight of the evening before (bedtime 23:30 = 1410).
    const ranges = D.map((r) => (r.bed_min != null && r.wake_min != null ? [r.bed_min, r.wake_min + 1440] : null));
    const all = ranges.filter(Boolean).flat();
    chart("c-schedule", {
      type: "bar",
      data: { labels, datasets: [bars("Asleep", ranges, css("--series-1"), { borderRadius: 4, borderSkipped: false })] },
      options: baseOptions({
        scales: { y: {
          reverse: true, min: all.length ? Math.floor((Math.min(...all) - 30) / 60) * 60 : 1260,
          max: all.length ? Math.ceil((Math.max(...all) + 30) / 60) * 60 : 1920,
          ticks: { stepSize: 120, maxTicksLimit: 12, callback: (v) => fmtClock(v) },
        } },
        plugins: { tooltip: { callbacks: { label: (c) => { const v = c.raw; return v ? `Bed ${fmtClock(v[0])} → wake ${fmtClock(v[1])} (${fmtDur((v[1] - v[0]) * 60)})` : ""; } } } },
      }),
    });

    chart("c-sleepscore", { type: "bar", data: { labels, datasets: [bars("Sleep score", D.map((r) => r.sleep_score), css("--series-1"))] },
      options: baseOptions({ scales: { y: { min: 0, max: 100 } } }) });

    chart("c-bb", {
      type: "line",
      data: { labels, datasets: [
        line("Body Battery peak", D.map((r) => r.body_battery_high), css("--series-1"), { short: "Body Battery" }),
        line("Avg stress", D.map((r) => r.stress_avg), css("--series-2"), { short: "Stress" }),
      ] },
      options: baseOptions({ layout: { padding: { right: 104 } }, scales: { y: { min: 0, max: 100 } },
        plugins: { legend: { display: true }, endLabels: { enabled: true } } }),
    });
  }

  // ---------- habits ----------
  function renderHabits() {
    const D = state.data.days, labels = D.map((r) => fmtDate(r.date)), S = state.data.settings;
    const drinks = D.map((r) => r.drinks);
    const wk = drinks.slice(-7).reduce((a, b) => a + (b || 0), 0);
    $("#drinks-sub").textContent = `Drinks per day · last 7 days: ${wk} (limit ${S.weekly_drink_limit})`;
    chart("c-drinks", { type: "bar", data: { labels, datasets: [bars("Drinks", drinks, css("--series-1"))] },
      options: baseOptions({ scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }) });

    chart("c-kcal", {
      type: "line",
      data: { labels, datasets: [
        line("Eaten", D.map((r) => r.calories), css("--series-1"), { short: "Eaten" }),
        line("Burned", D.map((r) => r.kcal_burned), css("--series-2"), { short: "Burned" }),
      ] },
      options: baseOptions({ layout: { padding: { right: 86 } }, plugins: { legend: { display: true }, endLabels: { enabled: true } } }),
    });

    const target = Math.round(S.protein_target_g_per_kg * S.weight_kg);
    $("#protein-sub").textContent = `Grams per day · target ${target} g (${S.protein_target_g_per_kg} g/kg)`;
    chart("c-protein", { type: "bar", data: { labels, datasets: [bars("Protein (g)", D.map((r) => r.protein_g), css("--series-1"))] },
      options: baseOptions({ scales: { y: { beginAtZero: true } }, plugins: { refs: { lines: [{ y: target, label: `target ${target} g` }] } } }) });

    chart("c-water", { type: "bar", data: { labels, datasets: [bars("Water (L)", D.map((r) => r.water_l), css("--series-1"))] },
      options: baseOptions({ scales: { y: { beginAtZero: true } } }) });

    const tb = $("#correlations tbody"); tb.replaceChildren();
    const names = { hrv: "HRV", resting_hr: "Resting HR", sleep_score: "Sleep score", body_battery_high: "Body Battery" };
    if (!state.data.correlations.length) tb.append(el("tr", {}, el("td", { colspan: "6", class: "muted", text: "Not enough logged data yet — keep logging for a couple of weeks." })));
    state.data.correlations.forEach((c) => {
      tb.append(el("tr", {},
        el("td", { text: c.habit }), el("td", { text: names[c.metric] || c.metric }),
        el("td", { class: "num", text: `${c.with} ${c.unit}` }), el("td", { class: "num", text: `${c.without} ${c.unit}` }),
        el("td", { class: "num " + (Math.abs(c.diff) < 1 ? "" : c.worse ? "worse" : "better"), text: `${c.diff > 0 ? "+" : ""}${c.diff} ${c.unit}` }),
        el("td", { class: "num", text: `${c.n_with} / ${c.n_without}` })));
    });
  }

  const HABIT_FIELDS = ["bedtime", "wake_time", "sleep_quality", "drinks", "calories", "protein_g", "carbs_g", "fat_g", "water_l", "caffeine_mg", "energy", "soreness", "notes"];
  async function loadHabit(day) {
    if (!day) return;
    const f = $("#habit-form");
    try {
      const r = await api(`/api/habits/${day}`);
      HABIT_FIELDS.forEach((k) => { f.elements[k].value = r.manual?.[k] ?? ""; f.elements[k].placeholder = ""; });
      const g = r.garmin || {};
      const ph = { bedtime: g.bedtime, wake_time: g.wake_time, calories: g.kcal_in, protein_g: g.protein_g, carbs_g: g.carbs_g, fat_g: g.fat_g, drinks: g.drinks, water_l: g.water_ml ? (g.water_ml / 1000).toFixed(1) : null };
      Object.entries(ph).forEach(([k, v]) => { if (v != null && f.elements[k].type !== "time") f.elements[k].placeholder = `Garmin: ${v}`; });
      $("#habit-hint").textContent = g.bedtime
        ? `Watch recorded bed ${g.bedtime} → wake ${g.wake_time}. Leave blank to use it.`
        : "No watch sleep for this night. Log bed and wake time and sleep is worked out from them.";
    } catch (e) { toast(e.message); }
  }


  // ---------- coach chat ----------
  // Minimal, safe markdown: paragraphs, bullet/numbered lists, ### headings, **bold**, *italic*, `code`.
  function inline(text, parent) {
    const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) parent.append(text.slice(last, m.index));
      const t = m[0];
      if (t.startsWith("**")) parent.append(el("strong", { text: t.slice(2, -2) }));
      else if (t.startsWith("`")) parent.append(el("code", { text: t.slice(1, -1) }));
      else parent.append(el("em", { text: t.slice(1, -1) }));
      last = m.index + t.length;
    }
    if (last < text.length) parent.append(text.slice(last));
    return parent;
  }
  function renderMarkdown(md) {
    const frag = document.createDocumentFragment();
    let list = null;
    md.split("\n").forEach((raw) => {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*[-*•]\s+(.*)/), num = line.match(/^\s*\d+[.)]\s+(.*)/), head = line.match(/^#{1,4}\s+(.*)/);
      if (bullet || num) {
        const tag = bullet ? "ul" : "ol";
        if (!list || list.tagName.toLowerCase() !== tag) { list = el(tag); frag.append(list); }
        list.append(inline((bullet || num)[1], el("li")));
        return;
      }
      list = null;
      if (!line.trim()) return;
      if (head) frag.append(inline(head[1], el("h4")));
      else frag.append(inline(line, el("p")));
    });
    return frag;
  }

  function chatMessage(m) {
    if (m.role === "user") return el("div", { class: "msg user", text: m.text });
    const div = el("div", { class: "msg assistant" });
    div.append(renderMarkdown(m.text || ""));
    (m.logged || []).forEach((l) => {
      const parts = Object.entries(l.fields || {}).map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`);
      div.append(el("div", { class: "logged", text: `✓ Logged for ${l.date} — ${parts.join(", ")}` }));
    });
    if (m.tools?.length) div.append(el("div", { class: "meta", text: "Looked at: " + m.tools.join(", ") }));
    return div;
  }

  function showChat(messages) {
    const log = $("#chat-log");
    log.replaceChildren();
    if (!messages.length) { log.append(chatEmpty); return; }
    messages.filter((m) => !m.interim).forEach((m) => log.append(chatMessage(m)));
    log.scrollTop = log.scrollHeight;
  }

  async function loadChatList() {
    try {
      const r = await api("/api/chat");
      const ul = $("#chat-list"); ul.replaceChildren();
      r.conversations.forEach((c) => {
        const b = el("button", { type: "button", title: c.title || "", text: c.title || "Conversation" });
        b.classList.toggle("on", c.id === state.conv);
        b.addEventListener("click", () => openConversation(c.id));
        ul.append(el("li", {}, b));
      });
    } catch (e) { /* non-fatal */ }
  }

  async function openConversation(id) {
    state.conv = id;
    try {
      const r = await api(`/api/chat/${id}`);
      showChat(r.messages);
    } catch (e) { toast(e.message); }
    loadChatList();
  }

  async function sendChat(text) {
    text = text.trim();
    if (!text || state.chatBusy) return;
    state.chatBusy = true;
    const log = $("#chat-log");
    if (log.contains(chatEmpty)) log.replaceChildren();
    log.append(chatMessage({ role: "user", text }));
    const wait = el("div", { class: "msg assistant thinking", text: "Looking at your data…" });
    log.append(wait); log.scrollTop = log.scrollHeight;
    $("#chat-input").value = ""; $("#chat-send").disabled = true;
    try {
      const r = await api("/api/chat", { method: "POST", body: { message: text, conversation_id: state.conv } });
      state.conv = r.conversation_id;
      wait.replaceWith(chatMessage({ role: "assistant", text: r.reply, tools: r.tools, logged: r.logged }));
      if (r.logged?.length) load();          // habits changed: refresh charts and diagnostics
      loadChatList();
    } catch (e) {
      wait.replaceWith(el("div", { class: "msg assistant error", text: e.message }));
    } finally {
      state.chatBusy = false; $("#chat-send").disabled = false;
      log.scrollTop = log.scrollHeight; $("#chat-input").focus();
    }
  }
  let chatEmpty;

  // ---------- settings ----------
  async function loadCoachStatus() {
    try { const s = await api("/api/status"); $("#coach-setup").hidden = s.coach.configured; } catch {}
  }

  async function loadStatus() {
    try {
      const s = await api("/api/status");
      $("#garmin-status").textContent = s.garmin.connected ? "✓ Connected" : (s.garmin.env_credentials ? "Credentials in .env — will log in on first sync" : "Not connected");
      $("#garmin-form").hidden = s.garmin.connected;
      $("#garmin-disconnect").hidden = !s.garmin.connected;
      $("#strava-status").textContent = !s.strava.configured ? "Add STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET to .env to enable (see README)."
        : s.strava.connected ? `✓ Connected${s.strava.athlete ? " as " + s.strava.athlete : ""}` : "Not connected";
      $("#strava-connect").hidden = !s.strava.configured || s.strava.connected;
      $("#strava-disconnect").hidden = !s.strava.connected;
      $("#counts").textContent = `${s.counts.activities} activities · ${s.counts.wellness_days} days of wellness data · ${s.counts.habit_days} habit entries` +
        (s.auto_sync_hours ? ` · auto-sync every ${s.auto_sync_hours} h` : "");
      const log = $("#sync-log"); log.replaceChildren();
      s.log.forEach((l) => log.append(el("li", { class: l.ok ? "" : "fail", text: `${l.finished_at.replace("T", " ")} — ${l.message}` })));
      const st = await api("/api/settings");
      const f = $("#settings-form");
      Object.entries(st).forEach(([k, v]) => { if (f.elements[k]) f.elements[k].value = v; });
    } catch (e) { toast(e.message); }
  }

  async function doSync(provider = "all", days) {
    const btn = $("#sync-btn"); btn.disabled = true; btn.textContent = "Syncing…";
    try {
      const r = await api(`/api/sync?provider=${provider}${days ? "&days=" + days : ""}`, { method: "POST" });
      toast(r.results.map((x) => (x.ok ? "✓ " : "✗ ") + x.message).join("  ·  "));
      await load();
      if (location.hash.startsWith("#settings")) loadStatus();
    } catch (e) { toast("Sync failed: " + e.message); }
    finally { btn.disabled = false; btn.textContent = "Sync now"; }
  }

  // ---------- events ----------
  function formValues(form, fields, numeric = true) {
    const out = {};
    fields.forEach((k) => {
      const v = form.elements[k].value.trim();
      out[k] = v === "" ? null : (numeric && form.elements[k].type === "number" ? Number(v) : v);
    });
    return out;
  }

  function bind() {
    window.addEventListener("hashchange", showTab);
    $("#range").addEventListener("change", (e) => { state.range = +e.target.value; load(); });
    $("#sync-btn").addEventListener("click", () => doSync());
    $$("[data-sync]").forEach((b) => b.addEventListener("click", () => doSync(b.dataset.sync, b.dataset.days)));
    $("#theme-btn").addEventListener("click", () => {
      const dark = document.documentElement.dataset.theme
        ? document.documentElement.dataset.theme === "dark"
        : matchMedia("(prefers-color-scheme: dark)").matches;
      document.documentElement.dataset.theme = dark ? "light" : "dark";
      themeLabel();
      try { localStorage.setItem("theme", document.documentElement.dataset.theme); } catch {}
      if (state.data) render();
    });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => state.data && render());
    $$("#ef-sport button").forEach((b) => b.addEventListener("click", () => {
      state.efSport = b.dataset.v;
      $$("#ef-sport button").forEach((x) => x.classList.toggle("on", x === b));
      renderEfficiency();
    }));

    chatEmpty = $("#chat-empty");
    $("#chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendChat($("#chat-input").value); });
    $("#chat-input").addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(e.target.value); }
    });
    $$("#chat-chips button").forEach((b) => b.addEventListener("click", () => sendChat(b.textContent)));
    $("#chat-new").addEventListener("click", () => { state.conv = null; showChat([]); loadChatList(); $("#chat-input").focus(); });

    const hf = $("#habit-form");
    hf.elements.date.addEventListener("change", (e) => loadHabit(e.target.value));
    hf.addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api(`/api/habits/${hf.elements.date.value}`, { method: "PUT", body: formValues(hf, HABIT_FIELDS) });
        toast("Saved");
        load();
      } catch (err) { toast("Could not save: " + err.message); }
    });
    $("#habit-clear").addEventListener("click", async () => {
      if (!confirm("Delete the manual entry for this date?")) return;
      await api(`/api/habits/${hf.elements.date.value}`, { method: "DELETE" });
      loadHabit(hf.elements.date.value); load();
    });

    $("#settings-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target;
      try {
        const body = formValues(f, ["max_hr", "resting_hr", "sex", "weight_kg", "sleep_target_h", "protein_target_g_per_kg", "weekly_drink_limit"]);
        await api("/api/settings", { method: "PUT", body });
        toast("Profile saved"); load();
      } catch (err) { toast("Could not save: " + err.message); }
    });

    $("#garmin-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = e.target, btn = f.querySelector("button"); btn.disabled = true;
      try {
        const r = await api("/api/garmin/login", { method: "POST", body: { email: f.email.value, password: f.password.value } });
        f.password.value = "";
        if (r.status === "needs_mfa") { $("#garmin-mfa").hidden = false; toast("Enter the code Garmin sent you."); }
        else { toast("Garmin connected — syncing…"); loadStatus(); doSync("garmin"); }
      } catch (err) { toast(err.message); } finally { btn.disabled = false; }
    });
    $("#garmin-mfa").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        await api("/api/garmin/mfa", { method: "POST", body: { code: e.target.code.value.trim() } });
        $("#garmin-mfa").hidden = true; toast("Garmin connected — syncing…"); loadStatus(); doSync("garmin");
      } catch (err) { toast(err.message); }
    });
    $("#garmin-disconnect").addEventListener("click", async () => { await api("/api/garmin/disconnect", { method: "POST" }); loadStatus(); });
    $("#strava-disconnect").addEventListener("click", async () => { await api("/api/strava/disconnect", { method: "POST" }); loadStatus(); });
  }

  function themeLabel() {
    const t = document.documentElement.dataset.theme;
    const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    $("#theme-btn").textContent = dark ? "Light mode" : "Dark mode";
  }

  // ---------- boot ----------
  try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch {}
  const today = new Date(); today.setMinutes(today.getMinutes() - today.getTimezoneOffset());
  $("#habit-form [name=date]").value = today.toISOString().slice(0, 10);
  bind();
  themeLabel();
  showTab();
  load();
})();
