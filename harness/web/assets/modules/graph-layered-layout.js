const KIND_ORDER = ['class', 'objectProperty', 'dataProperty', 'individual'];
const BASE_RADII = [64, 112, 160, 220];
const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));
const compare = (a, b) => String(a) < String(b) ? -1 : String(a) > String(b) ? 1 : 0;

// Coordinates only: semantic edges and caller-owned node objects stay untouched.
// Dense 2D layers use nearby rings; the next kind always starts outside them.
export function layeredGraphPositions(nodes, { dimensions = 3 } = {}) {
  const groups = new Map();
  for (const node of nodes) {
    const kind = node.kind ?? 'unknown';
    if (!groups.has(kind)) groups.set(kind, []);
    groups.get(kind).push(node);
  }
  const kinds = [...KIND_ORDER, ...[...groups.keys()].filter(kind => !KIND_ORDER.includes(kind)).sort(compare)];
  const positions = new Map();
  let previousOuterRadius = 0;
  for (const [layer, kind] of kinds.entries()) {
    const group = groups.get(kind);
    if (!group?.length) continue;
    const sorted = [...group].sort((a, b) => compare(a.id, b.id));
    const radius = Math.max(BASE_RADII[layer] ?? 220 + (layer - 3) * 52, previousOuterRadius + 48);
    if (dimensions === 2) {
      const rings = [];
      let capacity = 0;
      do {
        const r = radius + rings.length * 26;
        const count = Math.max(1, Math.floor(2 * Math.PI * r / 24));
        rings.push({ radius: r, capacity: count });
        capacity += count;
      } while (capacity < sorted.length);
      let offset = 0;
      let remainingCapacity = capacity;
      for (const [ringIndex, ring] of rings.entries()) {
        const count = Math.min(ring.capacity, Math.ceil((sorted.length - offset) * ring.capacity / remainingCapacity));
        for (let i = 0; i < count; i++) {
          const angle = i * 2 * Math.PI / count + layer * GOLDEN_ANGLE + ringIndex * GOLDEN_ANGLE;
          positions.set(sorted[offset + i].id, { x: Math.cos(angle) * ring.radius, y: Math.sin(angle) * ring.radius, z: 0 });
        }
        offset += count;
        remainingCapacity -= ring.capacity;
      }
      previousOuterRadius = rings.at(-1).radius;
    } else {
      // Fibonacci spheres spread nodes over the whole shell without polar clumps.
      const shellRadius = Math.max(radius, Math.sqrt(sorted.length) * 7);
      sorted.forEach((node, i) => {
        const y = 1 - 2 * (i + .5) / sorted.length;
        const horizontal = Math.sqrt(1 - y * y);
        const angle = (i + layer) * GOLDEN_ANGLE;
        positions.set(node.id, { x: Math.cos(angle) * horizontal * shellRadius, y: y * shellRadius, z: Math.sin(angle) * horizontal * shellRadius });
      });
      previousOuterRadius = shellRadius;
    }
  }
  return positions;
}
