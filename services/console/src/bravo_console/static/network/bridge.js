// Sova's same-origin, plan-authorized, read-only transport for the pinned atlas.
const plan = new URLSearchParams(location.search).get("plan");
const valid =
  typeof plan === "string" && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(plan);
export function networkURL(kind, socket = false) {
  if (!valid || !["status", "stream"].includes(kind))
    throw Error("등록된 계획을 Sova 콘솔에서 먼저 연결해 주세요.");
  const url = new URL(`/v1/plans/${plan}/network-${kind}`, location.origin);
  if (socket) url.protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return url.href;
}
export function clearNetworkScope(
  reason = "조회 권한이 만료되거나 회수됐습니다. 관측 자료를 지웠습니다.",
) {
  document.getElementById("sova-network-scope").textContent = reason;
}
export function showNetworkScope(data) {
  const context = data.sova_context;
  if (
    !valid ||
    context?.plan_id !== plan ||
    context.authorizes_cutover !== false ||
    context.scope !== "shared-environment-network"
  )
    throw Error("network_plan_mismatch");
  document.getElementById("sova-network-scope").textContent =
    `공유 환경 네트워크 · ${context.source_environment_id} → ${context.target_environment_id} · 앱 내부 의존성·접속 성공·게이트 통과를 뜻하지 않습니다. 프로토콜과 원본 도면은 날짜가 있는 참고 자료입니다.`;
}
if (!valid) {
  document.getElementById("sova-network-scope").textContent =
    "Sova 운영 콘솔에서 계획을 연결한 뒤 네트워크 지도를 열어 주세요.";
}
