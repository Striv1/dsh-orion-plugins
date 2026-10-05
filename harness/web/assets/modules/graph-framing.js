// Frame the actual projected node cloud, not its world-axis bounding cube.
export function perspectiveGraphFrame(nodes, direction, fov, aspect) {
  const points = nodes.filter(n => [n.x, n.y, n.z].every(Number.isFinite));
  if (!points.length || !(aspect > 0)) return null;
  const center = Object.fromEntries(['x','y','z'].map(axis => [axis, (Math.min(...points.map(n => n[axis])) + Math.max(...points.map(n => n[axis]))) / 2]));
  const length = Math.hypot(direction.x, direction.y, direction.z) || 1;
  const forward = [direction.x / length, direction.y / length, direction.z / length];
  if (Math.hypot(...forward) < .5) forward[2] = 1;
  const horizontal = Math.hypot(forward[0], forward[2]);
  const right = horizontal > .001 ? [forward[2]/horizontal, 0, -forward[0]/horizontal] : [1,0,0];
  const up = [forward[1]*right[2], forward[2]*right[0]-forward[0]*right[2], -forward[1]*right[0]];
  const verticalTan = Math.tan(fov * Math.PI / 360) * .86;
  const horizontalTan = verticalTan * aspect;
  let distance = 40;
  for (const node of points) {
    const offset = [node.x-center.x, node.y-center.y, node.z-center.z];
    const dot = basis => offset.reduce((sum, value, i) => sum + value*basis[i], 0);
    distance = Math.max(distance, dot(forward) + (Math.abs(dot(right))+14)/horizontalTan, dot(forward) + (Math.abs(dot(up))+14)/verticalTan);
  }
  return { center, position: Object.fromEntries(['x','y','z'].map((axis,i) => [axis, center[axis]+forward[i]*distance])) };
}
