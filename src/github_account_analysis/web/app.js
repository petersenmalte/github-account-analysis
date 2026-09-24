const form = document.querySelector("#analysis-form");
const result = document.querySelector("#result");
const template = document.querySelector("#report-template");

// Fixed categorical color order (validated colorblind-safe adjacent
// contrast) — same palette as the PDF's matplotlib charts, so the two
// surfaces agree. Never cycled: a category beyond this count folds into
// muted "Other" instead of repeating a hue.
const CHART_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"];
const CHART_MUTED = "#898781";
const CHART_INK = "#172033";
const CHART_GRID = "#e1e0d9";
const SVG_NS = "http://www.w3.org/2000/svg";

function text(element, value) { element.textContent = value; }
function showError(message) {
  result.hidden = false;
  result.replaceChildren(Object.assign(document.createElement("p"), {className: "error", textContent: message}));
}
function metricRows(metrics) {
  return [
    ["Attributable commits", metrics.attributable_commits],
    ["Opened pull requests", metrics.opened_pull_requests],
    ["Merged pull requests", metrics.merged_pull_requests],
    ["Owned repositories", metrics.owned_repositories],
    ["Contributed repositories", metrics.contributed_repositories],
    ["Source change volume", `+${metrics.code_change_volume.added_lines} / -${metrics.code_change_volume.removed_lines} lines`],
  ];
}

function svgEl(tag, attrs) {
  const element = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) element.setAttribute(key, value);
  return element;
}

function emptyChartNotice(container, message) {
  container.replaceChildren(Object.assign(document.createElement("p"), {className: "muted", textContent: message}));
}

/** Single-series vertical bar chart: no legend needed, the axis label names it. */
function renderMonthlyChart(container, monthlyActivity) {
  const months = Object.keys(monthlyActivity).sort();
  if (!months.length) { emptyChartNotice(container, "No dated activity is available for this timeframe."); return; }
  const values = months.map((month) => monthlyActivity[month]);
  const maxValue = Math.max(...values, 1);
  const width = 640, height = 220, marginLeft = 34, marginBottom = 44, marginTop = 14, marginRight = 10;
  const plotWidth = width - marginLeft - marginRight, plotHeight = height - marginTop - marginBottom;
  const barGap = 10;
  const barWidth = Math.min(64, (plotWidth - barGap * (months.length - 1)) / months.length);
  const usedWidth = barWidth * months.length + barGap * (months.length - 1);
  const startX = marginLeft + (plotWidth - usedWidth) / 2;
  const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Attributable commits and pull requests per month"});
  const gridGroup = svgEl("g");
  for (let step = 0; step <= 4; step++) {
    const y = marginTop + plotHeight - (plotHeight * step) / 4;
    gridGroup.append(svgEl("line", {x1: marginLeft, x2: width - marginRight, y1: y, y2: y, stroke: CHART_GRID, "stroke-width": 1}));
    const label = svgEl("text", {x: marginLeft - 6, y: y + 3, "text-anchor": "end", "font-size": 9, fill: CHART_MUTED});
    label.textContent = Math.round((maxValue * step) / 4);
    gridGroup.append(label);
  }
  svg.append(gridGroup);
  months.forEach((month, index) => {
    const value = values[index];
    const barHeight = maxValue ? (value / maxValue) * plotHeight : 0;
    const x = startX + index * (barWidth + barGap);
    const y = marginTop + plotHeight - barHeight;
    const bar = svgEl("rect", {x, y, width: barWidth, height: barHeight, rx: 2, fill: CHART_COLORS[0]});
    const barTitle = svgEl("title");
    barTitle.textContent = `${month}: ${value}`;
    bar.append(barTitle);
    svg.append(bar);
    const valueLabel = svgEl("text", {x: x + barWidth / 2, y: y - 5, "text-anchor": "middle", "font-size": 9, fill: CHART_INK});
    valueLabel.textContent = value;
    svg.append(valueLabel);
    const monthLabel = svgEl("text", {
      x: x + barWidth / 2, y: height - marginBottom + 14, "text-anchor": "end", "font-size": 8.5, fill: CHART_MUTED,
      transform: `rotate(-40 ${x + barWidth / 2} ${height - marginBottom + 14})`,
    });
    monthLabel.textContent = month;
    svg.append(monthLabel);
  });
  container.replaceChildren(svg);
}

/** Horizontal bar chart with a fixed categorical color order; anything past
 * the palette folds into a muted "Other" bar rather than cycling a hue. */
function renderLanguageChart(container, distribution) {
  if (!distribution.length) { emptyChartNotice(container, "No measured repository language data is available."); return; }
  const top = distribution.slice(0, CHART_COLORS.length);
  const restPercent = distribution.slice(CHART_COLORS.length).reduce((sum, row) => sum + row.percent, 0);
  const rows = top.map((row, index) => ({label: row.language, value: row.percent, color: CHART_COLORS[index]}));
  if (restPercent > 0) rows.push({label: "Other", value: Math.round(restPercent * 10) / 10, color: CHART_MUTED});
  const maxValue = Math.max(...rows.map((row) => row.value), 1);
  const rowHeight = 26, barHeight = 14, labelWidth = 110, marginLeft = 8, marginRight = 46, width = 640;
  const plotWidth = width - labelWidth - marginLeft - marginRight;
  const height = rows.length * rowHeight + 8;
  const svg = svgEl("svg", {viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Byte-weighted programming language share across owned repositories"});
  rows.forEach((row, index) => {
    const y = index * rowHeight + 6;
    const barWidth = (row.value / maxValue) * plotWidth;
    const label = svgEl("text", {x: labelWidth - 8, y: y + barHeight / 2 + 4, "text-anchor": "end", "font-size": 10, fill: CHART_INK});
    label.textContent = row.label;
    svg.append(label);
    const bar = svgEl("rect", {x: labelWidth, y, width: Math.max(barWidth, 1), height: barHeight, rx: 2, fill: row.color});
    const barTitle = svgEl("title");
    barTitle.textContent = `${row.label}: ${row.value}%`;
    bar.append(barTitle);
    svg.append(bar);
    const valueLabel = svgEl("text", {x: labelWidth + barWidth + 6, y: y + barHeight / 2 + 4, "font-size": 9.5, fill: CHART_INK});
    valueLabel.textContent = `${row.value}%`;
    svg.append(valueLabel);
  });
  container.replaceChildren(svg);
}

function renderHeuristicSignals(container, signals) {
  const caveat = document.createElement("p");
  caveat.className = "caveat";
  text(caveat, signals.caveat);
  const summary = document.createElement("p");
  text(summary, `Denominator: ${signals.denominator} non-bot artifacts. Artifacts with any tool mention: ${signals.artifacts_with_any_mention} (${signals.percent_with_any_mention ?? "n/a"}%).`);
  const children = [caveat, summary];
  const tools = Object.entries(signals.by_tool);
  if (tools.length) {
    const list = document.createElement("ul");
    for (const [tool, count] of tools) {
      const item = document.createElement("li");
      text(item, `${tool}: ${count}`);
      list.append(item);
    }
    children.push(list);
  } else {
    const none = document.createElement("p");
    none.className = "muted";
    text(none, "No known AI-tool name or footer marker was found.");
    children.push(none);
  }
  container.replaceChildren(...children);
}

function render(report, requestPayload) {
  const fragment = template.content.cloneNode(true);
  text(fragment.querySelector(".subject"), `@${report.subject.login}`);
  const partial = fragment.querySelector(".partial");
  text(partial, report.partial ? `Partial analysis: ${report.partial_reasons.join("; ")}` : "No collector partiality was reported.");
  partial.classList.toggle("warning", report.partial);
  const metrics = fragment.querySelector(".metrics");
  for (const [label, value] of metricRows(report.metrics)) {
    const term = document.createElement("dt"), description = document.createElement("dd");
    text(term, label); text(description, value); metrics.append(term, description);
  }
  renderMonthlyChart(fragment.querySelector(".chart-monthly"), report.metrics.monthly_activity);
  renderLanguageChart(fragment.querySelector(".chart-languages"), report.metrics.languages_in_owned_repositories.byte_weighted_distribution);
  const ai = report.ai_metadata;
  text(fragment.querySelector(".ai"), `Denominator: ${ai.denominator} non-bot attributable artifacts. A explicitly AI-assisted: ${ai.classes.A.count} (${ai.classes.A.percent ?? "n/a"}%); B explicitly entirely human declared: ${ai.classes.B.count} (${ai.classes.B.percent ?? "n/a"}%); C undetermined: ${ai.classes.C.count} (${ai.classes.C.percent ?? "n/a"}%). Bots: ${ai.bots}.`);
  renderHeuristicSignals(fragment.querySelector(".heuristic"), report.heuristic_ai_signals);
  text(fragment.querySelector(".quality"), `${report.quality.repository_state.status}: ${report.quality.repository_state.reason}`);
  const list = fragment.querySelector(".uncertainty");
  for (const item of report.uncertainty) { const row = document.createElement("li"); text(row, item); list.append(row); }
  text(fragment.querySelector("pre"), JSON.stringify(report, null, 2));
  fragment.querySelector(".pdf").addEventListener("click", async () => {
    try {
      const response = await fetch("/api/report.pdf", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(requestPayload)});
      if (!response.ok) { throw new Error("PDF generation failed."); }
      const link = Object.assign(document.createElement("a"), {href: URL.createObjectURL(await response.blob()), download: `github-account-analysis-${report.subject.login}.pdf`});
      link.click(); URL.revokeObjectURL(link.href);
    } catch (error) {
      showError(error instanceof Error ? error.message : "PDF generation failed.");
    }
  });
  result.replaceChildren(fragment); result.hidden = false;
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (window.location.protocol === "file:") {
    showError("This page was opened directly from a file. Start the Python server, then open http://127.0.0.1:8000 instead of opening index.html.");
    return;
  }
  const button = form.querySelector("button");
  const scope = [...form.querySelectorAll('input[name="scope"]:checked')].map(input => input.value);
  const requestPayload = {username: form.username.value, timeframe: form.timeframe.value, scope};
  button.disabled = true; text(button, "Analyzing…");
  try {
    const response = await fetch("/api/analyze", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(requestPayload)});
    let payload;
    try {
      payload = await response.json();
    } catch (parseError) {
      throw new Error(`The server returned an unexpected non-JSON response (HTTP ${response.status}). This can happen when a proxy times out a slow request, for example while GitHub's public API rate limit is being waited out. Please wait a few minutes and try again.`);
    }
    if (!response.ok) throw new Error(payload.error || "Analysis failed.");
    render(payload, requestPayload);
  } catch (error) {
    showError(error instanceof Error ? error.message : "The analysis request failed.");
  } finally {
    button.disabled = false; text(button, "Analyze public profile");
  }
});
