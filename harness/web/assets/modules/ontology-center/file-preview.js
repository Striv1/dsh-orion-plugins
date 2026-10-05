import { parseDocument } from "yaml";
(() => {
  const esc = value => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  const inline = value => esc(value).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  // Only escaped text enters generated markup. Embedded HTML is never executed.
  const markdown = text => {
    const lines = text.split(/\r?\n/); let html = '', code = null, list = false;
    const endList = () => { if (list) { html += '</ul>'; list = false; } };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (/^\s*```/.test(line)) { endList(); if (code !== null) { html += `<pre><code>${esc(code.join('\n'))}</code></pre>`; code = null; } else code = []; continue; }
      if (code !== null) { code.push(line); continue; }
      if (line.includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || '')) {
        endList(); const cells = row => row.trim().replace(/^\||\|$/g, '').split('|');
        html += '<div class="owa-vfp-table"><table><thead><tr>' + cells(line).map(c => `<th>${inline(c.trim())}</th>`).join('') + '</tr></thead><tbody>'; i++;
        while (i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].trim()) html += '<tr>' + cells(lines[++i]).map(c => `<td>${inline(c.trim())}</td>`).join('') + '</tr>';
        html += '</tbody></table></div>'; continue;
      }
      if (/^\s*[-*]\s+/.test(line)) { if (!list) html += '<ul>'; list = true; html += `<li>${inline(line.replace(/^\s*[-*]\s+/, ''))}</li>`; continue; }
      endList(); const heading = /^(#{1,6})\s+(.*)/.exec(line);
      if (heading) html += `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`;
      else if (/^>\s?/.test(line)) html += `<blockquote>${inline(line.replace(/^>\s?/, ''))}</blockquote>`;
      else if (/^\s*---+\s*$/.test(line)) html += '<hr>';
      else if (line.trim()) html += `<p>${inline(line)}</p>`;
    }
    endList(); if (code !== null) html += `<pre><code>${esc(code.join('\n'))}</code></pre>`;
    return `<div class="owa-vfp-document">${html}</div>`;
  };
  const labels = { rule_id: '规则编号', capability_name: '规则能力', expression: '规则表达式', description: '说明', conditions: '判定条件', conclusion: '结论', case_type: '用例类型', expected_outcome: '预期结果', note: '说明', executor: '执行器', validation: '验证要求',  mappings: '映射条目', rules: '规则', facts: '事实记录', classes: '类型', data_properties: '数据属性', object_properties: '对象关系', source: '来源', target: '目标', predicate: '事实类型', fact: '事实表达', provenance: '来源证据', status: '状态', description_zh: '中文说明', source_refs: '证据引用', validation_cases: '验证用例', files: '文件清单', name: '名称', id: '编号', schema_version: '格式版本', release_version: '发布版本', summary: '摘要', errors: '错误', warnings: '提示', passed: '通过', failed: '失败' };
  const structured = (value, depth = 0) => {
    if (value === null || typeof value !== 'object') return `<span class="owa-vfp-value${typeof value === 'boolean' ? ' owa-vfp-boolean' : ''}">${esc(value === null ? '未填写 (null)' : value)}</span>`;
    if (depth > 5) return `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`;
    if (Array.isArray(value) && value.length && value.every(item => item && typeof item === 'object' && 'source' in item && 'target' in item)) {
      return `<p>共 ${value.length} 条映射${value.length > 200 ? '，展示前 200 条' : ''}</p><div class="owa-vfp-table"><table><thead><tr><th>来源</th><th>目标</th><th>转换口径 / 说明</th></tr></thead><tbody>${value.slice(0,200).map(item => `<tr><td>${esc(item.source)}</td><td>${esc(item.target_label_zh || item.target)}<small>${esc(item.target)}</small></td><td>${esc(item.target_comment_zh || item.transformation || item.mapping_type || '未提供')}<small>${esc((item.source_refs || []).join?.('、') || '')}</small></td></tr>`).join('')}</tbody></table></div>`;
    }
    if (Array.isArray(value)) return `<p class="owa-vfp-note">共 ${value.length} 项${value.length > 60 ? '，此处展示前 60 项；完整数据见原文件。' : ''}</p>` + value.slice(0, 60).map((item, i) => `<details${i < 3 ? ' open' : ''}><summary>${esc(item?.rule_id || item?.id || item?.name || item?.fact || `第 ${i + 1} 项`)}</summary>${structured(item, depth + 1)}</details>`).join('');
    const entries = Object.entries(value);
    const scalars = entries.filter(([,v]) => v === null || typeof v !== 'object');
    const groups = entries.filter(([,v]) => v !== null && typeof v === 'object');
    return (scalars.length ? `<dl class="owa-vfp-fields">${scalars.map(([k,v]) => `<dt>${esc(labels[k] || k)}${labels[k] ? `<small>${esc(k)}</small>` : ''}</dt><dd>${structured(v,depth+1)}</dd>`).join('')}</dl>` : '') + groups.map(([k,v]) => `<section class="owa-vfp-section"><h3>${esc(labels[k] || k)}${labels[k] ? `<small>${esc(k)}</small>` : ''}</h3>${structured(v,depth+1)}</section>`).join('');
  };
  const renderContent = (text, extension) => {
    if (extension === 'md') return markdown(text);
    if (['yaml','yml'].includes(extension)) { try { const doc = parseDocument(text); if (doc.errors.length) throw doc.errors[0]; return structured(doc.toJS({maxAliasCount:50})); } catch { return '<p>YAML 无法安全解析，以下保留原文。</p><pre>' + esc(text) + '</pre>'; } }
    if (extension === 'json') { try { return structured(JSON.parse(text)); } catch { return '<p>JSON 格式无法解析，以下保留原文。</p><pre>' + esc(text) + '</pre>'; } }
    return `<p class="owa-vfp-note">${['yaml','yml','ttl','owl','obda'].includes(extension) ? '以下为完整文本。上方说明解释其用途；此视图不执行映射或推理，也不将文本行数当作对象数量。' : '文本预览'}</p><pre>${esc(text)}</pre>`;
  };
  let dialog = null, controller = null, focus = null;
  const close = () => { controller?.abort(); controller = null; if (dialog) { dialog.close(); dialog.remove(); dialog = null; focus?.focus?.(); } };
  const open = async (asset, file, guide) => {
    close(); focus = document.activeElement;
    const current = document.createElement('dialog'); dialog = current;
    const request = new AbortController(); controller = request;
    const href = window.__ORION_ONTOLOGY_API__.artifactUrl(asset.sourceProjectId, file.path);
    current.className = 'owa-file-preview';
    current.innerHTML = `<header><div><small>${esc(asset.name)} · v${esc(asset.version)}</small><h2>${esc(file.display_name || file.path.split('/').at(-1))}</h2></div><button type="button" data-preview-close aria-label="关闭文件预览">关闭预览</button></header><section class="owa-vfp-context"><p><b>${esc(guide.role)}</b> · ${esc(guide.purpose)}</p><details><summary>输入、输出与版本信息</summary><dl><dt>输入 / 依据</dt><dd>${esc(guide.inputs)}</dd><dt>输出 / 使用者</dt><dd>${esc(guide.outputs)}</dd><dt>修改影响</dt><dd>${esc(guide.impact)}</dd><dt>文件状态</dt><dd>${esc(file.lifecycle_status || '账本未提供状态')}</dd><dt>文件路径</dt><dd>${esc(file.path)}</dd><dt>账本指纹</dt><dd>${esc(file.sha256 || '未提供')}</dd></dl><small>用途说明不代替本版本的实际执行记录。</small></details><a href="${esc(href)}" target="_blank" rel="noopener">打开原文件</a> · <a href="${esc(href)}" download="${esc(file.path.split('/').at(-1))}">下载原文件</a></section><nav class="owa-vfp-toolbar" aria-label="预览显示方式"><div><button type="button" data-mode="read" aria-pressed="true">阅读视图</button><button type="button" data-mode="raw" aria-pressed="false">源文件</button></div><span data-format></span></nav><article class="owa-vfp-content" aria-live="polite">正在读取原文件…</article>`;
    current.querySelector('[data-preview-close]').addEventListener('click', close);
    current.addEventListener('cancel', event => { event.preventDefault(); event.stopPropagation(); close(); });
    current.addEventListener('keydown', event => { if (event.key === 'Escape') event.stopPropagation(); });
    document.body.appendChild(current); current.showModal();
    const body = current.querySelector('.owa-vfp-content');
    const ext = file.path.split('.').at(-1).toLowerCase();
    current.querySelector('[data-format]').textContent = ext.toUpperCase() + ' · 只读预览';
    if (!['md','json','yaml','yml','ttl','owl','obda','txt','csv','jsonl','rq','sql'].includes(ext)) {
      body.textContent = '此格式请使用上方“打开原文件”查看，或下载后用对应软件打开。'; return;
    }
    try {
      const response = await fetch(href, { credentials: 'same-origin', signal: request.signal });
      if (!response.ok) throw new Error(`文件读取失败（HTTP ${response.status}），请重新打开或下载原文件。`);
      const limit = 2 * 1024 * 1024;
      if (Number(response.headers.get('content-length')) > limit) { await response.body?.cancel(); throw new Error('文件超过 2 MB，预览暂不展开。请下载原文件；这不表示文件为空。'); }
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let text = '', total = 0;
      while (true) { const {done, value} = await reader.read(); if (done) break; total += value.byteLength; if (total > limit) { await reader.cancel(); throw new Error('文件超过预览大小限制，请下载原文件。'); } text += decoder.decode(value, {stream:true}); }
      text += decoder.decode();
      if (dialog !== current || request.signal.aborted) return;
      const formatted = text.length ? renderContent(text, ext) : '<p>该原文件为零字节，没有正文。</p>';
      body.innerHTML = formatted;
      current.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {
        body.innerHTML = button.dataset.mode === 'raw' ? `<pre class="owa-vfp-source"><code>${esc(text)}</code></pre>` : formatted;
        body.scrollTop = 0;
        current.querySelectorAll('[data-mode]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
      }));
    } catch (error) { if (dialog === current && !request.signal.aborted) body.textContent = error.message || '文件读取失败'; }
  };
  window.__ORION_FILE_PREVIEW__ = { open, close, renderContent };
})();
