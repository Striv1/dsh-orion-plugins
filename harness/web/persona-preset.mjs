const PERSONA_ROW = /- id: persona\n[\s\S]*?(?=- id: agent-instructions\n)/;

function indentBlock(text) {
  return text.trimEnd().split("\n").map((line) => `      ${line}`).join("\n");
}

export function detectPersonaConfigSchema(personaRow) {
  const legacy = /^\s+text:\s/m.test(personaRow);
  const split = /^\s+prefix:\s/m.test(personaRow) || /^\s+suffix:\s/m.test(personaRow);
  if (legacy === split) {
    throw new Error("Harness persona 配置结构无法识别");
  }
  return split ? "prefix-suffix" : "text";
}

export function renderPersonaRow(persona, schema) {
  const body = indentBlock(persona);
  if (schema === "text") {
    return `- id: persona
  name: '@deepseek-ai/dsh-persona'
  config:
    text: |-
${body}

`;
  }
  if (schema === "prefix-suffix") {
    // Existing ORION personas already carry {{cwd}} and their complete policy.
    // Keep the whole audited text at order 0 and explicitly shadow the new
    // deployment suffix so the alpha.2 split does not duplicate or reorder it.
    return `- id: persona
  name: '@deepseek-ai/dsh-persona'
  config:
    prefix: |-
${body}
    suffix: ''

`;
  }
  throw new Error(`不支持的 Harness persona 配置结构：${schema}`);
}

export function replacePresetPersona(standardComposition, persona, failureLabel) {
  const match = standardComposition.match(PERSONA_ROW);
  if (!match) {
    throw new Error(`无法从 Harness 标准预设生成 ${failureLabel}`);
  }
  const schema = detectPersonaConfigSchema(match[0]);
  return {
    composition: standardComposition.replace(
      PERSONA_ROW,
      renderPersonaRow(persona, schema),
    ),
    schema,
  };
}

const RC2_STANDARD_ROW = /^ {4}- id: preset-standard\n[\s\S]*?(?=^ {4}- id: |(?![\s\S]))/m;
const RC2_PERSONA_ROW = /^ {10}- id: persona\n[\s\S]*?(?=^ {10}- id: agent-instructions\n)/m;

const yamlString = (value) => JSON.stringify(String(value));

/**
 * rc.2 moved agent presets from standalone agent.cordis.yml files into
 * `@deepseek-ai/dsh-agent-preset` rows registered by the preset registry.
 * Derive ORION presets from the shipped standard row so upstream tool rows
 * stay current, replacing only the persona and preset metadata.
 */
export function renderRc2PresetRows(standardPatch, presets) {
  const match = standardPatch.match(RC2_STANDARD_ROW);
  if (!match) throw new Error("无法从 Harness rc.2 标准预设中找到 preset-standard");
  const standardRow = match[0].trimEnd();
  if (!RC2_PERSONA_ROW.test(standardRow)) {
    throw new Error("Harness rc.2 标准预设的 persona 结构无法识别");
  }
  const rows = presets.map(({ id, name, description, order, persona }) => {
    const body = persona.trimEnd().split("\n").map((line) => (line ? `                ${line}` : "")).join("\n");
    const personaRow = `          - id: persona
            name: '@deepseek-ai/dsh-persona'
            config:
              prefix: |-
${body}
              suffix: ''
`;
    return standardRow
      .replace(/^ {4}- id: preset-standard$/m, `    - id: preset-${id}`)
      .replace(
        /^ {8}id: standard\n {8}order: 1$/m,
        [
          `        id: ${id}`,
          `        name: ${yamlString(name)}`,
          `        description: ${yamlString(description)}`,
          `        order: ${Number(order)}`,
        ].join("\n"),
      )
      .replace(RC2_PERSONA_ROW, personaRow);
  });
  for (const [index, row] of rows.entries()) {
    if (!row.includes(`        id: ${presets[index].id}\n`)) {
      throw new Error(`无法生成 Harness rc.2 预设：${presets[index].id}`);
    }
  }
  return `# ORION presets derived from the shipped rc.2 standard preset.
- insert:
${rows.join("\n")}
`;
}
