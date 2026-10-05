// Canvas cannot inherit CSS text colors. Read the same variables as the graph
// chrome once per theme change, rather than resolving styles for every node.
const CANVAS_THEME = {
  labelColor: ["--graph-text", "#17263b"],
  selectedLabelColor: ["--graph-text", "#17263b"],
  selectedLabelBackground: ["--graph-label-selected-bg", "#e1ebfa"],
  hoverLabelBackground: ["--graph-label-hover-bg", "#e6eef8"],
  labelOutline: ["--graph-label-outline", "#f2f5f9"],
  nodeStroke: ["--graph-node-stroke", "#526a81"],
  gridColor: ["--graph-grid", "rgba(64,100,145,.12)"],
  layerGridColor: ["--graph-layer-grid", "rgba(64,100,145,.22)"],
  relationLabelColor: ["--graph-relation-label", "#774dac"],
  inheritanceLabelColor: ["--graph-inheritance-label", "#526577"],
  inheritance: ["--graph-edge-inheritance", "rgba(82,101,119,.64)"],
  inheritanceActive: ["--graph-edge-inheritance-active", "rgba(38,58,79,.95)"],
  relation: ["--graph-edge-relation", "rgba(119,77,172,.72)"],
  relationActive: ["--graph-edge-relation-active", "rgba(99,53,143,.95)"],
  faded: ["--graph-edge-faded", "rgba(72,94,121,.2)"],
  instance: ["--graph-edge-instance", "rgba(82,111,132,.5)"],
  instanceActive: ["--graph-edge-instance-active", "rgba(38,72,97,.95)"],
  instanceHovered: ["--graph-edge-instance-hover", "rgba(82,111,132,.68)"],
  instanceFaded: ["--graph-edge-instance-faded", "rgba(72,94,121,.18)"],
};

export function readGraphCanvasTheme(element, getStyle = globalThis.getComputedStyle) {
  const style = element && typeof getStyle === "function" ? getStyle(element) : null;
  return Object.fromEntries(Object.entries(CANVAS_THEME).map(([key, [name, fallback]]) =>
    [key, style?.getPropertyValue(name).trim() || fallback]));
}
