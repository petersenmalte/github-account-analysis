const form = document.querySelector("#analysis-form");
const result = document.querySelector("#result");
const template = document.querySelector("#report-template");

function text(element, value) { element.textContent = value; }
function metricRows(metrics) {
  return [
    ["Attributable commits", metrics.attributable_commits],
    ["Opened pull requests", metrics.opened_pull_requests],
    ["Merged pull requests", metrics.merged_pull_requests],
    ["Owned repositories", metrics.owned_repositories],
    ["Contributed repositories", metrics.contributed_repositories],
    ["Source change volume", `+${metrics.code_change_volume.added_lines} / -${metrics.code_change_volume.removed_lines} lines`],
    ["Languages", Object.entries(metrics.languages_in_owned_repositories.repository_counts).map(([language, count]) => `${language}: ${count} owned repositories`).join(", ") || "Not available"],
    ["Monthly activity", Object.entries(metrics.monthly_activity).map(([month, count]) => `${month}: ${count}`).join(", ") || "Not available"],
  ];
}
function render(report) {
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
  const ai = report.ai_metadata;
  text(fragment.querySelector(".ai"), `Denominator: ${ai.denominator} non-bot attributable artifacts. A explicitly AI-assisted: ${ai.classes.A.count} (${ai.classes.A.percent ?? "n/a"}%); B explicitly entirely human declared: ${ai.classes.B.count} (${ai.classes.B.percent ?? "n/a"}%); C undetermined: ${ai.classes.C.count} (${ai.classes.C.percent ?? "n/a"}%). Bots: ${ai.bots}.`);
  text(fragment.querySelector(".quality"), `${report.quality.repository_state.status}: ${report.quality.repository_state.reason}`);
  const list = fragment.querySelector(".uncertainty");
  for (const item of report.uncertainty) { const row = document.createElement("li"); text(row, item); list.append(row); }
  text(fragment.querySelector("pre"), JSON.stringify(report, null, 2));
  fragment.querySelector(".pdf").addEventListener("click", async () => {
    const response = await fetch("/api/report.pdf", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(report)});
    if (!response.ok) { throw new Error("PDF generation failed."); }
    const link = Object.assign(document.createElement("a"), {href: URL.createObjectURL(await response.blob()), download: `github-account-analysis-${report.subject.login}.pdf`});
    link.click(); URL.revokeObjectURL(link.href);
  });
  result.replaceChildren(fragment); result.hidden = false;
}
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector("button");
  const scope = [...form.querySelectorAll('input[name="scope"]:checked')].map(input => input.value);
  button.disabled = true; text(button, "Analyzing…");
  try {
    const response = await fetch("/api/analyze", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({username: form.username.value, timeframe: form.timeframe.value, scope})});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Analysis failed.");
    render(payload);
  } catch (error) {
    result.hidden = false; result.replaceChildren(Object.assign(document.createElement("p"), {className: "error", textContent: error.message}));
  } finally {
    button.disabled = false; text(button, "Analyze public profile");
  }
});
