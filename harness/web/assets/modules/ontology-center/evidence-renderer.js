(() => {
  if (window.__ORION_EVIDENCE_RENDERER__) return;

  const appendEvidenceList = (parent, title, values) => {
    if (!Array.isArray(values) || !values.length) return;
    const section = document.createElement("section");
    const heading = document.createElement("strong");
    heading.textContent = title;
    const list = document.createElement("ul");
    for (const value of values) {
      const item = document.createElement("li");
      item.textContent = value;
      list.append(item);
    }
    section.append(heading, list);
    parent.append(section);
  };

  const evidenceValueText = (value) => typeof value === "string" ? value : JSON.stringify(value, null, 2);

  const appendEvidencePages = (parent, title, values, renderItem = null) => {
    if (!Array.isArray(values) || !values.length) return;
    const section = document.createElement("section");
    const heading = document.createElement("strong");
    heading.textContent = `${title}（${values.length} 项）`;
    const list = document.createElement("ol");
    const nav = document.createElement("div");
    nav.dataset.evidencePagination = "true";
    const previous = document.createElement("button");
    const next = document.createElement("button");
    const count = document.createElement("span");
    previous.type = next.type = "button";
    previous.textContent = "上一页";
    next.textContent = "下一页";
    let page = 0;
    const paint = () => {
      list.replaceChildren();
      const start = page * 20;
      const end = Math.min(start + 20, values.length);
      list.start = start + 1;
      for (const value of values.slice(start, end)) {
        const item = document.createElement("li");
        if (renderItem) renderItem(item, value);
        else {
          const text = document.createElement("pre");
          text.textContent = evidenceValueText(value);
          item.append(text);
        }
        list.append(item);
      }
      count.textContent = `显示 ${start + 1}–${end} / ${values.length} 项；分页展示，记录未截断`;
      previous.disabled = page === 0;
      next.disabled = end === values.length;
    };
    previous.addEventListener("click", () => { if (page > 0) { page -= 1; paint(); } });
    next.addEventListener("click", () => { if ((page + 1) * 20 < values.length) { page += 1; paint(); } });
    if (values.length > 20) nav.append(previous, count, next);
    else nav.append(count);
    section.append(heading, list, nav);
    parent.append(section);
    paint();
  };

  const renderFullEvidence = (body, detail) => {
    body.replaceChildren();
    const modelAnalysis = source => ["RELEASE_SNAPSHOT_DATABASE", "LIVE_SOURCE_DATABASE"].includes(source.analysis?.execution_scope);
    const hasModelAnalysis = (detail.source_facts ?? []).some(modelAnalysis);
    const technical = hasModelAnalysis ? document.createElement("details") : body;
    if (hasModelAnalysis) {
      const label = document.createElement("summary");
      label.textContent = "证据技术与来源追溯详情";
      technical.append(label);
    }
    const meta = document.createElement("p");
    meta.textContent = `项目 ${detail.project_id} · 发布 ${detail.release_version} · ${detail.release_fingerprint} · 查询 ${detail.query_id ?? "未提供"} · 回执生成时间 ${detail.generated_at ?? "未提供"}`;
    technical.append(meta);
    if (detail.receipt_id) appendEvidenceList(technical, "不可变证据回执", [`${detail.receipt_id} · ${detail.receipt_sha256}`]);
    // The durable native log contains only a receipt pointer. Source metadata
    // must come from the identity/hash-verified receipt loaded on expansion.
    renderEvidencePanel(technical, detail.source_provenance);
    if (hasModelAnalysis) {
      const traceNote = document.createElement("p");
      traceNote.textContent = "以上发布包快照用于追溯业务定义与来源；本次读取的数据范围和时间，以各项分析结果的说明为准。";
      technical.append(traceNote);
      appendEvidenceList(body, "来源降级与 UNKNOWN 原因", detail.source_provenance?.degraded ?? []);
    }
    const boundary = document.createElement("p");
    boundary.textContent = `回答状态 ${detail.status}（${detail.complete ? "完整" : "不完整"}）。以下为本次工具返回的来源事实与规则执行记录；输入总数与已回传明细分别列出，缺少的事实或步骤不作补写。`;
    body.append(boundary);
    for (const source of detail.source_facts ?? []) {
      const section = document.createElement("section");
      const heading = document.createElement("strong");
      const isModelAnalysis = modelAnalysis(source) && window.__ORION_ANALYTICS_PANEL__;
      heading.textContent = isModelAnalysis ? "本次分析结果" : `来源事实 · ${source.source_ref ?? source.evidence_id ?? "未提供来源"}`;
      const scope = document.createElement("p");
      scope.textContent = `查询 ${source.query_template ?? "资料证据"} · 回执声明 ${source.reported_count ?? "未提供"} 条 · 已回传 ${(source.rows ?? []).length} 条明细`;
      section.append(heading);
      const sourceMeta = isModelAnalysis ? technical : section;
      sourceMeta.append(scope);
      if (source.fact_source) appendEvidenceList(isModelAnalysis ? section : sourceMeta, isModelAnalysis ? "统计范围说明" : "资料定位", [evidenceValueText(source.fact_source)]);
      if (Object.keys(source.parameters ?? {}).length) appendEvidenceList(sourceMeta, "本次查询参数", [evidenceValueText(source.parameters)]);
      if (isModelAnalysis) {
        window.__ORION_ANALYTICS_PANEL__.renderResult(section, {
          ...source, truncated: source.result_truncated === true || source.truncated === true,
          session_id: detail.session_id, query_id: detail.query_id,
          expected_release: { project_id: detail.project_id, release_version: detail.release_version,
            release_fingerprint: detail.release_fingerprint },
          evidence_receipt: detail.receipt_id ? { receipt_id: detail.receipt_id,
            sha256: detail.receipt_sha256, query_id: detail.query_id } : null,
        });
      } else {
        if (source.analysis) renderAnalysis(section, source);
        appendEvidencePages(section, source.analysis ? "统计结果" : "查询返回的事实记录", source.rows);
      }
      body.append(section);
    }
    const reasoning = detail.reasoning ?? [];
    if (!reasoning.length) {
      const empty = document.createElement("p");
      empty.textContent = hasModelAnalysis && !detail.reasoning_status?.degraded_reason
        ? "本次展示本体模型约束下的数据统计；回执没有规则推导记录，未将统计结果当作规则推理结论。"
        : `本次回执没有规则执行记录（${detail.reasoning_status?.status ?? "未请求或未返回"}），不能据此展示推导链。${detail.reasoning_status?.degraded_reason ?? ""}`;
      body.append(empty);
    }
    for (const record of reasoning) {
      const section = document.createElement("section");
      const title = document.createElement("strong");
      title.textContent = record.description_zh ?? record.capability_name ?? "本次规则执行";
      const scope = document.createElement("p");
      scope.textContent = `执行器 ${record.engine ?? "未提供"} · 范围 ${record.execution_scope ?? "未提供"} · 输入 ${record.input_fact_count ?? "未提供"} 条 · 触发 ${record.rules_fired ?? "未提供"} 次 · 目标结论 ${(record.result_facts ?? []).length} 条`;
      section.append(title, scope);
      if (record.conclusion_boundary_zh) {
        const boundary = document.createElement("p");
        boundary.textContent = record.conclusion_boundary_zh;
        section.append(boundary);
      }
      if (record.input_facts_returned) appendEvidencePages(section, "实际交给执行器的输入事实", record.input_facts);
      else {
        const missing = document.createElement("p");
        missing.textContent = "该历史回执没有保存完整输入事实，仅能核对已记录步骤的匹配前提与输入数量/哈希。";
        section.append(missing);
      }
      appendEvidenceList(section, "正式规则与输入凭证", [
        `规则包 ${record.rule_artifact ?? "未提供"} · ${record.rule_sha256 ?? "哈希未提供"}`,
        `输入事实哈希 ${record.input_facts_sha256 ?? "未提供"}`,
        ...(record.rules ?? []).map((rule) => `${rule.rule_id ?? "未提供 ID"} · ${rule.description_zh ?? "未提供说明"}${rule.expression ? ` · ${rule.expression}` : ""}${rule.confidence != null ? ` · 规则声明置信度 ${rule.confidence}` : ""}`),
      ]);
      appendEvidencePages(section, "事实 → 规则 → 结论", record.steps, (item, step) => {
        const label = document.createElement("strong");
        label.textContent = `${step.rule_id ?? "未提供规则 ID"} · ${step.rule_description_zh ?? "规则执行步骤"}`;
        item.append(label);
        appendEvidenceList(item, "已匹配前提", (step.premises ?? []).map(evidenceValueText));
        appendEvidenceList(item, "正式规则", [step.rule_expression ?? "执行回执未提供表达式"]);
        appendEvidenceList(item, "推出结论", [step.conclusion ?? "执行回执未提供结论"]);
        appendEvidenceList(item, "闭世界否定依据", (step.closed_world_negations ?? []).map(evidenceValueText));
        appendEvidenceList(item, "置信度（规则/执行器声明）", [
          `规则 ${step.declared_rule_confidence ?? "未提供"} · 执行器 ${step.engine_confidence ?? "未提供"}`,
        ]);
      });
      if (!(record.steps ?? []).length) {
        const empty = document.createElement("p");
        empty.textContent = "执行回执未返回推导步骤；不得补造未触发原因或推导链。目标结论为零时，仅表示本次没有推出目标事实。";
        section.append(empty);
      }
      appendEvidencePages(section, "目标业务结论与本体语义", record.semantic_result_facts);
      appendEvidencePages(section, "目标结论原子", record.result_facts);
      appendEvidencePages(section, "全部已回传派生事实", record.derived_facts);
      appendEvidenceList(section, "执行提示", record.warnings);
      body.append(section);
    }
    appendEvidenceList(body, "证据不足与降级说明", [
      ...(detail.warnings ?? []).map(String),
      ...(detail.degraded ?? []).map((item) => `${item.source_ref ?? item.source_kind ?? "来源"} · ${item.reason ?? "原因未提供"}`),
    ]);
    if (hasModelAnalysis) body.append(technical);
  };

  const renderAnalysis = (body, source) => {
    const analysis = source.analysis;
    const panel = document.createElement("section");
    panel.className = "orion-analysis-chart";
    const title = document.createElement("strong");
    title.textContent = "分组统计与明细";
    const scope = document.createElement("p");
    scope.textContent = `来源 ${analysis.source_row_count} 条完整结果 · 本次筛选 ${analysis.matched_row_count} 条。点击分组查看原始明细。`;
    const metricPicker = document.createElement("div");
    const chart = document.createElement("div");
    chart.className = "orion-analysis-bars";
    const detail = document.createElement("div");
    const operationLabels = {count_rows: "记录数", count_non_null: "非空计数", count_distinct: "去重计数", sum: "合计", avg: "平均", min: "最小", max: "最大"};
    const metrics = analysis.plan?.metrics ?? [];
    const draw = (metric) => {
      chart.replaceChildren();
      detail.replaceChildren();
      const numeric = (source.rows ?? []).map(row => row[metric.name]).filter(value => value != null).map(Number).filter(Number.isFinite);
      const min = Math.min(0, ...numeric);
      const max = Math.max(1, ...numeric);
      (source.rows ?? []).forEach((row, index) => {
        const values = (analysis.plan?.dimensions ?? []).map((dimension, i) => `${dimension.field}：${row[`group_${i+1}`] ?? "未知/缺失"}`);
        const label = values.join(" · ") || "全部结果";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "orion-analysis-bar";
        const text = document.createElement("span");
        text.textContent = `${label} · ${row[metric.name] ?? "无有效数值"}`;
        button.append(text);
        if (row[metric.name] != null && Number.isFinite(Number(row[metric.name]))) {
          const bar = document.createElement("meter");
          bar.min = min; bar.max = max; bar.value = Number(row[metric.name]);
          bar.title = text.textContent;
          button.append(bar);
        }
        button.addEventListener("click", () => {
          detail.replaceChildren();
          const indices = analysis.groups?.[index]?.source_row_numbers ?? [];
          const records = indices.map(number => ({来源行号: number + 1, 记录: analysis.detail_rows?.[number]}));
          appendEvidencePages(detail, `${label} · ${records.length} 条来源明细`, records);
        });
        chart.append(button);
      });
    };
    for (const metric of metrics) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `${metric.field ? `${metric.field} · ` : ""}${operationLabels[metric.operation] ?? metric.operation}`;
      button.addEventListener("click", () => draw(metric));
      metricPicker.append(button);
    }
    panel.append(title, scope, metricPicker, chart, detail);
    if (metrics.length) draw(metrics[0]);
    const lineage = document.createElement("p");
    lineage.textContent = `来源回执 ${analysis.source_receipt?.receipt_id ?? "未提供"} · Wren ${analysis.engine_version} · ${analysis.mdl_sha256}`;
    panel.append(lineage);
    body.append(panel);
  };

  const renderEvidencePanel = (body, evidence) => {
    if (!evidence || typeof evidence !== "object") return;
    body.dataset.evidenceBundleV2 = "true";
    const summary = document.createElement("p");
    summary.textContent = [
      `结论 ${evidence.complete === true ? "完整" : "不完整"}（${evidence.status ?? "unknown"}）`,
      `快照集 ${evidence.snapshot_set_id ?? "未绑定"}`,
      `发布指纹 ${evidence.release_fingerprint ?? "未绑定"}`,
    ].join(" · ");
    body.append(summary);
    appendEvidenceList(body, "数据时效与来源边界", [
      ...Object.entries(evidence.query_modes ?? {}).map(([name, mode]) => {
        if (mode === "SNAPSHOT_ONLY") return `${name}：快照查询（SNAPSHOT_ONLY）。本次执行成功不代表上游数据最新。`;
        if (mode === "HYBRID") return `${name}：混合查询（HYBRID）。上游实时性以实际补查回执为准。`;
        if (mode === "REALTIME_REQUIRED") return `${name}：要求实时查询（REALTIME_REQUIRED）。须核对下方实时补查回执。`;
        return `${name}：未声明数据时效。`;
      }),
      ...(evidence.source_trace?.snapshot_set_id && !evidence.snapshot_set_id
        ? [`发布包内 S1 追溯快照：${evidence.source_trace.snapshot_set_id}。旧版本未冻结完整运行快照合同，此身份仅供追溯。`]
        : []),
    ]);
    appendEvidenceList(
      body,
      "来源拓扑与快照",
      (evidence.sources ?? []).map((source) => {
        const scope = [source.engine, source.source_id].filter(Boolean).join("/");
        const snapshot = [source.dataset_id || (source.dataset_ids ?? []).join("、"), source.snapshot_version].filter(Boolean).join("@");
        const fields = [
          ...(source.tables ?? []),
          ...(source.columns ?? []).map((column) => `字段:${column}`),
        ].join("、");
        const provenance = source.provenance_scope === "PROTECTED_S1_TRACE_ONLY"
          ? "发布包内 S1 追溯"
          : source.provenance_scope === "RELEASE_SNAPSHOT_CONTRACT" ? "发布运行快照" : "来源合同未完整提供";
        return `${scope} · ${snapshot || "快照身份未提供"} · ${provenance} · 上游新鲜度 ${source.freshness} · 采集 ${source.observed_at ?? "未记录"} · 查询 ${source.queried_at ?? "未记录"} · PII ${source.pii_scope}/${source.masking}${fields ? ` · ${fields}` : ""}`;
      }),
    );
    appendEvidenceList(
      body,
      "统一语义与身份",
      (evidence.identities ?? []).map((identity) => (
        `${identity.contract_id} · ${identity.normalization}/${identity.cardinality} · 冲突策略 ${identity.collision_policy}`
      )),
    );
    appendEvidenceList(
      body,
      "推理与实时补查",
      [
        ...(evidence.sources ?? [])
          .filter((source) => source.rule_id || source.derived_fact_count)
          .map((source) => `${source.source_id} · 规则 ${source.rule_id ?? "未声明"} · 派生事实 ${source.derived_fact_count}`),
        ...(evidence.realtime ?? []).map((receipt) => (
          `${receipt.template_id} · ${receipt.status}${receipt.observed_at ? ` · ${receipt.observed_at}` : ""}`
        )),
      ],
    );
    appendEvidenceList(body, "降级与 UNKNOWN 原因", evidence.degraded ?? []);
  };

  window.__ORION_EVIDENCE_RENDERER__ = { renderFullEvidence, renderEvidencePanel };
})();
