from __future__ import annotations

import html
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from .stage_contracts import (
    STAGE_CONTRACT_VERSION,
    project_stage_contract_version,
    stage_contract,
)


def _stage_title(project: dict[str, Any], stage: str, legacy: str, *, report: bool = False) -> str:
    if project_stage_contract_version(project) != STAGE_CONTRACT_VERSION:
        return legacy
    return f"{stage} {stage_contract(stage)['name']}" + ("报告" if report else "")


ASSET_ROOT = Path(__file__).with_name("report_assets")
REPORT_RENDERER_MARKER = "ORION report renderer v6.0 chinese-stage-summary"

STATUS_LABELS = {
    "PASSED": "已通过",
    "RUNNING": "正在执行",
    "PENDING": "待开始",
    "BLOCKED_HUMAN": "等待负责人确认",
    "DEFERRED": "已暂缓发布",
    "REVOKED": "已撤回",
    "FAILED": "校验失败",
    "NOT_APPLICABLE": "当前项目不适用该阶段",
    "DATABASE_FACT": "数据库事实",
    "DOCUMENT_EVIDENCE": "资料证据事实",
    "AI_INFERENCE": "人工智能辅助判断",
    "NEEDS_HUMAN_CONFIRMATION": "待本体工程师确认",
    "RESOLVED": "已完成确认",
}

KIND_LABELS = {
    "CLASS": "业务类",
    "OBJECT_PROPERTY": "业务关系",
    "DATA_PROPERTY": "数据属性",
    "ENUM": "枚举或固定取值集合",
    "RULE_CANDIDATE": "业务规则候选",
}

ONTOLOGY_NAME_LABELS = {
    "Company": "公司",
    "Plant": "工厂",
    "Warehouse": "仓库",
    "ProductionLine": "生产线",
    "ProductionPlan": "生产计划",
    "MaterialCategory": "物料分类",
    "Material": "物料",
    "Supplier": "供应商",
    "SupplierMaterialQualification": "供应商物料资格",
    "PurchaseContract": "采购合同",
    "ContractClause": "合同条款",
    "PurchaseOrder": "采购订单",
    "PurchaseOrderLine": "采购订单行",
    "DeliveryCommitment": "交付承诺",
    "MaterialRequirement": "物料需求",
    "InventorySnapshot": "库存快照",
    "QualityIncident": "质量事件",
    "ExpeditingTask": "催交任务",
    "AlternateSourcingRequest": "备选寻源请求",
    "Document": "业务文档",
    "SupplyActionLog": "保供动作日志",
    "belongsTo": "属于公司",
    "locatedAt": "位于工厂",
    "operatedAt": "在工厂运营",
    "scheduledOn": "安排在生产线",
    "classifiedBy": "按物料分类归类",
    "subCategoryOf": "属于上级分类",
    "supplies": "供应",
    "qualifiedFor": "具备物料供货资格",
    "hasContract": "拥有采购合同",
    "hasClause": "合同包含条款",
    "placedByPlant": "由工厂下单",
    "orderedFrom": "向供应商下单",
    "underContract": "订单受合同约束",
    "hasLine": "订单包含订单行",
    "lineForMaterial": "订单行对应物料",
    "commitsLine": "交付承诺对应订单行",
    "requiredByPlan": "物料需求来自生产计划",
    "requiresMaterial": "需求指向物料",
    "stockedIn": "库存位于仓库",
    "holdsMaterial": "库存持有物料",
    "reportedAgainstSupplier": "质量事件涉及供应商",
    "reportedOnMaterial": "质量事件涉及物料",
    "escalatesOrder": "催交任务升级处理订单",
    "escalatedToSupplier": "催交任务指向供应商",
    "seeksMaterial": "备选寻源寻找物料",
    "sourcingForPlant": "为工厂执行寻源",
    "triggeredByOrder": "由采购订单触发",
    "recommendsSupplier": "推荐备选供应商",
    "docAboutSupplier": "文档涉及供应商",
    "docAboutMaterial": "文档涉及物料",
    "docAboutOrder": "文档涉及采购订单",
    "actedOnOrder": "动作作用于采购订单",
    "supplierLevel": "供应商等级",
    "onTimeDeliveryRate": "准时交付率",
    "qualityScore": "质量评分",
    "criticality": "物料关键度",
    "standardLeadDays": "标准提前期天数",
    "safetyStock": "安全库存",
    "riskLevel": "风险等级",
    "orderedQuantity": "订购数量",
    "receivedQuantity": "已收货数量",
    "unitPrice": "单价",
    "committedDeliveryAt": "承诺交付时间",
    "committedQuantity": "承诺交付数量",
    "onHandQuantity": "账面库存数量",
    "availableQuantity": "可用库存数量",
    "severity": "严重程度",
    "affectedQuantity": "受影响数量",
    "SupplierLevel": "供应商等级枚举",
    "PurchaseOrderRiskLevel": "采购订单风险等级枚举",
    "PurchaseOrderStatus": "采购订单状态枚举",
    "MaterialCriticality": "物料关键度枚举",
    "QualificationStatus": "供货资格状态枚举",
    "DeliverySourceType": "交付来源类型枚举",
    "ContractClauseType": "合同条款类型枚举",
    "IncidentSeverity": "质量事件严重度枚举",
    "DocumentType": "文档类型枚举",
}

RELATIONSHIP_LABELS = {
    "BELONGS_TO": "属于公司",
    "LOCATED_AT": "位于工厂",
    "OPERATED_AT": "在工厂运营",
    "SCHEDULED_ON": "安排在生产线",
    "CLASSIFIED_BY": "按物料分类归类",
    "SUB_CATEGORY_OF": "属于上级分类",
    "SUPPLIES": "供应",
    "SUPPLIES_MATERIAL": "供应物料",
    "CONTRACT_WITH": "与供应商签订合同",
    "GOVERNED_BY": "受合同条款约束",
    "PLACED_BY_PLANT": "由工厂下单",
    "ORDERED_FROM": "向供应商下单",
    "UNDER_CONTRACT": "订单受合同约束",
    "LINE_OF": "属于采购订单",
    "FOR_MATERIAL": "对应物料",
    "COMMITS_LINE": "承诺对应订单行",
    "REQUIRED_BY_PLAN": "来自生产计划",
    "REQUIRES_MATERIAL": "需求指向物料",
    "STOCKED_IN": "库存位于仓库",
    "HOLDS_MATERIAL": "库存持有物料",
    "REPORTED_AGAINST_SUPPLIER": "质量事件涉及供应商",
    "REPORTED_ON_MATERIAL": "质量事件涉及物料",
    "ESCALATES_ORDER": "催交任务升级处理订单",
    "ESCALATED_TO_SUPPLIER": "催交任务指向供应商",
    "SEEKS_MATERIAL": "备选寻源寻找物料",
    "FOR_PLANT": "为工厂执行寻源",
    "TRIGGERED_BY_ORDER": "由采购订单触发",
    "RECOMMENDS_SUPPLIER": "推荐备选供应商",
    "ABOUT_SUPPLIER": "文档涉及供应商",
    "ABOUT_MATERIAL": "文档涉及物料",
    "ABOUT_ORDER": "文档涉及采购订单",
    "ACTED_ON_ORDER": "动作作用于采购订单",
}

TABLE_LABELS = {
    "sc_action_logs": "保供动作日志表",
    "sc_alternate_sourcing_requests": "备选寻源请求表",
    "sc_companies": "公司表",
    "sc_contract_clauses": "合同条款表",
    "sc_delivery_commitments": "交付承诺表",
    "sc_documents": "业务文档表",
    "sc_expediting_tasks": "催交任务表",
    "sc_inventory_snapshots": "库存快照表",
    "sc_material_categories": "物料分类表",
    "sc_material_requirements": "物料需求表",
    "sc_materials": "物料表",
    "sc_plants": "工厂表",
    "sc_production_lines": "生产线表",
    "sc_production_plans": "生产计划表",
    "sc_purchase_contracts": "采购合同表",
    "sc_purchase_order_lines": "采购订单行表",
    "sc_purchase_orders": "采购订单表",
    "sc_quality_incidents": "质量事件表",
    "sc_supplier_materials": "供应商物料资格表",
    "sc_suppliers": "供应商表",
    "sc_warehouses": "仓库表",
}

ENUM_DOMAIN_LABELS = {
    "action_status": "动作执行状态",
    "action_type": "动作类型",
    "alternate_sourcing_status": "备选寻源状态",
    "contract_clause_type": "合同条款类型",
    "contract_status": "合同状态",
    "currency": "币种",
    "delivery_source_type": "交付信息来源类型",
    "delivery_status": "交付承诺状态",
    "document_type": "文档类型",
    "expediting_priority": "催交优先级",
    "expediting_status": "催交任务状态",
    "expediting_task_type": "催交任务类型",
    "material_criticality": "物料关键度",
    "material_status": "物料状态",
    "po_risk_level": "采购订单风险等级",
    "po_status": "采购订单状态",
    "production_plan_status": "生产计划状态",
    "quality_incident_type": "质量事件类型",
    "quality_severity": "质量严重度",
    "quality_status": "质量事件状态",
    "supplier_level": "供应商等级",
    "supplier_material_qualification": "供应商物料资格状态",
    "supplier_status": "供应商状态",
    "warehouse_type": "仓库类型",
}

ENUM_VALUE_LABELS = {
    "EXECUTED": "已执行",
    "PROTECT_SUPPLY": "保供",
    "PENDING_REVIEW": "待评审",
    "DELIVERY_NOTICE": "交期变更通知",
    "LATE_DELIVERY": "延期交付",
    "ACTIVE": "有效/生效",
    "CNY": "人民币",
    "EDI": "电子数据交换",
    "EMAIL": "邮件",
    "SUPERSEDED": "已被新版本替代",
    "SUPPLIER_EMAIL": "供应商邮件",
    "CONTRACT": "合同",
    "QUALITY_REPORT": "质量报告",
    "PURCHASING_POLICY": "采购政策",
    "MATERIAL_SPEC": "物料规格",
    "URGENT": "紧急",
    "OPEN": "处理中/未关闭",
    "MANAGEMENT_ESCALATION": "管理层升级催交",
    "A": "A 级/高关键度",
    "B": "B 级/中关键度",
    "C": "C 级/一般关键度",
    "NORMAL": "正常",
    "WATCH": "关注",
    "HIGH": "高风险",
    "CRITICAL": "严重风险",
    "CONFIRMED": "已确认",
    "PARTIALLY_RECEIVED": "部分收货",
    "RELEASED": "已下达",
    "PLANNED": "已计划",
    "MAJOR": "重大",
    "MINOR": "一般",
    "CONTAINED": "已遏制",
    "CLOSED": "已关闭",
    "CORE": "核心供应商",
    "STRATEGIC": "战略供应商",
    "QUALIFIED": "合格供应商",
    "CONDITIONAL": "有条件供应商",
    "APPROVED": "已批准",
    "RAW_MATERIAL": "原材料仓",
}


def _now() -> str:
    value = datetime.now().astimezone()
    offset = value.strftime("%z")
    readable_offset = f"{offset[:3]}:{offset[3:]}" if offset else ""
    return f"{value:%Y年%m月%d日 %H:%M:%S} UTC{readable_offset}"


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else "—"))


def _status_label(value: Any) -> str:
    key = str(value or "UNCLASSIFIED")
    return STATUS_LABELS.get(key, "未分类状态")


def _kind_label(value: Any) -> str:
    key = str(value or "UNCLASSIFIED")
    return KIND_LABELS.get(key, "其他候选类型")


def _ontology_name(value: Any) -> str:
    key = str(value or "—")
    chinese = ONTOLOGY_NAME_LABELS.get(key, "尚未提供中文业务名称")
    return (
        '<span class="term-name">'
        f'<strong>{_escape(chinese)}</strong><code>{_escape(key)}</code></span>'
    )


def _relationship_name(value: Any) -> str:
    key = str(value or "UNCLASSIFIED")
    chinese = RELATIONSHIP_LABELS.get(key, "尚未解释的关系")
    return (
        '<span class="term-name">'
        f'<strong>{_escape(chinese)}</strong><code>{_escape(key)}</code></span>'
    )


def _table_name(value: Any) -> str:
    key = str(value or "—")
    chinese = TABLE_LABELS.get(key, "未提供中文表名")
    return (
        '<span class="term-name">'
        f'<strong>{_escape(chinese)}</strong><code>{_escape(key)}</code></span>'
    )


def _enum_domain_name(value: Any) -> str:
    key = str(value or "—")
    chinese = ENUM_DOMAIN_LABELS.get(key, "未提供中文业务域")
    return (
        '<span class="term-name">'
        f'<strong>{_escape(chinese)}</strong><code>{_escape(key)}</code></span>'
    )


def _enum_value(value: Any) -> str:
    key = str(value or "—")
    chinese = ENUM_VALUE_LABELS.get(key)
    return f"{_escape(chinese)}（{_code(key)}）" if chinese else _code(key)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ensure_assets(stage_dir: Path) -> None:
    sources = (
        ASSET_ROOT / "js/echarts.min.js",
        ASSET_ROOT / "css/orion-report.css",
        ASSET_ROOT / "js/orion-motion.js",
    )
    for source in sources:
        if not source.exists():
            raise RuntimeError(f"报告资源缺失：{source}")


def _inline_report_css() -> str:
    return (ASSET_ROOT / "css/orion-report.css").read_text(encoding="utf-8")


def _inline_script(path: Path) -> str:
    # Prevent an embedded library string from terminating the surrounding script tag.
    return path.read_text(encoding="utf-8").replace("</script", r"<\/script")


def _table(headers: list[str], rows: list[list[Any]], empty: str = "暂无数据") -> str:
    head = "".join(f"<th>{_escape(item)}</th>" for item in headers)
    if rows:
        body = "".join(
            "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
            for row in rows
        )
    else:
        body = f'<tr><td colspan="{len(headers)}" class="empty">{_escape(empty)}</td></tr>'
    return (
        '<div class="table-wrap"><table><thead><tr>'
        f"{head}</tr></thead><tbody>{body}</tbody></table></div>"
    )


def _badge(value: Any, tone: str = "neutral") -> str:
    return f'<span class="badge {tone}">{_escape(value)}</span>'


def _code(value: Any) -> str:
    return f"<code>{_escape(value)}</code>"


def _metric_cards(metrics: list[tuple[str, Any, str]]) -> str:
    cards = []
    for label, value, note in metrics:
        count_attribute = (
            f' data-count="{_escape(value)}"'
            if isinstance(value, int | float) and not isinstance(value, bool)
            else ""
        )
        cards.append(
            f'<article class="metric reveal"><span>{_escape(label)}</span>'
            f'<strong class="stat-value"{count_attribute}>{_escape(value)}</strong>'
            f"<small>{_escape(note)}</small></article>"
        )
    return "".join(cards)


def _sources(items: list[tuple[str, str]]) -> str:
    rows = "".join(
        f'<li id="cite-{index}"><span class="src-title">{_escape(title)}</span>'
        f'<span class="src-url">{_escape(path)}</span></li>'
        for index, (title, path) in enumerate(items, start=1)
    )
    return f'<footer><div class="sources"><h2>来源与正式资产</h2><ol>{rows}</ol></div></footer>'


def _technical_reference_guide() -> str:
    terms = [
        ("业务类", "Class", "表示一类业务对象，例如供应商、采购订单、物料。"),
        ("对象属性/业务关系", "Object Property", "连接两个业务对象，例如“订单向供应商下单”。"),
        ("数据属性/字段含义", "Data Property", "描述对象自身的值，例如风险等级、数量、交付时间。"),
        ("语义映射", "Mapping", "规定文件资料或数据库结构如何正式翻译成本体。"),
        ("证据", "Evidence", "支撑判断的原文位置、表、字段、SQL、业务材料或人工决策。"),
        ("质量门禁", "Gate", "进入下一阶段前必须通过的检查条件。"),
    ]
    term_rows = "".join(
        '<article><div><strong>'
        f'{_escape(chinese)}</strong><code>{_escape(english)}</code></div>'
        f'<p>{_escape(description)}</p></article>'
        for chinese, english, description in terms
    )
    return (
        '<details class="language-guide reveal"><summary><div class="guide-heading">'
        '<span>需要核对原始资产时再展开</span><h2>必要技术标识说明</h2></div></summary>'
        '<div class="judgement-legend">'
        '<div><b class="dot fact"></b><strong>数据库事实</strong><span>直接来自表、字段、外键或查询结果，不是人工智能猜测。</span></div>'
        '<div><b class="dot fact"></b><strong>资料证据事实</strong><span>直接来自已登记文件和原文定位，不是人工智能猜测。</span></div>'
        '<div><b class="dot ai"></b><strong>人工智能辅助理解</strong><span>根据数据证据提出的业务解释，必须保留证据，不能自动当成事实。</span></div>'
        '<div><b class="dot review"></b><strong>待工程师确认</strong><span>业务影响较大或证据不足，需要本体工程师作出正式建模决定。</span></div>'
        f'</div><div class="term-grid">{term_rows}</div></details>'
    )


def _report_overview(stage: str, status: str, sources: list[tuple[str, str]]) -> str:
    stage_id = stage.split(maxsplit=1)[0]
    stage_summaries = {
        "S0": {
            "purpose": "把 PDF、图片和制度文档整理成可以搜索、复核并回到原文页码的证据。",
            "completed": "登记原始资料，生成结构化文本，检查识别质量，并建立页码与内容校验记录。",
            "next": "资料建模项目留痕跳过 S1 后进入 S2；混合工程进入 S1 数据理解，再共同继续本体建模。",
        },
        "S1": {
            "purpose": "先弄清楚哪些数据真实存在、质量如何、哪些表和字段适合用于本体建模。",
            "completed": "盘点数据源、业务表、字段、主外键、数据量和异常情况，并保存只读查询证据。",
            "next": "把已经确认的数据结构交给 S2，继续识别业务对象、关系、属性和规则候选。",
        },
        "S2": {
            "purpose": "把文件资料或数据库结构翻译成业务人员能够理解的对象、关系、属性和规则候选。",
            "completed": "逐项区分来源证据事实、人工智能辅助理解和待人工确认内容，并保留每项判断的来源。",
            "next": "把候选交给 S3 评审；没有经过确认的推测不会直接进入正式本体。",
        },
        "S3": {
            "purpose": "确认文件资料或数据库结构最终应该对应本体中的哪些业务含义。",
            "completed": "完成映射检查，记录自动决定和人工决定，并形成后续设计唯一采用的正式映射。",
            "next": "正式映射通过后进入 S4，本体施工图不得绕过本阶段临时增加核心语义。",
        },
        "S4": {
            "purpose": "在构建本体文件前先把业务类、关系、属性、约束和验收问题设计清楚。",
            "completed": "把正式映射落实成施工图，并由负责人确认本体必须回答的业务问题。",
            "next": "施工图和业务问题确认后交给 S5，按确定内容构建正式本体资产。",
        },
        "S5": {
            "purpose": "按照已经确认的施工图生成可以被本体工具重新打开和检查的正式文件。",
            "completed": "构建本体、可读定义和数据约束文件，并保存真实工具运行与导出证据。",
            "next": "构建产物交给 S6，继续验证逻辑、约束、实例数据和业务问题。",
        },
        "S6": {
            "purpose": "证明本体不仅文件可打开，而且在真实业务实例上能通过约束并回答验收问题。",
            "completed": "分别检查逻辑一致性、数据约束、映射覆盖、业务问题和图谱运行结果。",
            "next": "所有关键检查通过后才进入 S7；任何一项未通过都需要回到对应阶段修正。",
        },
        "S7": {
            "purpose": "汇总 S0 至 S6 的阶段成果，由负责人决定发布、暂缓或撤回。",
            "completed": "核对阶段状态、人工批准、交付文件和内容校验清单，并记录最终发布决定。",
            "next": "发布后按版本使用；暂缓或撤回时保留原因、原报告和原发布包，便于审计与修订。",
        },
    }
    summary = stage_summaries.get(
        stage_id,
        {
            "purpose": "说明本阶段解决的问题。",
            "completed": "执行并记录本阶段工作。",
            "next": "按阶段结论决定下一步。",
        },
    )
    return (
        '<section class="report-overview reveal"><div class="section-title">'
        '<span>先看这里</span><h2>本阶段总结</h2></div>'
        '<div class="detail-grid">'
        f'<article><span>本阶段解决什么</span><p>{_escape(summary["purpose"])}</p></article>'
        f'<article><span>已经完成什么</span><p>{_escape(summary["completed"])}</p></article>'
        f'<article><span>当前结论</span><p>{_escape(_status_label(status))}</p></article>'
        f'<article><span>接下来怎么用</span><p>{_escape(summary["next"])}</p></article>'
        '</div><div class="overview-sources"><strong>怎样深入核对</strong><p>先阅读本阶段结论和正文；需要核查原始文件、字段或技术标识时，再查看报告末尾的“来源与正式资产”。</p></div></section>'
    )


def _document(
    *,
    stage_dir: Path,
    title: str,
    subtitle: str,
    stage: str,
    status: str,
    metrics: list[tuple[str, Any, str]],
    toc: list[tuple[str, str]],
    sections: str,
    chart_script: str,
    sources: list[tuple[str, str]],
) -> str:
    toc_rows = "".join(
        f'<a href="#{_escape(anchor)}"><span>{index:02d}</span>{_escape(label)}</a>'
        for index, (anchor, label) in enumerate(toc, start=1)
    )
    report_css = _inline_report_css()
    stage_chart_js = _inline_script(stage_dir / f"assets/{chart_script}")
    uses_echarts = "echarts." in stage_chart_js or "echarts.init" in stage_chart_js
    echarts_js = _inline_script(ASSET_ROOT / "js/echarts.min.js") if uses_echarts else ""
    motion_js = _inline_script(ASSET_ROOT / "js/orion-motion.js")
    visible_stage = stage.split("（", 1)[0].strip()
    return f'''<!-- Generated by Trae Work -->
<!-- {REPORT_RENDERER_MARKER} -->
<!-- ORION_REPORT_SELF_CONTAINED: no external runtime assets required -->
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{_escape(title)}</title>
  <style>{report_css}</style>
</head>
<body>
  <header class="cover">
    <div class="cover-inner">
      <div class="eyebrow rise" style="--d:.02s">本体工程阶段总结 · {_escape(visible_stage)}</div>
      <h1 class="rise" style="--d:.10s">{_escape(title)}</h1>
      <p class="rise" style="--d:.18s">{_escape(subtitle)}</p>
      <div class="cover-meta rise" style="--d:.26s"><span class="badge-pulse">✓</span><span>{_badge(_status_label(status), 'ok')}</span><span>生成时间：{_escape(_now())}</span></div>
    </div>
  </header>
  <div class="metric-band grid">{_metric_cards(metrics)}</div>
  <div class="report-layout">
    <aside class="toc reveal"><strong>报告目录</strong><nav>{toc_rows}</nav></aside>
    <main>{_report_overview(stage, status, sources)}{sections}{_technical_reference_guide()}{_sources(sources)}</main>
  </div>
  {f'<script>{echarts_js}</script>' if echarts_js else ''}
  <script>{stage_chart_js}</script>
  <script>{motion_js}</script>
</body>
</html>
'''


def _section(anchor: str, number: int, title: str, body: str, intro: str = "") -> str:
    intro_html = f'<p class="section-intro">{intro}</p>' if intro else ""
    return (
        f'<section id="{_escape(anchor)}" class="reveal"><div class="section-title">'
        f'<span>{number:02d}</span><h2>{_escape(title)}</h2></div>{intro_html}{body}</section>'
    )


def _s1_tables(payload: dict[str, Any]) -> list[dict[str, Any]]:
    schema = payload["schema_snapshot"]
    profile = payload["data_profile"]
    inventory = payload["datasource_inventory"]
    row_counts = {
        str(item.get("table") or item.get("name")): item["row_count"]
        for item in profile.get("tables") or []
        if isinstance(item, dict)
        and (item.get("table") or item.get("name"))
        and item.get("row_count") is not None
    }
    row_counts.update(profile.get("row_counts") or {})
    table_columns = schema.get("table_columns") or {}
    primary_keys = schema.get("primary_key_map") or {}
    declared_tables = schema.get("tables") or []
    declared_map = {
        str(item.get("name") or item.get("table")): item
        for item in declared_tables
        if isinstance(item, dict)
    }
    scope = inventory.get("business_tables_scope") or inventory.get("tables_in_scope") or []
    names = sorted(set(table_columns) | set(row_counts) | set(declared_map) | set(scope))
    relation_counts = Counter(
        str(item.get("source_table"))
        for item in payload["relation_candidates"]
        if item.get("source_table")
    )
    result = []
    for name in names:
        declared = declared_map.get(name, {})
        columns = table_columns.get(name) or declared.get("columns") or []
        primary_key = primary_keys.get(name) or declared.get("primary_key") or []
        if isinstance(primary_key, str):
            primary_key = [primary_key]
        foreign_keys = declared.get("foreign_keys") or []
        result.append(
            {
                "name": name,
                "row_count": int(row_counts.get(name, declared.get("row_count", 0)) or 0),
                "column_count": len(columns),
                "primary_key": primary_key,
                "relation_count": max(len(foreign_keys), relation_counts.get(name, 0)),
                "columns": columns,
            }
        )
    return result


def render_s0_report(
    stage_dir: Path,
    project: dict[str, Any],
    documents: list[dict[str, Any]],
    quality_report: dict[str, Any],
    evidence_index: list[dict[str, Any]],
    processing_trace: dict[str, Any],
    gate_results: dict[str, Any],
) -> str:
    _prepare_text_report(stage_dir, "s0-charts.js")

    def _content_scale(item: dict[str, Any]) -> str:
        source_type = str(item.get("source_type") or "DOCUMENT").upper()
        if source_type in {"EXCEL", "XLS", "XLSX", "CSV"}:
            return f'{item.get("sheet_count") or item.get("content_unit_count") or 0} 个工作表/数据区'
        if item.get("page_count"):
            return f'{item.get("page_count")} 页'
        return f'{item.get("content_unit_count") or 0} 个内容单元'

    document_rows = [
        [
            _code(item.get("document_id")),
            _escape(item.get("source_name")),
            _badge(item.get("source_type") or "DOCUMENT", "accent"),
            _escape(_content_scale(item)),
            _code(item.get("source_sha256")),
            _code(item.get("structured_markdown_path")),
        ]
        for item in documents
    ]
    evidence_rows = [
        [
            _code(item.get("evidence_id")),
            _code(item.get("document_id")),
            _escape(item.get("source_page") or item.get("source_locator")),
            _escape(item.get("markdown_section")),
            _code(item.get("content_sha256") or item.get("excerpt_sha256") or "—"),
        ]
        for item in evidence_index
    ]
    trace_rows = [
        [_escape("运行编号"), _code(processing_trace.get("run_id"))],
        [_escape("执行主体"), _escape(processing_trace.get("provider") or processing_trace.get("tool") or "文档处理工具服务")],
        [_escape("工具调用"), _code("、".join(map(str, processing_trace.get("tool_calls") or [])))],
        [_escape("记录时间"), _escape(processing_trace.get("recorded_at") or "—")],
    ]
    gate_rows = [
        [_code(item.get("id")), _badge(_status_label(item.get("status")), "ok")]
        for item in gate_results.get("gates") or []
    ]
    sections = "".join(
        [
            _section(
                "summary",
                1,
                "资料接入结论",
                '<div class="lead"><p>S0 只负责把 PDF、Word、Excel 等资料整理为可读取、可复核、可追溯的证据，不在本阶段直接生成本体结论。每份结构化文本都保留原始文件内容校验码、原始位置和真实工具运行编号。</p></div>',
            ),
            _section(
                "documents",
                2,
                "资料登记与结构化结果",
                _table(
                    ["资料编号", "原始文件", "类型", "内容规模", "原始内容校验码", "结构化文本"],
                    document_rows,
                ),
            ),
            _section(
                "quality",
                3,
                "识别与复核质量",
                '<div class="detail-grid">'
                f'<article><span>已处理内容单元</span><strong>{_escape(quality_report.get("processed_units") or quality_report.get("processed_pages", 0))}</strong></article>'
                f'<article><span>处理失败</span><strong>{_escape(quality_report.get("failed_units") or quality_report.get("failed_pages", 0))}</strong></article>'
                f'<article><span>低置信度内容</span><strong>{_escape(quality_report.get("low_confidence_units") or quality_report.get("low_confidence_pages", 0))}</strong></article>'
                f'<article><span>已复核低置信度</span><strong>{_escape(quality_report.get("reviewed_low_confidence_units") or quality_report.get("reviewed_low_confidence_pages", 0))}</strong></article>'
                "</div>",
            ),
            _section(
                "evidence",
                4,
                "证据追溯索引",
                _table(
                    ["证据编号", "资料编号", "原始位置", "结构化文本章节", "内容校验码"],
                    evidence_rows,
                ),
            ),
            _section("trace", 5, "工具处理轨迹", _table(["轨迹项", "内容"], trace_rows)),
            _section("gates", 6, "S0 质量门禁", _table(["门禁", "状态"], gate_rows)),
        ]
    )
    return _document(
        stage_dir=stage_dir,
        title=_stage_title(project, "S0", "S0 资料接入与证据整理报告", report=True),
        subtitle=f'{project.get("project_name", "本体项目")} · 原始资料到可审计证据',
        stage=_stage_title(project, "S0", "S0 资料接入与证据整理"),
        status=gate_results.get("status", "PASSED"),
        metrics=[
            ("输入资料", len(documents), "已登记原始哈希"),
            ("处理页面", quality_report.get("page_count", quality_report.get("processed_pages", 0)), "具备页码的资料页面"),
            *([
                ("处理内容单元", quality_report["processed_units"], "解析器记录的内容单元，不等同于页面数"),
            ] if "processed_units" in quality_report else []),
            ("证据条目", len(evidence_index), "原始位置可追溯"),
            (
                "失败内容单元" if "failed_units" in quality_report else "失败页面",
                quality_report.get("failed_units", quality_report.get("failed_pages", 0)),
                "必须为 0",
            ),
        ],
        toc=[
            ("summary", "资料接入结论"),
            ("documents", "资料登记"),
            ("quality", "识别质量"),
            ("evidence", "证据索引"),
            ("trace", "工具处理轨迹"),
            ("gates", "质量门禁"),
        ],
        sections=sections,
        chart_script="s0-charts.js",
        sources=[
            ("资料登记簿", "document-register.json"),
            ("接入质量报告", "ingestion-quality-report.json"),
            ("证据索引", "evidence-index.json"),
            ("工具处理轨迹", "processing-trace.json"),
            ("S0 门禁结果", "gate-results.json"),
        ],
    )


def render_s0_scope_report(
    stage_dir: Path,
    project: dict[str, Any],
    scope_decision: dict[str, Any],
    gate_results: dict[str, Any],
) -> str:
    versioned = project.get("stage_contract_version") == "s0-s7-stage-contract-v2"
    result_status = scope_decision.get("status", "NOT_APPLICABLE")
    _prepare_text_report(stage_dir, "s0-charts.js")
    datasource_refs = scope_decision.get("datasource_refs") or []
    decision_rows = [
        [_escape("接入模式"), _badge(scope_decision.get("intake_mode"), "accent")],
        [_escape("阶段结论"), _badge(_status_label(result_status), "neutral")],
        [_escape("判定人"), _escape(scope_decision.get("decided_by"))],
        [_escape("判定时间"), _escape(scope_decision.get("decided_at"))],
        [_escape("原因"), _escape(scope_decision.get("rationale"))],
    ]
    datasource_rows = [
        [_code(index), _escape(reference)]
        for index, reference in enumerate(datasource_refs, start=1)
    ]
    gate_rows = [
        [_code(item.get("id")), _badge(_status_label(item.get("status")), "ok")]
        for item in gate_results.get("gates") or []
    ]
    sections = "".join(
        [
            _section(
                "summary",
                1,
                "范围判定结论",
                ('<div class="lead"><p>本工程已登记建设目标和数据库来源范围，S0 核心职责已完成；文档处理子任务不适用。后续 S1 将验证真实来源、库表范围和数据质量。</p></div>' if versioned else '<div class="lead"><p>本项目仅接入结构化数据库，不包含需要文字识别、结构化文本或页码证据索引的 PDF、图片和制度文档。因此 S0 被正式判定为不适用，而不是被静默跳过；判定依据、责任人和时间已进入审计链。</p></div>'),
            ),
            _section(
                "decision",
                2,
                "判定记录",
                _table(["判定项", "内容"], decision_rows),
            ),
            _section(
                "datasources",
                3,
                "数据库接入范围",
                _table(["序号", "数据源引用"], datasource_rows)
                if datasource_rows
                else '<p class="empty-state">尚未填写数据源引用；S1 仍需重新发现并确认真实数据源。</p>',
            ),
            _section(
                "gates",
                4,
                "S0 范围门禁",
                _table(["门禁", "状态"], gate_rows),
            ),
        ]
    )
    return _document(
        stage_dir=stage_dir,
        title="S0 目标与来源登记报告" if versioned else "S0 资料接入范围判定报告",
        subtitle=f'{project.get("project_name", "本体项目")} · 数据库直连接入',
        stage="S0 接入范围判定",
        status=result_status,
        metrics=[
            ("接入模式", "仅数据库处理", "已记录范围判定"),
            ("文档资料", 0, "无需文字识别"),
            ("数据源引用", len(datasource_refs), "S1 重新验证"),
            ("判定责任人", scope_decision.get("decided_by"), "已留审计记录"),
        ],
        toc=[
            ("summary", "范围判定"),
            ("decision", "判定记录"),
            ("datasources", "数据源范围"),
            ("gates", "范围门禁"),
        ],
        sections=sections,
        chart_script="s0-charts.js",
        sources=[
            ("S0 范围判定", "scope-decision.json"),
            ("S0 范围门禁", "gate-results.json"),
        ],
    )


def render_s1_scope_report(
    stage_dir: Path,
    project: dict[str, Any],
    scope_decision: dict[str, Any],
    gate_results: dict[str, Any],
) -> str:
    """为仅资料建模项目生成 S1 跳过报告，避免无记录地跨过数据库摸排。"""

    versioned = project.get("stage_contract_version") == "s0-s7-stage-contract-v2"
    result_status = scope_decision.get("status", "NOT_APPLICABLE")
    _prepare_text_report(stage_dir, "s1-charts.js")
    document_refs = scope_decision.get("document_refs") or []
    decision_rows = [
        [_escape("接入模式"), _badge("资料建模", "accent")],
        [_escape("阶段结论"), _badge(_status_label(result_status), "neutral")],
        [_escape("判定人"), _escape(scope_decision.get("decided_by"))],
        [_escape("判定时间"), _escape(scope_decision.get("decided_at"))],
        [_escape("原因"), _escape(scope_decision.get("rationale"))],
    ]
    document_rows = [
        [_code(index), _code(reference)]
        for index, reference in enumerate(document_refs, start=1)
    ]
    gate_rows = [
        [_code(item.get("id")), _badge(_status_label(item.get("status")), "ok")]
        for item in gate_results.get("gates") or []
    ]
    sections = "".join(
        [
            _section(
                "summary",
                1,
                "范围判定结论",
                ('<div class="lead"><p>本工程已基于 S0 验证资料来源、证据定位和解析质量，完成资料理解。数据库分析子任务不适用；后续 S2 从这些真实资料识别业务语义。</p></div>' if versioned else '<div class="lead"><p>本项目以 PDF、Word、Excel 等文件资料作为建模证据，不连接业务数据库，因此无需执行数据库、表、字段和 SQL 画像。S1 被正式标记为不适用并生成本报告；流程不会结束，而是继续进入 S2 业务语义识别。</p></div>'),
            ),
            _section("decision", 2, "判定记录", _table(["判定项", "内容"], decision_rows)),
            _section(
                "documents",
                3,
                "上游资料证据",
                _table(["序号", "资料编号"], document_rows),
                "这些资料已经在 S0 完成原文件校验、结构化文本和证据定位；S2 必须继续保留相同证据引用。",
            ),
            _section("gates", 4, "S1 范围门禁", _table(["门禁", "状态"], gate_rows)),
        ]
    )
    return _document(
        stage_dir=stage_dir,
        title="S1 资料与数据理解报告" if versioned else "S1 数据理解范围判定报告",
        subtitle=f'{project.get("project_name", "本体项目")} · 仅资料建模，跳过数据库摸排',
        stage="S1 数据理解范围判定",
        status=result_status,
        metrics=[
            ("接入方式", "文件资料", "PDF / Word / Excel 等"),
            ("数据库连接", 0, "本项目不适用"),
            ("上游资料", len(document_refs), "S0 已形成证据"),
            ("下一阶段", "S2", "继续本体工程"),
        ],
        toc=[
            ("summary", "范围判定"),
            ("decision", "判定记录"),
            ("documents", "上游资料证据"),
            ("gates", "范围门禁"),
        ],
        sections=sections,
        chart_script="s1-charts.js",
        sources=[
            ("S1 范围判定", "scope-decision.json"),
            ("S1 范围门禁", "gate-results.json"),
            ("S0 资料登记", "../00-document-evidence/document-register.json"),
            ("S0 证据索引", "../00-document-evidence/evidence-index.json"),
        ],
    )


def render_s1_report(
    stage_dir: Path,
    project: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    _ensure_assets(stage_dir)
    inventory = payload["datasource_inventory"]
    profile = payload["data_profile"]
    tables = _s1_tables(payload)
    relations = payload["relation_candidates"]
    evidence = payload["evidence_sql"]
    enum_domains = profile.get("enum_domains") or {}
    observations = profile.get("sample_observations") or {}
    datasource = inventory.get("datasource") or inventory
    total_rows = sum(item["row_count"] for item in tables)
    total_columns = sum(item["column_count"] for item in tables)
    selected_database = (
        inventory.get("selected_database")
        or datasource.get("database")
        or datasource.get("selected")
        or "未标注"
    )
    selected_schema = inventory.get("selected_schema") or datasource.get("schema") or "未标注"
    excluded = inventory.get("excluded_internal_tables") or []

    chart_data = {
        "rowCounts": [
            {"name": item["name"], "value": item["row_count"]}
            for item in sorted(tables, key=lambda item: item["row_count"], reverse=True)[:15]
        ],
        "enumSizes": [
            {"name": name, "value": len(values or [])}
            for name, values in sorted(
                enum_domains.items(), key=lambda item: len(item[1] or []), reverse=True
            )[:12]
        ],
    }
    _write(stage_dir / "assets/s1-charts.js", _chart_script_s1(chart_data))

    source_card = (
        '<div class="detail-grid">'
        f'<article><span>数据源</span><strong>{_escape(datasource.get("alias") or project.get("datasource_label") or "—")}</strong></article>'
        f'<article><span>数据库类型</span><strong>{_escape(datasource.get("db_type") or "—")}</strong></article>'
        f'<article><span>数据库与数据区域</span><strong>{_escape(selected_database)} / {_escape(selected_schema)}</strong></article>'
        f'<article><span>连接方式</span><strong>数据库理解与查询工具（{_escape(datasource.get("via") or "Chat2DB")}）</strong></article>'
        '</div>'
        '<div class="callout"><strong>范围边界</strong><p>'
        f'本次纳入 <mark class="key">{len(tables)} 张业务表</mark>；排除 {len(excluded)} 张工作流内部表。'
        f'{_escape(inventory.get("exclude_reason") or "排除项不属于当前业务域。")}</p></div>'
    )

    table_rows = [
        [
            _table_name(item["name"]),
            _escape(item["row_count"]),
            _escape(item["column_count"]),
            _code(", ".join(map(str, item["primary_key"])) or "—"),
            _escape(item["relation_count"]),
        ]
        for item in sorted(tables, key=lambda item: item["row_count"], reverse=True)
    ]
    column_rows = [
        [
            _table_name(item["name"]),
            _escape(item["column_count"]),
            _code(", ".join(map(str, item["columns"][:12])) + (" …" if len(item["columns"]) > 12 else "")),
        ]
        for item in tables
    ]
    enum_rows = [
        [
            _enum_domain_name(name),
            _escape(len(values or [])),
            " · ".join(_enum_value(value) for value in values or []),
        ]
        for name, values in enum_domains.items()
    ]
    observation_rows = [
        [_code(name), _escape(value)] for name, value in observations.items()
    ]
    relation_rows = [
        [
            _table_name(item.get("source_table") or item.get("from") or "—"),
            _code(item.get("source_column") or "—"),
            _relationship_name(item.get("relationship") or item.get("basis") or "候选"),
            _table_name(item.get("target_table") or item.get("to") or "—"),
            _escape(item.get("cardinality") or "—"),
            (
                _badge("外键连接是数据库事实（DATABASE_FACT）", "ok")
                + " "
                + _badge("业务关系命名属于人工智能辅助理解", "warning")
            )
            if "FK" in str(item.get("evidence") or item.get("basis") or "").upper()
            else _badge("关系候选，需要继续评审", "warning"),
        ]
        for item in relations
    ]
    evidence_rows = [
        [
            _code(item.get("id")),
            _escape(item.get("purpose")),
            _code("、".join(map(str, item.get("source_tables") or [])) or "—"),
            f'<details><summary>查看 SQL</summary><pre>{_escape(item.get("sql") or item.get("query") or "—")}</pre></details>',
        ]
        for item in evidence
    ]

    explicit_quality = (
        profile.get("quality_issues")
        or profile.get("data_quality_issues")
        or profile.get("quality_findings")
        or []
    )
    if explicit_quality:
        quality_body = _table(
            ["问题", "严重度", "证据"],
            [
                [
                    _escape(item.get("issue") or item.get("name") or item),
                    _badge(item.get("severity") or "待评估", "warning") if isinstance(item, dict) else _badge("待评估", "warning"),
                    _escape(item.get("evidence") or "—") if isinstance(item, dict) else "—",
                ]
                for item in explicit_quality
            ],
        )
    else:
        quality_body = (
            '<div class="callout warning"><strong>当前证据边界</strong><p>'
            '本阶段资产尚未提供独立的空值率、唯一性、异常值和跨表一致性检查结果，因此不能写成“数据质量已通过”。'
            '现阶段只能确认数据库结构、记录量、枚举域、样本观察与关系证据已经留档。</p></div>'
        )

    sections = "".join(
        [
            _section(
                "summary",
                1,
                "执行摘要",
                '<div class="lead"><p>本次通过数据库理解与查询工具对企业真实数据库进行了只读摸排，形成可复用的数据库结构、数据画像、关系与查询证据。'
                f'共识别 <mark class="key">{len(tables)} 张业务表、{total_rows} 条记录、{len(relations)} 条关系候选</mark>。'
                '所有结论均可回溯到正式阶段资产，报告不替代底层 JSON 与 SQL。</p></div>'
                '<div class="status-strip"><span>必需资产门禁（G-S1-REQUIRED）</span><span>只读安全门禁（G-S1-READONLY）</span><span>证据完整门禁（G-S1-EVIDENCE）</span></div>'
                '<div class="callout ai-boundary"><strong>本阶段的人工智能参与边界</strong><p>'
                '表、字段、主键、外键、记录数和查询结果属于数据库事实；“哪些表代表业务对象、关系应该叫什么、是否应成为正式业务关系”等属于人工智能辅助理解候选。'
                '这些候选只进入 S2 继续识别，并不会在本阶段自动写入正式本体。</p></div>',
            ),
            _section("source", 2, "项目与数据源", source_card),
            _section(
                "scope",
                3,
                "摸排范围",
                _table(
                    ["范围类型", "数量", "内容"],
                    [
                        ["业务表", _escape(len(tables)), _code("、".join(item["name"] for item in tables))],
                        ["排除表", _escape(len(excluded)), _code("、".join(map(str, excluded)) or "—")],
                        ["证据查询", _escape(len(evidence)), "全部为只读查询（SELECT / WITH），不会修改客户数据"],
                    ],
                ),
            ),
            _section(
                "scale",
                4,
                "数据库结构与数据规模",
                '<div class="chart-grid"><figure class="chart-figure"><figcaption>核心表记录量 Top 15</figcaption><div id="chart-row-counts" class="chart tall"></div></figure>'
                '<figure class="chart-figure"><figcaption>枚举域取值数量 Top 12</figcaption><div id="chart-enum-sizes" class="chart"></div></figure></div>',
                f"当前快照包含 {total_columns} 个字段；图表只展示记录量和枚举复杂度，不代表业务重要性排序。",
            ),
            _section(
                "tables",
                5,
                "核心业务表",
                _table(["表", "记录数", "字段数", "主键", "关系数"], table_rows),
                "中文名称帮助业务人员理解；英文表名是客户数据库中的原始技术标识，为了证据追溯必须保留。",
            ),
            _section(
                "columns",
                6,
                "字段结构与主键",
                _table(["表", "字段数", "字段预览"], column_rows),
                "字段名保留数据库原文，避免翻译后无法回查；字段的正式中文业务含义将在 S2/S3 的语义候选和映射中确认。",
            ),
            _section(
                "domains",
                7,
                "枚举域与样本观察",
                '<h3>枚举域</h3>'
                + _table(["业务域", "取值数", "已观察取值"], enum_rows)
                + '<h3>样本观察</h3>'
                + _table(["主题", "观察结论"], observation_rows),
            ),
            _section("quality", 8, "数据质量与证据边界", quality_body),
            _section(
                "relations",
                9,
                "显式与候选关系",
                _table(["源表", "源字段", "业务关系与原始标识", "目标表", "数量关系", "证据性质"], relation_rows),
                "关系候选是 S2 业务语义识别的输入，不等同于最终本体中的正式业务关系。",
            ),
            _section(
                "evidence",
                10,
                "查询证据台账（SQL）",
                _table(["编号", "验证目的", "来源表", "查询语句（SQL）"], evidence_rows),
                "结构化查询语言（SQL）仅用于解释为什么形成某项判断；后续阶段应继续引用稳定证据编号。",
            ),
            _section(
                "handoff",
                11,
                "S2 输入与下一步",
                '<div class="checklist"><div><b>✓</b><span>数据源与范围已锁定</span></div><div><b>✓</b><span>数据库结构、主键与字段已快照</span></div>'
                '<div><b>✓</b><span>关系候选与只读查询证据已编号</span></div><div><b>→</b><span>下一步区分数据库事实、人工智能辅助理解与待确认语义</span></div></div>',
            ),
        ]
    )
    return _document(
        stage_dir=stage_dir,
        title=_stage_title(project, "S1", "S1 数据理解报告", report=True),
        subtitle=f"{project['project_name']} · 数据库结构、画像、质量边界与关系证据",
        stage=_stage_title(project, "S1", "S1 数据理解"),
        status="PASSED",
        metrics=[
            ("业务表", len(tables), "纳入本体工程范围"),
            ("记录总量", total_rows, "当前快照统计"),
            ("字段总量", total_columns, "数据库结构快照"),
            ("关系候选", len(relations), "显式与候选关系"),
            ("证据查询（SQL）", len(evidence), "只读查询，不修改数据"),
        ],
        toc=[
            ("summary", "执行摘要"),
            ("source", "项目与数据源"),
            ("scope", "摸排范围"),
            ("scale", "数据库结构与数据规模"),
            ("tables", "核心业务表"),
            ("columns", "字段结构与主键"),
            ("domains", "枚举域与样本观察"),
            ("quality", "数据质量与证据边界"),
            ("relations", "显式与候选关系"),
            ("evidence", "查询证据台账（SQL）"),
            ("handoff", "S2 输入与下一步"),
        ],
        sections=sections,
        chart_script="s1-charts.js",
        sources=[
            ("数据源清单", "datasource-inventory.json"),
            ("数据库结构快照", "schema-snapshot.json"),
            ("数据画像", "data-profile.json"),
            ("关系候选", "relation-candidates.json"),
            ("只读查询证据（SQL）", "evidence-sql.json"),
            ("S1 门禁结果", "gate-results.json"),
        ],
    )


def _cq_semantic_review_table(review: Any, *, upstream: bool = False) -> str:
    """Render design-time coverage without implying runtime or business approval."""
    boundary = (
        "以下为 S2 设计时的业务语义检查快照，未重新验证当前 S4 模型、数据或运行结果；后续决策请结合正式设计评审。"
        if upstream else
        "以下检查用于发现 CQ 所需的业务定义、模型表达和数据缺口；关联到来源或形成候选不等于已获业务批准或通过真实查询验收。"
    )
    notice = f'<p>{boundary}</p>'
    if not isinstance(review, dict) or not isinstance(review.get("questions"), list) or not review["questions"]:
        return notice + '<div class="callout warning"><strong>CQ 业务语义尚未评估</strong><p>当前资产没有逐 CQ 检查记录，不能据此判断业务规则完整或查询能力就绪。</p></div>'
    labels = {
        "UNASSESSED": "尚未评估", "NEEDS_DECISION": "待业务决策",
        "SOURCE_LINKED": "已关联来源", "INCOMPLETE": "表达不完整",
        "CANDIDATE": "候选待审", "MISSING": "来源缺失",
        "UNVERIFIED": "尚未验证", "NOT_REQUIRED": "无需实例数据",
        "NOT_VALIDATED": "未验收",
    }
    actions = {
        "COMPLETE_CQ_SEMANTICS": "补全 CQ 业务语义",
        "PREPARE_BUSINESS_DECISION": "准备业务决策",
        "COMPLETE_MODEL_OR_RULE_BINDINGS": "补全模型或规则绑定",
        "WAITING_FOR_SOURCE": "等待真实来源",
        "VALIDATE_SOURCE_AND_QUERY": "验证来源与查询",
    }
    rows = []
    for question in review["questions"]:
        if not isinstance(question, dict):
            continue
        gaps = []
        for key, title in (("missing_semantics", "业务语义缺口"), ("missing_data", "数据缺口"), ("unbound_premises", "未绑定规则前提")):
            values = question.get(key) or []
            if isinstance(values, list) and values:
                gaps.append(f'<p><strong>{title}：</strong>{_escape("；".join(map(str, values)))}</p>')
        states = [
            _escape(labels.get(str(question.get(key)), "尚未评估"))
            for key in ("business_status", "model_status", "data_status", "validation_status")
        ]
        action = str(question.get("next_action") or "补充逐 CQ 检查")
        rows.append([
            _code(question.get("question_id")), _escape(question.get("question")),
            *states, "".join(gaps) or "当前检查未列出缺口，仍须评审与验收",
            _escape(actions.get(action, action)),
        ])
    return notice + _table(["CQ 编号", "业务问题", "业务定义", "模型表达", "数据来源", "实际验收", "缺口", "建议下一步（不代表阶段已推进）"], rows, empty="CQ 业务语义尚未评估")


def render_s2_report(
    stage_dir: Path,
    project: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    _ensure_assets(stage_dir)
    candidates = payload["ontology_candidates"]
    rules = payload["business_rule_candidates"]
    by_kind = Counter(str(item.get("kind") or "UNCLASSIFIED") for item in candidates)
    by_status = Counter(str(item.get("status") or "UNCLASSIFIED") for item in candidates)
    confidences = [
        float(item["confidence"])
        for item in candidates
        if isinstance(item.get("confidence"), int | float)
    ]
    refs = sorted(
        {
            str(ref)
            for item in [*candidates, *rules]
            for ref in item.get("source_refs") or []
        }
    )
    all_items = [*candidates, *rules]
    uncertain = [
        item
        for item in all_items
        if item.get("status") in {"AI_INFERENCE", "NEEDS_HUMAN_CONFIRMATION"}
    ]
    database_fact_count = by_status.get("DATABASE_FACT", 0)
    document_fact_count = by_status.get("DOCUMENT_EVIDENCE", 0)
    ai_candidate_count = by_status.get("AI_INFERENCE", 0)
    review_count = by_status.get("NEEDS_HUMAN_CONFIRMATION", 0)
    ai_rule_count = sum(item.get("status") == "AI_INFERENCE" for item in rules)
    chart_data = {
        "kinds": [{"name": _kind_label(name), "value": value} for name, value in by_kind.items()],
        "statuses": [
            {"name": _status_label(name), "value": value}
            for name, value in by_status.items()
        ],
    }
    _write(stage_dir / "assets/s2-charts.js", _chart_script_s2(chart_data))

    def candidate_rows(kind: str) -> list[list[Any]]:
        return [
            [
                _code(item.get("id")),
                _ontology_name(item.get("name")),
                _badge(_status_label(item.get("status")), _status_tone(item.get("status"))),
                _escape(f"{float(item.get('confidence', 0)):.0%}"),
                _code("、".join(map(str, item.get("source_refs") or []))),
                _escape(item.get("note") or "—"),
            ]
            for item in candidates
            if item.get("kind") == kind
        ]

    class_rows = candidate_rows("CLASS")
    object_rows = candidate_rows("OBJECT_PROPERTY")
    remaining = [
        item
        for item in candidates
        if item.get("kind") not in {"CLASS", "OBJECT_PROPERTY"}
    ]
    remaining_rows = [
        [
            _code(item.get("id")),
            _badge(_kind_label(item.get("kind")), "accent"),
            _ontology_name(item.get("name")),
            _badge(_status_label(item.get("status")), _status_tone(item.get("status"))),
            _code("、".join(map(str, item.get("source_refs") or []))),
        ]
        for item in remaining
    ]
    rule_rows = [
        [
            _code(item.get("id")),
            _escape(item.get("name")),
            _badge(_status_label(item.get("status")), _status_tone(item.get("status"))),
            _escape(f"{float(item.get('confidence', 0)):.0%}"),
            _escape(item.get("note") or "—"),
            _code("、".join(map(str, item.get("source_refs") or []))),
        ]
        for item in rules
    ]
    uncertain_rows = [
        [
            _code(item.get("id")),
            _badge(
                _kind_label("RULE_CANDIDATE" if item in rules else item.get("kind")),
                "accent",
            ),
            _ontology_name(item.get("name"))
            if item not in rules
            else _escape(item.get("name")),
            _badge(_status_label(item.get("status")), _status_tone(item.get("status"))),
            _escape(item.get("note") or "当前资产未提供补充说明"),
        ]
        for item in uncertain
    ]
    trace_rows = [
        [_code(ref), _escape(sum(ref in (item.get("source_refs") or []) for item in all_items))]
        for ref in refs
    ]
    average_confidence = sum(confidences) / len(confidences) if confidences else 0

    document_mode = str(project.get("intake_mode") or "HYBRID") == "DOCUMENT_ONLY"
    evidence_origin = "S0 的文件资料与定位证据" if document_mode else "S1 的数据库结构与查询证据"
    fact_label = "资料证据事实" if document_mode else "数据库事实"
    fact_count = document_fact_count if document_mode else database_fact_count
    sections = "".join(
        [
            _section(
                "summary",
                1,
                "执行摘要",
                f'<div class="lead"><p>S2 将{evidence_origin}翻译为本体候选，并强制区分证据事实与推测。'
                f'当前共形成 <mark class="key">{len(candidates)} 个本体候选和 {len(rules)} 个规则候选</mark>；'
                f'{len(uncertain)} 项仍属于人工智能辅助理解或待确认，不应直接进入正式本体文件。</p></div>'
                '<div class="status-strip"><span>判断状态门禁（G-S2-STATUS）</span><span>证据引用门禁（G-S2-EVIDENCE）</span><span>候选编号唯一门禁（G-S2-UNIQUE）</span></div>'
                '<div class="callout ai-boundary"><strong>本阶段的人工智能参与边界</strong><p>'
                f'当前本体候选中有 <mark class="key">{fact_count} 项{fact_label}、{ai_candidate_count} 项人工智能辅助理解、{review_count} 项待本体工程师确认</mark>；'
                f'另有 {ai_rule_count} 条业务规则由人工智能根据现有证据提出。人工智能只负责提出“可能的业务含义”，不能把推测改写成证据事实，也不能绕过 S3 语义映射评审直接进入正式本体。</p></div>',
            ),
            _section(
                "distribution",
                2,
                "候选总体分布",
                '<div class="chart-grid"><figure class="chart-figure"><figcaption>本体候选类型构成</figcaption><div id="chart-kinds" class="chart"></div></figure>'
                '<figure class="chart-figure"><figcaption>候选证据状态构成</figcaption><div id="chart-statuses" class="chart"></div></figure></div>',
            ),
            _section(
                "confidence",
                3,
                "事实等级与置信度",
                '<div class="detail-grid">'
                f'<article><span>{fact_label}</span><strong>{fact_count}</strong></article>'
                f'<article><span>人工智能辅助理解</span><strong>{ai_candidate_count}</strong></article>'
                f'<article><span>待工程师确认（NEEDS_HUMAN_CONFIRMATION）</span><strong>{review_count}</strong></article>'
                f'<article><span>平均判断把握度</span><strong>{average_confidence:.0%}</strong></article></div>'
                '<div class="callout"><strong>使用规则</strong><p>判断把握度只表示人工智能对当前解释的把握程度，用于排序和评审；它不能把推测自动变成证据事实。高影响不确定项必须在 S3 形成正式建模决策。</p></div>',
            ),
            _section(
                "classes",
                4,
                f"业务类候选（共 {len(class_rows)} 项）",
                _table(["编号", "业务含义与原始标识", "判断性质", "判断把握度", "证据引用", "判断说明"], class_rows),
            ),
            _section(
                "objects",
                5,
                f"业务关系候选（共 {len(object_rows)} 项）",
                _table(["编号", "业务含义与原始标识", "判断性质", "判断把握度", "证据引用", "判断说明"], object_rows),
                "这些关系只是语义候选；适用对象、目标对象与正式唯一标识要在 S3/S4 确认。",
            ),
            _section(
                "others",
                6,
                "数据属性、枚举和其他候选",
                _table(["编号", "候选类型", "业务含义与原始标识", "判断性质", "证据引用"], remaining_rows),
            ),
            _section(
                "rules",
                7,
                f"业务规则候选（{len(rule_rows)}）",
                _table(["编号", "规则", "判断性质", "判断把握度", "辅助判断说明", "证据引用"], rule_rows),
                "规则候选是人工智能根据现有证据提出的业务理解，不能直接等同于可执行推理规则；缺少阈值、口径或业务依据时必须保留为候选。",
            ),
            _section(
                "uncertain",
                8,
                f"高影响不确定项（{len(uncertain_rows)}）",
                _table(["编号", "候选类型", "业务含义与原始标识", "判断性质", "为什么仍不确定"], uncertain_rows),
            ),
            _section(
                "traceability",
                9,
                "证据可追溯性（Evidence Traceability）",
                _table(["证据引用", "被候选引用次数"], trace_rows),
                f"当前共有 {len(refs)} 个唯一证据引用。它记录每项判断来自哪份资料、哪个原文位置，或哪张表、哪个字段和哪条查询证据；S3 正式语义映射必须继续保留这些引用。",
            ),
            _section("cq-semantic-review", 10, "CQ 驱动业务语义补全", _cq_semantic_review_table(payload.get("cq_semantic_review"))),
            _section(
                "handoff",
                11,
                "S3 语义映射评审就绪度",
                '<div class="checklist"><div><b>✓</b><span>候选编号（ID）唯一且稳定</span></div><div><b>✓</b><span>每个候选都有证据引用（source_refs）</span></div>'
                f'<div><b>✓</b><span>{fact_label}与人工智能辅助理解已分级</span></div><div><b>→</b><span>下一步确认来源证据如何正式翻译为本体语义</span></div></div>',
            ),
        ]
    )
    return _document(
        stage_dir=stage_dir,
        title=_stage_title(project, "S2", "S2 业务语义识别报告", report=True),
        subtitle=f"{project['project_name']} · 从来源证据到可评审的本体候选",
        stage=_stage_title(project, "S2", "S2 业务语义识别"),
        status="PASSED",
        metrics=[
            ("本体候选", len(candidates), "业务类 / 属性 / 枚举"),
            ("业务类", by_kind.get("CLASS", 0), "业务对象候选"),
            ("业务关系", by_kind.get("OBJECT_PROPERTY", 0), "业务关系候选"),
            ("规则候选", len(rules), "辅助理解，尚未执行"),
            ("不确定项", len(uncertain), "辅助理解或待确认"),
        ],
        toc=[
            ("summary", "执行摘要"),
            ("distribution", "候选总体分布"),
            ("confidence", "事实等级与置信度"),
            ("classes", "业务类候选"),
            ("objects", "业务关系候选"),
            ("others", "其他候选"),
            ("rules", "业务规则候选"),
            ("uncertain", "高影响不确定项"),
            ("traceability", "证据可追溯性（Evidence Traceability）"),
            ("cq-semantic-review", "CQ 驱动业务语义补全"),
            ("handoff", "S3 语义映射评审就绪度"),
        ],
        sections=sections,
        chart_script="s2-charts.js",
        sources=([
            ("S0 资料登记", "../00-document-evidence/document-register.json"),
            ("S0 证据索引", "../00-document-evidence/evidence-index.json"),
        ] if document_mode else [
            ("S1 数据库结构快照", "../01-data-understanding/schema-snapshot.json"),
            ("S1 数据画像", "../01-data-understanding/data-profile.json"),
            ("S1 关系候选", "../01-data-understanding/relation-candidates.json"),
        ]) + [
            ("本体候选正式资产", "ontology-candidates.yaml"),
            ("业务规则候选", "business-rule-candidates.json"),
            ("CQ 能力计划与语义检查", "capability-plan.json"),
            ("S2 门禁结果", "gate-results.json"),
        ],
    )


def _prepare_text_report(stage_dir: Path, script_name: str) -> None:
    """准备与 S1/S2 相同的自包含报告资源；纯文本报告不初始化图表。"""

    _ensure_assets(stage_dir)
    _write(stage_dir / f"assets/{script_name}", "/* 此阶段没有必须渲染的图表。 */\n")


def render_s3_report(
    stage_dir: Path,
    project: dict[str, Any],
    mapping: dict[str, Any],
    confirmations: list[dict[str, Any]],
    automatic_decisions: list[dict[str, Any]],
    gate_results: dict[str, Any],
) -> str:
    _prepare_text_report(stage_dir, "s3-charts.js")
    versioned = project_stage_contract_version(project) == STAGE_CONTRACT_VERSION
    mapping_label = "已审候选映射" if versioned else "正式映射"
    mappings = mapping.get("mappings") or []
    by_type = Counter(str(item.get("mapping_type") or "UNCLASSIFIED") for item in mappings)
    mapping_rows = [
        [
            _code(item.get("id")),
            _badge({
                "TABLE_TO_CLASS": "表 → 业务类",
                "SQL_TO_CLASS": "受控快照关联/过滤 → 业务类",
                "RULE_TO_CLASS": "已审规则结论 → 业务类",
                "COLUMN_VALUE_TO_CLASS": "字段值集合 → 业务类",
                "FK_TO_OBJECT_PROPERTY": "外键 → 业务关系",
                "FOREIGN_KEY_TO_OBJECT_PROPERTY": "外键 → 业务关系",
                "CANDIDATE_JOIN_TO_OBJECT_PROPERTY": "候选关联 → 业务关系",
                "COLUMN_VALUE_TO_OBJECT_PROPERTY": "字段值关联 → 业务关系",
                "SQL_TO_OBJECT_PROPERTY": "只读 SQL 投影 → 业务关系",
                "COLUMN_TO_DATA_PROPERTY": "字段 → 数据属性",
                "SQL_TO_DATA_PROPERTY": "只读 SQL 投影 → 数据属性",
                "EVIDENCE_TO_CLASS": "资料概念 → 业务类",
                "EVIDENCE_TO_OBJECT_PROPERTY": "资料关系 → 业务关系",
                "EVIDENCE_TO_DATA_PROPERTY": "资料属性 → 数据属性",
            }.get(str(item.get("mapping_type")), str(item.get("mapping_type") or "未分类")), "accent"),
            _code(item.get("source")),
            _ontology_name(item.get("target")),
            _code("、".join(map(str, item.get("source_refs") or []))),
        ]
        for item in mappings
    ]
    decision_rows = [
        [
            _code(item.get("id")),
            _escape(item.get("title") or item.get("topic")),
            _escape(item.get("decision") or item.get("rationale") or "—"),
            _badge("人工建模决定" if item in confirmations else "低风险自动决定", "warning" if item in confirmations else "ok"),
            _code("、".join(map(str, item.get("affected_mapping_ids") or item.get("source_refs") or []))),
        ]
        for item in [*confirmations, *automatic_decisions]
    ]
    sections = "".join([
        _section("summary", 1, "评审结论",
            '<div class="lead"><p>本阶段把来源证据正式翻译成本体术语，并完成高影响语义决策。'
            f'共形成 <mark class="key">{len(mappings)} 条{mapping_label}</mark>；其中人工决定 {len(confirmations)} 项，低风险自动决定 {len(automatic_decisions)} 项。</p></div>'
            '<div class="callout ai-boundary"><strong>人工智能参与边界</strong><p>人工智能可以提出映射草案和推荐方案；正式映射文件只保存经过门禁与决策记录约束后的结果。来源证据、辅助理解和人工决定在报告中分别保留。</p></div>'),
        _section("distribution", 2, "映射构成",
            '<div class="detail-grid">' + ''.join(
                f'<article><span>{_escape(key)}</span><strong>{value}</strong></article>'
                for key, value in by_type.items()
            ) + '</div>'),
        _section("mappings", 3, "正式语义映射", _table(
            ["编号", "映射类型与中文解释", "证据来源", "本体目标", "证据引用"], mapping_rows
        )),
        _section("decisions", 4, "建模决策记录", _table(
            ["编号", "议题", "最终决定", "决定性质", "影响范围"], decision_rows,
            "没有需要单独记录的建模决定。",
        )),
        _section("gates", 5, "质量门禁",
            _table(["门禁", "状态"], [[_code(item.get("id")), _badge(item.get("status"), "ok")] for item in gate_results.get("gates") or []])),
    ])
    if versioned:
        sections = sections.replace("正式语义映射", "已审候选映射").replace("正式映射文件", "候选映射文件")
        sections += _section("baseline", 6, "设计冻结边界", "<p>本阶段确认语义和映射可行性；本体、映射、规则与验收预期将在 S4 联合评审批准后一起冻结。</p>")
    return _document(
        stage_dir=stage_dir, title=_stage_title(project, "S3", "S3 语义映射评审报告", report=True),
        subtitle=f'{project.get("project_name", "本体项目")} · 从来源证据到正式本体语义的关键评审',
        stage=_stage_title(project, "S3", "S3 映射评审"), status=gate_results.get("status", "PASSED"),
        metrics=[
            (mapping_label, len(mappings), "映射评审文件"),
            (
                "业务类映射",
                sum(count for kind, count in by_type.items() if kind.endswith("_TO_CLASS")),
                "资料概念、数据表、字段值或只读投影到业务类",
            ),
            (
                "关系映射",
                sum(count for kind, count in by_type.items() if kind.endswith("_TO_OBJECT_PROPERTY")),
                "来源关系到业务关系",
            ),
            (
                "属性映射",
                sum(count for kind, count in by_type.items() if kind.endswith("_TO_DATA_PROPERTY")),
                "来源属性到数据属性",
            ),
        ],
        toc=[("summary", "评审结论"), ("distribution", "映射构成"), ("mappings", "正式语义映射"), ("decisions", "建模决策记录"), ("gates", "质量门禁")],
        sections=sections, chart_script="s3-charts.js",
        sources=[("正式语义映射", "mapping.yaml"), ("人工决策记录", "decisions.jsonl"), ("自动决定", "automatic-decisions.json"), ("S3 门禁结果", "gate-results.json")],
    )


def render_s4_report(stage_dir: Path, project: dict[str, Any], design: dict[str, Any], gate_results: dict[str, Any]) -> str:
    _prepare_text_report(stage_dir, "s4-charts.js")
    try:
        capability_plan = json.loads((stage_dir.parent / "02-semantic-recognition/capability-plan.json").read_text(encoding="utf-8"))
        semantic_review = capability_plan.get("semantic_review") if isinstance(capability_plan, dict) else None
    except (OSError, ValueError):
        semantic_review = None
    groups = [
        ("业务类", "classes", design.get("classes") or []),
        ("业务关系", "object_properties", design.get("object_properties") or []),
        ("数据属性", "data_properties", design.get("data_properties") or []),
    ]
    entity_rows = []
    for label, _, entities in groups:
        for item in entities:
            entity_rows.append([_badge(label, "accent"), _ontology_name(item.get("name")), _code(item.get("iri")), _code("、".join(map(str, item.get("source_mapping_ids") or []))), _escape(f'{item.get("domain", "—")} → {item.get("range", "—")}')])
    cq_rows = [[_code(q.get("id")), _escape(q.get("question")), _escape(q.get("expected")), f'<details><summary>查看技术查询语句</summary><pre>{_escape(q.get("sparql"))}</pre></details>'] for q in design.get("competency_questions") or []]
    sections = "".join([
        _section("summary", 1, "施工图结论", '<div class="lead"><p>本体施工图是在正式构建前冻结的确定性设计。所有实体都必须追溯到 S3 的正式映射，模型不得在 S5 临场新增核心语义。</p></div>'),
        _section("entities", 2, "本体实体设计", _table(["类型", "业务含义与原始标识", "正式唯一标识", "来源映射", "适用对象 → 取值范围"], entity_rows)),
        _section("cq", 3, "业务验收问题", _table(["编号", "业务问题", "怎样算回答正确", "技术查询语句"], cq_rows)),
        _section("gates", 4, "设计门禁", _table(["门禁", "状态"], [[_code(x.get("id")), _badge(x.get("status"), "ok")] for x in gate_results.get("gates") or []])),
    ])
    axiom_policy = design.get("logical_axiom_applicability")
    if isinstance(axiom_policy, dict):
        title = (
            "本次事实查询无需新增公理"
            if axiom_policy.get("status") == "NOT_APPLICABLE"
            and axiom_policy.get("policy_version") == "structured-reviewed-fact-axioms-v1"
            else "已审规则推理无需新增 OWL 公理"
            if axiom_policy.get("status") == "NOT_APPLICABLE"
            else "需验证已定义公理"
        )
        sections += _section("axiom-applicability", 5, "逻辑公理适用性", f'<p><strong>{_escape(title)}</strong></p><p>{_escape(axiom_policy.get("reason"))}</p><p>公理数量：{_escape(axiom_policy.get("axiom_count"))}；已审 CQ：{_escape("、".join(axiom_policy.get("verified_question_ids") or []))}。判定与来源指纹已纳入本次设计批准，S5 重新核对。</p>')
    sections += _section("cq-semantic-review", 5, "CQ 业务语义检查（S2 快照）", _cq_semantic_review_table(semantic_review, upstream=True))
    if project_stage_contract_version(project) == STAGE_CONTRACT_VERSION:
        sections += _section("joint-design", 6, "联合设计基线", '<p>本阶段一并批准本体、映射、规则、来源范围及验收预期。后续构建和验证必须核对同一基线。</p><p><a href="joint-design-baseline.json">查看批准基线</a> · <a href="competency-question-review.json">查看完整设计评审</a></p>')
    return _document(stage_dir=stage_dir, title=_stage_title(project, "S4", "S4 本体设计报告", report=True), subtitle=f'{project.get("project_name", "本体项目")} · 正式构建前的本体施工图', stage=_stage_title(project, "S4", "S4 本体设计"), status=gate_results.get("status", "PASSED"), metrics=[("业务类", len(design.get("classes") or []), "业务对象类型"), ("业务关系", len(design.get("object_properties") or []), "对象之间的关系"), ("数据属性", len(design.get("data_properties") or []), "对象自身的字段含义"), ("业务问题", len(design.get("competency_questions") or []), "已通过门禁的验收问题")], toc=[("summary", "施工图结论"), ("entities", "本体实体设计"), ("cq", "业务问题"), ("gates", "设计门禁"), ("cq-semantic-review", "CQ 业务语义检查（S2 快照）")], sections=sections, chart_script="s4-charts.js", sources=[("本体施工图", "ontology-design.yaml"), ("业务问题评审", "competency-question-review.json"), ("S4 门禁结果", "gate-results.json"), ("上游正式映射", "../03-mapping-review/mapping.yaml"), ("S2 CQ 能力计划与语义检查", "../02-semantic-recognition/capability-plan.json")])


def render_s5_report(stage_dir: Path, project: dict[str, Any], build_report: dict[str, Any], gate_results: dict[str, Any]) -> str:
    _prepare_text_report(stage_dir, "s5-charts.js")
    metrics = build_report.get("verified_metrics") or gate_results.get("metrics") or {}
    evidence_rows = [[_escape("构建主体"), _badge("专业本体编辑器工具服务", "ok")], [_escape("工具运行编号"), _code(build_report.get("run_id") or build_report.get("trace_id") or "未提供")], [_escape("导出格式"), _code("、".join(map(str, build_report.get("exported_formats") or [])))], [_escape("记录时间"), _escape(build_report.get("recorded_at") or build_report.get("completed_at") or "—")]]
    sections = "".join([
        _section("summary", 1, "构建结论", '<div class="lead"><p>本阶段按 S4 施工图由专业本体编辑器构建并导出正式本体文件和数据约束文件。系统随后重新解析产物并核对全部设计标识，避免只凭模型文字声称“已构建”。</p></div>'),
        _section("evidence", 2, "本体编辑器构建证据", _table(["证据项", "内容"], evidence_rows)),
        _section("assets", 3, "正式本体资产", _table(["资产", "用途"], [[_code("ontology.owl"), _escape("标准本体交换文件")], [_code("ontology.ttl"), _escape("便于阅读和查询的本体定义")], [_code("shapes.ttl"), _escape("真实实例数据约束")]])),
        _section("gates", 4, "构建门禁", _table(["门禁", "状态"], [[_code(x.get("id")), _badge(_status_label(x.get("status")), "ok")] for x in gate_results.get("gates") or []])),
    ])
    if project_stage_contract_version(project) == STAGE_CONTRACT_VERSION:
        sections += _section("rules", 5, "业务规则与执行器", '<p><a href="rule-review/业务规则目录.md">查看业务规则目录</a> · <a href="rule-review/规则执行与Protégé复核说明.md">查看执行与复核说明</a></p><p>目录按正式合同声明实际执行器和验证范围；SWRL 执行能力需要单独实现和验证。</p>')
    return _document(stage_dir=stage_dir, title=_stage_title(project, "S5", "S5 本体构建报告", report=True), subtitle=f'{project.get("project_name", "本体项目")} · 按施工图确定性构建与导出', stage=_stage_title(project, "S5", "S5 本体构建"), status=gate_results.get("status", "PASSED"), metrics=[("可读本体三元组", metrics.get("ttl_triple_count", 0), "本体定义文件"), ("交换文件三元组", metrics.get("owl_triple_count", 0), "标准交换文件"), ("业务类", metrics.get("class_count", 0), "已构建业务对象类型"), ("约束形状", metrics.get("node_shape_count", 0), "实例数据约束")], toc=[("summary", "构建结论"), ("evidence", "本体编辑器构建证据"), ("assets", "正式本体资产"), ("gates", "构建门禁")], sections=sections, chart_script="s5-charts.js", sources=[("本体编辑器构建报告", "protege-build-report.json"), ("标准本体文件", "ontology.owl"), ("可读本体文件", "ontology.ttl"), ("数据约束文件", "shapes.ttl"), ("S5 门禁结果", "gate-results.json")])


def render_s6_report(stage_dir: Path, project: dict[str, Any], reports: dict[str, dict[str, Any]], quality: dict[str, Any], gate_results: dict[str, Any]) -> str:
    _prepare_text_report(stage_dir, "s6-charts.js")
    execution = quality.get("validation_execution") or {}
    execution_note = (
        "服务端已在预检中真实执行验证；正式提交复用同一密封回执，并重新核对输入、验证器和候选身份。"
        if execution.get("execution_reused") else "正式提交时由服务端真实执行验证。"
    )
    labels = {"hermit_report": "逻辑推理一致性", "mapping_report": "映射覆盖", "semantic_quality_report": "语义质量", "competency_question_report": "业务验收问题", "semantica_report": "图谱真实实例与规则运行", "shacl": "数据约束"}
    rows = []
    for key, label in labels.items():
        report = reports.get(key, {}) if key != "shacl" else {"status": "PASSED" if quality.get("shacl_conforms") else "FAILED", "summary": "服务端已对真实实例数据重新执行约束检查"}
        rows.append([_escape(label), _badge(_status_label(report.get("status", "UNKNOWN")), "ok" if report.get("status") == "PASSED" else "warning"), _escape(report.get("summary_zh") or report.get("summary") or report.get("conclusion") or "详见正式验证资产"), _code(report.get("run_id") or report.get("trace_id") or "—")])
    sections = "".join([
        _section("summary", 1, "综合质量结论", '<div class="lead"><p>本阶段同时检查“定义是否一致、约束是否满足、映射是否完整、业务问题是否能回答、真实实例与规则是否能运行”。只有六类验证全部通过才能进入发布评审。</p><p>' + _escape(execution_note) + '</p></div>'),
        _section("matrix", 2, "多维验证矩阵", _table(["验证维度", "结论", "中文说明", "工具运行编号"], rows)),
        _section("runtime", 3, "真实实例运行验证", '<div class="detail-grid">' f'<article><span>实例化三元组</span><strong>{_escape(quality.get("materialized_triple_count", 0))}</strong></article>' f'<article><span>真实业务实例</span><strong>{_escape(quality.get("semantica_instance_count", 0))}</strong></article>' f'<article><span>业务问题</span><strong>{_escape(quality.get("competency_question_total", 0))}</strong></article>' f'<article><span>数据约束结论</span><strong>{"通过" if quality.get("shacl_conforms") else "未通过"}</strong></article></div>'),
        _section("gates", 4, "质量门禁", _table(["门禁", "状态"], [[_code(x.get("id")), _badge(_status_label(x.get("status")), "ok")] for x in gate_results.get("gates") or []])),
    ])
    return _document(stage_dir=stage_dir, title=_stage_title(project, "S6", "S6 质量与运行验证报告", report=True), subtitle=f'{project.get("project_name", "本体项目")} · 逻辑、约束、映射、业务问答与图谱运行回读', stage=_stage_title(project, "S6", "S6 质量验证"), status=gate_results.get("status", "PASSED"), metrics=[("综合状态", _status_label(quality.get("status", "UNKNOWN")), "全部关键检查"), ("实例化三元组", quality.get("materialized_triple_count", 0), "真实实例图"), ("业务问题", quality.get("competency_question_total", 0), "全部通过"), ("真实业务实例", quality.get("semantica_instance_count", 0), "已完成图谱回读")], toc=[("summary", "综合质量结论"), ("matrix", "多维验证矩阵"), ("runtime", "真实实例运行验证"), ("gates", "质量门禁")], sections=sections, chart_script="s6-charts.js", sources=[("逻辑推理报告", "hermit-report.json"), ("正式映射验证报告", "mapping-report.json"), ("语义质量报告", "semantic-quality-report.json"), ("业务问题验证报告", "competency-question-report.json"), ("图谱回读报告", "semantica-report.json"), ("数据约束报告", "shacl-report.ttl"), ("S6 门禁结果", "gate-results.json")])


def render_s7_report(
    stage_dir: Path,
    project: dict[str, Any],
    publication: dict[str, Any] | None,
    package_manifest: dict[str, Any] | None,
    stage_statuses: dict[str, str],
    release_control: dict[str, Any] | None = None,
) -> str:
    """生成 S7 全流程汇总；发布、暂缓、恢复和撤回都形成独立可读报告。"""

    _prepare_text_report(stage_dir, "s7-charts.js")
    publication = publication or {}
    package_manifest = package_manifest or {}
    release_control = release_control or {}
    stage_catalog = {
        "S0": (
            "资料接入与证据整理",
            "整理原始资料、结构化文本、页码证据和识别质量。",
            "00-document-evidence/document-evidence-report.html",
        ),
        "S1": (
            "数据理解",
            "确认数据源、业务表、字段、关系、数据量与质量边界。",
            "01-data-understanding/data-understanding-report.html",
        ),
        "S2": (
            "业务语义识别",
            "形成业务对象、关系、属性和规则候选，并区分事实与辅助理解。",
            "02-semantic-recognition/business-semantics-report.html",
        ),
        "S3": (
            "映射评审",
            "完成高影响问题确认，冻结来源证据到本体的正式映射。",
            "03-mapping-review/mapping-review-report.html",
        ),
        "S4": (
            "本体设计",
            "形成本体施工图，并确认必须回答的业务验收问题。",
            "04-ontology-design/ontology-design-report.html",
        ),
        "S5": (
            "本体构建",
            "按施工图生成正式本体与数据约束文件，保留真实构建证据。",
            "05-ontology-build/ontology-build-report.html",
        ),
        "S6": (
            "质量与运行验证",
            "验证逻辑、约束、映射、真实实例和业务问题均可运行。",
            "06-quality-validation/quality-validation-report.html",
        ),
        "S7": (
            "评审与发布",
            "汇总全部阶段，由负责人决定发布、暂缓、恢复或撤回。",
            "07-release/release-report.html",
        ),
    }
    if project_stage_contract_version(project) == STAGE_CONTRACT_VERSION:
        stage_catalog = {
            stage: (stage_contract(stage)["name"], stage_contract(stage)["purpose"], entry[2])
            for stage, entry in stage_catalog.items()
        }
    elif str(project.get("intake_mode") or "HYBRID") == "DOCUMENT_ONLY":
        stage_catalog["S1"] = (
            "数据理解",
            "本项目不连接数据库；已生成范围判定报告并留痕跳过，随后进入 S2。",
            "01-data-understanding/data-understanding-report.html",
        )
    stage_rows = []
    for stage, (name, outcome, _report_path) in stage_catalog.items():
        stage_status = stage_statuses.get(stage, "PENDING")
        stage_rows.append(
            [
                _escape(f"{stage} {name}"),
                _escape(outcome),
                _badge(
                    _status_label(stage_status),
                    "ok" if stage_status in {"PASSED", "NOT_APPLICABLE"} else "warning",
                ),
                _escape(f"{stage} 阶段总结"),
            ]
        )

    control_status = str(
        release_control.get("status")
        or release_control.get("decision")
        or ("PASSED" if publication else stage_statuses.get("S7", "RUNNING"))
    )
    if control_status == "RESUMED":
        control_status = "RUNNING"
    decision_labels = {
        "PASSED": "批准发布",
        "DEFERRED": "暂不发布",
        "RUNNING": "恢复发布评审",
        "REVOKED": "撤回已发布版本",
    }
    decision_label = release_control.get("decision_label") or decision_labels.get(
        control_status, "等待发布决定"
    )
    actor = (
        publication.get("approved_by")
        or release_control.get("decided_by")
        or release_control.get("resumed_by")
        or release_control.get("revoked_by")
        or "尚未记录"
    )
    # The audit actor can include a full approval explanation; keep it in the decision table.
    actor_label = str(actor).split("（", 1)[0].split("(", 1)[0].strip() or "已记录"
    if len(actor_label) > 24:
        actor_label = actor_label[:23] + "…"
    decided_at = (
        publication.get("published_at")
        or release_control.get("decided_at")
        or release_control.get("resumed_at")
        or release_control.get("revoked_at")
        or "—"
    )
    reason = release_control.get("reason") or {
        "PASSED": "S0 至 S6 的质量证据与正式产物已经复核，负责人明确批准生成本次交付包。",
        "DEFERRED": "本次发布暂缓，具体原因已保存在正式发布决定记录中。",
        "RUNNING": "前序问题已经重新核对，现恢复发布评审，但仍需负责人再次明确批准。",
        "REVOKED": "该版本不再推荐继续使用，撤回原因已保存在正式发布控制记录中。",
    }.get(control_status, "本次决定及原始说明已保存在正式审计记录中。")
    release_version = publication.get("release_version") or release_control.get(
        "release_version"
    )
    conclusion = {
        "PASSED": "负责人已明确批准发布，并生成带内容校验清单的正式交付包。各阶段详细报告继续保留在工程审计区。",
        "DEFERRED": "负责人已明确选择暂不发布。系统没有生成发布包，并保留暂缓原因，后续可恢复评审或重新核对前序工作。",
        "RUNNING": "发布评审已经恢复，但尚未形成新的发布批准。只有负责人再次明确确认后，系统才会生成正式交付包。",
        "REVOKED": "已发布版本已被标记为不再推荐使用。原发布包和原发布报告不会删除，可据此新建修订并重新验证。",
    }.get(control_status, "发布状态已更新，当前决定与依据已进入审计记录。")

    file_rows = [
        [_code(item.get("path")), _code(item.get("sha256"))]
        for item in package_manifest.get("files") or []
    ]
    manifest_body = _table(
        ["交付文件", "内容校验码"],
        file_rows,
        "本次操作没有生成新的发布包。",
    )
    sections = "".join(
        [
            _section(
                "summary",
                1,
                "全流程结论",
                f'<div class="lead"><p>{_escape(conclusion)}</p></div>',
            ),
            _section(
                "decision",
                2,
                "本次发布决定",
                _table(
                    ["决定项", "内容"],
                    [
                        [_escape("当前决定"), _badge(decision_label, "accent")],
                        [_escape("负责人"), _escape(actor)],
                        [_escape("发布版本"), _code(release_version or "尚未生成")],
                        [_escape("决定说明"), _escape(reason)],
                        [_escape("记录时间"), _escape(decided_at)],
                    ],
                ),
            ),
            _section(
                "stages",
                3,
                "S0 至 S7 阶段成果汇总",
                _table(
                    ["工作节点", "本阶段形成的主要成果", "当前状态", "详细报告"],
                    stage_rows,
                ),
                "这里汇总每个阶段做了什么；需要查看细节时，再打开对应阶段总结报告。",
            ),
            _section("manifest", 4, "交付包完整性清单", manifest_body),
        ]
    )
    sources = [
        ("本次发布决定", release_control.get("source_path") or "publication.json"),
        ("项目全部阶段状态", "../workflow-state.json"),
    ]
    if package_manifest.get("files"):
        sources.append(("交付包清单", "manifest.json"))
    return _document(
        stage_dir=stage_dir,
        title="S7 评审与发布总结报告",
        subtitle=f'{project.get("project_name", "本体项目")} · {decision_label}',
        stage="S7 评审与发布",
        status=control_status,
        metrics=[
            ("当前决定", decision_label, "负责人明确操作"),
            ("阶段完成", sum(x in {"PASSED", "NOT_APPLICABLE"} for x in stage_statuses.values()), "S0 至 S7"),
            ("交付文件", package_manifest.get("file_count", 0), "只统计正式交付包"),
            ("负责人", actor_label, "完整记录见下方发布决定"),
        ],
        toc=[
            ("summary", "全流程结论"),
            ("decision", "本次发布决定"),
            ("stages", "阶段成果汇总"),
            ("manifest", "交付包清单"),
        ],
        sections=sections,
        chart_script="s7-charts.js",
        sources=sources,
    )


def render_change_report(
    revision_dir: Path,
    project: dict[str, Any],
    revision: dict[str, Any],
    diff_payload: dict[str, Any],
    before_state: dict[str, Any],
    after_state: dict[str, Any],
) -> str:
    _prepare_text_report(revision_dir, "revision-charts.js")
    change_rows = [
        [
            _badge({
                "ADDED": "新增",
                "MODIFIED": "修改",
                "REMOVED": "移除",
            }.get(str(item.get("change_type")), "其他变化"), {
                "ADDED": "ok",
                "MODIFIED": "warning",
                "REMOVED": "danger",
            }.get(str(item.get("change_type")), "neutral")),
            _code(item.get("path")),
            _code(item.get("before_sha256") or "—"),
            _code(item.get("after_sha256") or "—"),
            _escape(item.get("note") or "—"),
        ]
        for item in diff_payload.get("changes") or []
    ]
    stage_rows = []
    before_statuses = before_state.get("stage_statuses") or {}
    after_statuses = after_state.get("stage_statuses") or {}
    for stage in ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7"):
        stage_rows.append(
            [
                _code(stage),
                _badge(_status_label(before_statuses.get(stage, "NOT_APPLICABLE"))),
                _badge(_status_label(after_statuses.get(stage, "PENDING"))),
            ]
        )
    excerpts = [
        item for item in diff_payload.get("changes") or [] if item.get("diff_excerpt")
    ]
    excerpt_html = "".join(
        f'<details><summary>{_escape(item.get("path"))}</summary><pre>{_escape(item.get("diff_excerpt"))}</pre></details>'
        for item in excerpts
    ) or '<div class="empty">没有适合逐行展示的文本差异；二进制文件或网页报告仍通过调整前后的内容校验码标记变化。</div>'
    summary = diff_payload.get("summary") or {}
    sections = "".join(
        [
            _section(
                "summary",
                1,
                "调整说明",
                '<div class="lead"><p>本报告固定保存调整前状态、当前状态和受影响产物的内容校验码。旧报告不会被静默覆盖；网页报告发生变化时也会明确列为“修改”。</p></div>'
                + _table(
                    ["项目", "内容"],
                    [
                        [_escape("调整编号"), _code(revision.get("revision_id"))],
                        [_escape("目标阶段"), _code(revision.get("target_stage"))],
                        [_escape("发起人"), _escape(revision.get("requested_by"))],
                        [_escape("调整原因"), _escape(revision.get("reason"))],
                        [_escape("调整状态"), _badge(revision.get("status"))],
                    ],
                ),
            ),
            _section("stages", 2, "阶段状态前后对比", _table(["阶段", "调整前", "当前"], stage_rows)),
            _section(
                "files",
                3,
                "产物与报告差异",
                _table(["变化类型", "产物路径", "调整前内容校验码", "当前内容校验码", "说明"], change_rows, "当前尚未形成文件差异。"),
            ),
            _section("details", 4, "可读文本差异", excerpt_html),
        ]
    )
    return _document(
        stage_dir=revision_dir,
        title="本体工程阶段调整差异报告",
        subtitle=f'{project.get("project_name", "本体项目")} · 调整记录 {revision.get("revision_id", "未编号")}',
        stage=f'{revision.get("target_stage", "当前阶段")} 调整审计',
        status="PASSED" if revision.get("status") == "COMPLETED" else "RUNNING",
        metrics=[
            ("新增产物", summary.get("added", 0), "本次新增"),
            ("修改产物", summary.get("modified", 0), "本次修改"),
            ("移除产物", summary.get("removed", 0), "本次移除"),
            ("未变化", summary.get("unchanged", 0), "保持不变"),
        ],
        toc=[
            ("summary", "调整说明"),
            ("stages", "状态对比"),
            ("files", "产物与报告差异"),
            ("details", "文本差异"),
        ],
        sections=sections,
        chart_script="revision-charts.js",
        sources=[
            ("调整记录", "revision.json"),
            ("调整前状态", "before-state.json"),
            ("当前状态", "after-state.json"),
            ("调整差异清单", "diff.json"),
        ],
    )


def _status_tone(status: Any) -> str:
    return {
        "DATABASE_FACT": "ok",
        "DOCUMENT_EVIDENCE": "ok",
        "AI_INFERENCE": "warning",
        "NEEDS_HUMAN_CONFIRMATION": "danger",
    }.get(str(status), "neutral")


def _chart_script_s1(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return _CHART_COMMON.replace("__DATA__", payload).replace("__BODY__", _S1_CHART_BODY)


def _chart_script_s2(data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return _CHART_COMMON.replace("__DATA__", payload).replace("__BODY__", _S2_CHART_BODY)


_CHART_COMMON = r'''(function () {
  'use strict';
  var data = __DATA__;
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var palette = [accent, accent2, muted, style.getPropertyValue('--ok').trim(), style.getPropertyValue('--warning').trim()];
  function baseGrid() { return { left: 12, right: 28, top: 20, bottom: 20, containLabel: true }; }
  function tooltip() { return { trigger: 'item', appendToBody: true }; }
  function mount(id, option) {
    var el = document.getElementById(id);
    if (!el) return;
    var chart = echarts.init(el, null, { renderer: 'svg' });
    option.animation = false;
    option.color = option.color || palette;
    option.textStyle = { color: ink };
    chart.setOption(option);
    window.addEventListener('resize', function () { chart.resize(); });
  }
  __BODY__
}());
'''

_S1_CHART_BODY = r'''
  mount('chart-row-counts', {
    tooltip: tooltip(), grid: baseGrid(),
    xAxis: { type: 'value', axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'category', inverse: true, data: data.rowCounts.map(function (d) { return d.name; }), axisLabel: { color: muted }, axisLine: { lineStyle: { color: rule } } },
    series: [{ type: 'bar', data: data.rowCounts.map(function (d) { return d.value; }), itemStyle: { color: accent, borderRadius: [0, 5, 5, 0] }, label: { show: true, position: 'right', color: ink } }]
  });
  mount('chart-enum-sizes', {
    tooltip: tooltip(), grid: baseGrid(),
    xAxis: { type: 'category', data: data.enumSizes.map(function (d) { return d.name; }), axisLabel: { color: muted, rotate: 35 }, axisLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'value', axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } } },
    series: [{ type: 'bar', data: data.enumSizes.map(function (d) { return d.value; }), itemStyle: { color: accent2, borderRadius: [5, 5, 0, 0] }, label: { show: true, position: 'top', color: ink } }]
  });
'''

_S2_CHART_BODY = r'''
  mount('chart-kinds', {
    tooltip: tooltip(), legend: { bottom: 0, textStyle: { color: muted } },
    series: [{ type: 'pie', radius: ['48%', '72%'], center: ['50%', '44%'], data: data.kinds, label: { color: ink, formatter: '{b}\n{c}' }, itemStyle: { borderColor: bg2, borderWidth: 3 } }]
  });
  mount('chart-statuses', {
    tooltip: tooltip(), grid: baseGrid(),
    xAxis: { type: 'value', axisLabel: { color: muted }, splitLine: { lineStyle: { color: rule } } },
    yAxis: { type: 'category', data: data.statuses.map(function (d) { return d.name; }), axisLabel: { color: muted }, axisLine: { lineStyle: { color: rule } } },
    series: [{ type: 'bar', data: data.statuses.map(function (d) { return d.value; }), itemStyle: { color: accent, borderRadius: [0, 5, 5, 0] }, label: { show: true, position: 'right', color: ink } }]
  });
'''
