// Layout is presentation only; it cannot create or remove dependency edges.
// Registered positions are hints. Repeated cells are placed on a new row so
// several resources cannot hide behind one selectable node.
export function workloadLayout(nodes) {
  const groups = { source: [], target: [], control: [] };
  for (const node of nodes) groups[node.lane].push(node);
  const positions = new Map();
  const slots = {};
  for (const [lane, rows] of Object.entries(groups)) {
    const occupied = new Set();
    slots[lane] = rows.map((n) => {
      const column = lane === "control" ? 0 : n.column;
      let row = n.row;
      while (occupied.has(`${column}:${row}`)) row++;
      occupied.add(`${column}:${row}`);
      return { node: n, column, row };
    });
  }
  const columns = Math.max(
    4,
    ...[...slots.source, ...slots.target].map((n) => n.column + 1),
  );
  const environmentWidth = columns * 170 + 50;
  const sourceHeight = Math.max(
    270,
    ...slots.source.map((n) => n.row * 120 + 160),
  );
  const targetHeight = Math.max(
    170,
    ...slots.target.map((n) => n.row * 120 + 160),
  );
  const targetY = 35 + sourceHeight + 40;
  const controlX = environmentWidth + 40;
  const height = Math.max(
    targetY + targetHeight + 30,
    ...slots.control.map((n) => n.row * 140 + 210),
  );
  for (const [lane, rows] of Object.entries(slots)) {
    for (const { node, row, column } of rows) {
      positions.set(node.id, {
        x: lane === "control" ? controlX + 100 : 100 + column * 170,
        y:
          lane === "source"
            ? 115 + row * 120
            : lane === "target"
              ? targetY + 90 + row * 120
              : 115 + row * 140,
      });
    }
  }
  return {
    positions,
    width: controlX + 220,
    height,
    lanes: [
      [18, 35, environmentWidth, sourceHeight, "출발 환경"],
      [18, targetY, environmentWidth, targetHeight, "대체 실행 환경"],
      [controlX, 35, 200, height - 65, "보호·제어"],
    ],
  };
}
