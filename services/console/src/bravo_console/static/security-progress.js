import { controlReportFresh, controlReportMatches } from "./model.js";
import { ready } from "./operations.js";

// Presentation of the native T19 report only. Never evaluates evidence or grants execution.
export function securityProgress(model, controls, now = Date.now()) {
  const report = controls?.report;
  if (
    !ready(model, now) ||
    !report ||
    !controlReportFresh(report, now) ||
    !controlReportMatches(report, model.snapshot, model.gate)
  )
    return {
      state: "UNAVAILABLE",
      message: "현재 계획의 유효한 보안 시험 결과를 기다립니다.",
    };
  const count = (key) =>
    Object.fromEntries(
      ["PASS", "FAIL", "UNKNOWN"].map((verdict) => [
        verdict,
        report.rows.filter((row) => row[key] === verdict).length,
      ]),
    );
  return {
    state: report.decision,
    profile: `${report.profile.id} · v${report.profile.version}`,
    tests: report.rows.length,
    source: count("source_verdict"),
    target: count("target_verdict"),
    attention: report.rows
      .filter((row) => row.target_verdict !== "PASS" || row.decision !== "PASS")
      .map(
        (row) =>
          `${row.test_id}: ${row.target_verdict} · ${row.reason}${row.exception_applied ? " · 예외 적용" : ""}`,
      ),
    evaluatedAt: report.evaluated_at,
    message:
      "서버에 기록된 비교 결과입니다. 실제 시험 환경은 실행 증거에서 확인하며, 이 요약은 전환 권한이나 개별 노드의 격리를 뜻하지 않습니다.",
  };
}

export function renderSecurityProgress(
  host,
  model,
  controls,
  now = Date.now(),
) {
  if (!host) return;
  const value = securityProgress(model, controls, now),
    doc = host.ownerDocument;
  const element = (tag, text, className) => {
    const node = doc.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  host.replaceChildren(element("h3", "보안 시험 진행 상태"));
  host.dataset.state = value.state;
  if (value.state !== "UNAVAILABLE") {
    const grid = element("div", "", "security-progress-grid");
    const totals = (counts) =>
      `통과 ${counts.PASS} · 실패 ${counts.FAIL} · 미확인 ${counts.UNKNOWN}`;
    for (const [title, detail] of [
      ["등록된 평가 범위", `${value.profile} · ${value.tests}개 시험`],
      ["출발지 결과", totals(value.source)],
      ["목적지 결과", totals(value.target)],
      ["T19 비교 판정", value.state],
    ]) {
      const card = element("div", "", "security-progress-step");
      card.append(element("strong", title), element("p", detail));
      grid.append(card);
    }
    host.append(grid);
    if (value.attention.length) {
      const list = doc.createElement("ul");
      for (const reason of value.attention) list.append(element("li", reason));
      host.append(list);
    }
  }
  host.append(element("p", value.message, "muted"));
}
