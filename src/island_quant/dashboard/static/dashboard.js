(() => {
  "use strict";
  const root = document.documentElement;
  const savedTheme = localStorage.getItem("iq-theme");
  if (savedTheme) root.dataset.theme = savedTheme;
  document.querySelector("[data-theme-toggle]")?.addEventListener("click", () => {
    root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
    localStorage.setItem("iq-theme", root.dataset.theme);
    drawAllCharts();
  });
  document.querySelector("[data-menu]")?.addEventListener("click", () => {
    document.querySelector("#sidebar")?.classList.toggle("open");
  });

  const css = (name) => getComputedStyle(root).getPropertyValue(name).trim();
  function setupCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, rect.width * ratio);
    canvas.height = Math.max(1, rect.height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    return { context, width: rect.width, height: rect.height };
  }
  function drawChart(container, seriesList, kind) {
    const canvas = container.querySelector("canvas");
    if (!canvas || !seriesList.length) return;
    const { context: ctx, width, height } = setupCanvas(canvas);
    const pad = { left: 38, right: 12, top: 14, bottom: 27 };
    const all = seriesList.flatMap((series) => series.points.map((point) => point.value));
    let min = Math.min(...all, 0), max = Math.max(...all, 0);
    if (min === max) { min -= 1; max += 1; }
    const plotWidth = width - pad.left - pad.right, plotHeight = height - pad.top - pad.bottom;
    ctx.clearRect(0, 0, width, height);
    ctx.strokeStyle = css("--border"); ctx.fillStyle = css("--muted"); ctx.lineWidth = 1;
    ctx.font = "10px ui-monospace";
    for (let i = 0; i <= 4; i++) {
      const y = pad.top + plotHeight * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      const value = max - (max - min) * i / 4;
      ctx.fillText(Math.abs(value) >= 10 ? value.toFixed(0) : value.toFixed(2), 2, y + 3);
    }
    const xFor = (index, count) => pad.left + (count <= 1 ? plotWidth / 2 : index * plotWidth / (count - 1));
    const yFor = (value) => pad.top + (max - value) * plotHeight / (max - min);
    seriesList.forEach((series, seriesIndex) => {
      ctx.strokeStyle = series.color; ctx.fillStyle = series.color; ctx.lineWidth = 2;
      if (kind === "bar" && seriesIndex === 0) {
        const barWidth = Math.max(3, plotWidth / series.points.length * .58);
        series.points.forEach((point, index) => {
          const x = xFor(index, series.points.length) - barWidth / 2;
          const zero = yFor(0), y = yFor(point.value);
          ctx.globalAlpha = .78; ctx.fillRect(x, Math.min(zero, y), barWidth, Math.max(2, Math.abs(zero-y))); ctx.globalAlpha = 1;
        });
      } else {
        ctx.beginPath();
        series.points.forEach((point, index) => { const x=xFor(index,series.points.length), y=yFor(point.value); index ? ctx.lineTo(x,y) : ctx.moveTo(x,y); });
        ctx.stroke();
      }
    });
    const labels = seriesList[0].points;
    const step = Math.max(1, Math.ceil(labels.length / 6));
    ctx.fillStyle = css("--muted");
    labels.forEach((point, index) => { if(index % step === 0 || index === labels.length-1) ctx.fillText(point.label, xFor(index,labels.length)-6, height-7); });
  }
  function drawAllCharts() {
    document.querySelectorAll("[data-chart]").forEach((container) => {
      const payload = JSON.parse(container.querySelector("script").textContent);
      drawChart(container, [payload], container.dataset.chart);
    });
    document.querySelectorAll("[data-multi-chart]").forEach((container) => {
      const payload = [...container.querySelectorAll("script")].map((script) => JSON.parse(script.textContent));
      drawChart(container, payload, container.dataset.multiChart);
    });
  }
  let redrawTimer;
  window.addEventListener("resize", () => { clearTimeout(redrawTimer); redrawTimer = setTimeout(drawAllCharts, 120); });
  drawAllCharts();

  document.querySelector("[data-factor-filter]")?.addEventListener("change", (event) => {
    document.querySelectorAll("[data-factor-row]").forEach((row) => { row.hidden = event.target.value && row.dataset.category !== event.target.value; });
  });

  document.querySelectorAll("table.sortable th").forEach((header, column) => {
    header.addEventListener("click", () => {
      const body = header.closest("table").tBodies[0];
      const rows = [...body.rows];
      const direction = header.dataset.direction === "asc" ? -1 : 1;
      rows.sort((a,b) => a.cells[column].innerText.localeCompare(b.cells[column].innerText, undefined, {numeric:true}) * direction);
      rows.forEach((row) => body.appendChild(row));
      header.dataset.direction = direction === 1 ? "asc" : "desc";
    });
  });

  const issueDialog = document.querySelector("[data-issue-dialog]");
  function bindIssueButtons() {
    document.querySelectorAll("[data-issue-open]").forEach((button) => button.addEventListener("click", () => {
      const issue = JSON.parse(button.closest("tr").dataset.issue);
      issueDialog.querySelector("[data-dialog-content]").innerHTML = `<h2>Quality issue detail</h2><dl>${Object.entries(issue).map(([key,value]) => `<div><dt>${key.replaceAll("_"," ")}</dt><dd>${String(value)}</dd></div>`).join("")}</dl><p class="muted">Read-only synthetic fixture record. No mutation actions are available.</p>`;
      issueDialog.showModal();
    }));
  }
  bindIssueButtons();
  document.querySelector("[data-dialog-close]")?.addEventListener("click", () => issueDialog.close());

  const issueForm = document.querySelector("[data-issue-filters]");
  issueForm?.addEventListener("change", async () => {
    const params = new URLSearchParams(new FormData(issueForm));
    [...params.entries()].forEach(([key,value]) => { if (!value) params.delete(key); });
    const response = await fetch(`/api/dashboard/data-issues?${params}`);
    if (!response.ok) return;
    const page = await response.json();
    const body = document.querySelector("[data-issue-rows]");
    body.innerHTML = page.items.map((issue) => `<tr data-issue='${JSON.stringify(issue).replaceAll("'","&#39;")}'><td><button class="link-button" data-issue-open>${issue.instrument_id}</button></td><td>${issue.event_date}</td><td>${issue.category.replaceAll("_"," ")}</td><td><span class="severity severity-${issue.severity}">${issue.severity}</span></td><td>${issue.reason}</td><td>${issue.source}</td><td>${issue.resolution_status}</td></tr>`).join("");
    document.querySelector("[data-issue-status]").textContent = `Showing ${page.items.length} of ${page.total} synthetic issues`;
    bindIssueButtons();
  });
})();
