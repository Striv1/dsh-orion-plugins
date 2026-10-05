// Hover is transient; selection stays authoritative while the pointer moves.
export function graphNodeInteraction({ selected = false, hovered = false, hasSelection = false, hasHover = false, neighbor = false, palette = null } = {}) {
  const emphasized = selected || hovered;
  return {
    opacity: emphasized || neighbor ? 1 : hasSelection ? .14 : hasHover ? .78 : 1,
    ring: selected ? .35 : hovered ? .7 : null,
    halo: selected ? .2 : hovered ? .3 : 0,
    labelBackground: selected ? palette?.selectedLabelBackground ?? '#ededed' : hovered ? palette?.hoverLabelBackground ?? '#2a2a2a' : null,
    labelColor: selected ? palette?.selectedLabelColor ?? '#0a0a0a' : palette?.labelColor ?? '#ededed',
    bold: selected,
  };
}

export function tintToWhite(hex, amount) {
  const n = Number.parseInt(hex.slice(1), 16);
  return `rgb(${[n >> 16, (n >> 8) & 255, n & 255].map(c => Math.round(c + (255 - c) * amount)).join(',')})`;
}

// Values from the running 1516 rc5 asset index-CMzJLkub.js (not newer source).
export function graphEdgeInteraction({ model = true, kind = 'relationship', highlighted = false, selected = false, hovered = false, zoom = 1, palette = null } = {}) {
  if (model) {
    const inheritance = kind === 'subclass';
    const size = highlighted ? Math.max(inheritance ? .9 : 2, 1) * 1.5 : inheritance ? .9 : 2;
    const legacyColor = highlighted ? inheritance ? 'rgba(235,235,235,0.95)' : 'rgba(214,193,255,0.95)'
        : selected ? 'rgba(48,48,48,0.4)' : inheritance ? 'rgba(195,195,195,0.4)' : 'rgba(196,165,255,0.55)',
      themeKey = highlighted ? inheritance ? 'inheritanceActive' : 'relationActive'
        : selected ? 'faded' : inheritance ? 'inheritance' : 'relation';
    return {
      color: palette?.[themeKey] ?? legacyColor,
      width: Math.max(3, size * Math.sqrt(zoom)),
    };
  }
  const legacyColor = highlighted ? 'rgba(255,255,255,0.55)' : selected ? '#141414'
    : hovered ? 'rgba(51,51,51,0.824)' : 'rgba(163,163,163,0.2)',
    themeKey = highlighted ? 'instanceActive' : selected ? 'instanceFaded' : hovered ? 'instanceHovered' : 'instance';
  return {
    color: palette?.[themeKey] ?? legacyColor,
    width: (highlighted ? 1.85 : selected ? .6 : hovered ? .688 : 1) * Math.sqrt(zoom),
  };
}
