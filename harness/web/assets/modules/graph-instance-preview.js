export const instancePreviewNotice = (preview) => {
  if (preview?.status === "UNAVAILABLE") {
    return preview.reason === "NO_MATERIALIZED_GRAPH"
      ? "当前版本没有可预览的实例图，仅展示本体模型。"
      : "实例预览暂不可用，当前仅展示本体模型。请查看该版本的 S6 验收报告。";
  }
  if (!preview || preview.complete !== false) return "";
  const count = (value) => Number.isSafeInteger(value) && value >= 0;
  const parts = ["实例预览"];
  if (count(preview.selected_subject_count)) parts.push(`选中主体 ${preview.selected_subject_count}`);
  if (count(preview.context_individual_count)) parts.push(`关联上下文 ${preview.context_individual_count}`);
  if (count(preview.displayed_individual_count)) parts.push(`当前加载 ${preview.displayed_individual_count} 个实例`);
  if (count(preview.full_graph_triple_count)) parts.push(`S6 验收完整图 ${preview.full_graph_triple_count.toLocaleString("zh-CN")} 条三元组`);
  parts.push("仅展示部分实例与关系，完整数据保留在工程中");
  return parts.join(" · ");
};
