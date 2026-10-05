from __future__ import annotations

import json
import mimetypes
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RouteRule:
    category: str
    label: str
    extensions: tuple[str, ...]
    primary: str
    fallbacks: tuple[str, ...]
    locator: str
    availability: str
    performance: str
    quality_control: str


@dataclass(frozen=True)
class RouteDecision:
    category: str
    label: str
    detected_format: str
    extension: str
    mime_type: str
    strategy: str
    fallback_chain: tuple[str, ...]
    locator: str
    availability: str
    performance: str
    quality_control: str
    reason: str


ROUTE_RULES = (
    RouteRule(
        "pdf",
        "PDF 文档",
        (".pdf",),
        "PDF_TEXT_LAYER_DIRECT",
        ("PADDLEOCR_PP_STRUCTURE_V3", "PADDLEOCR_OCR_FALLBACK", "HUMAN_REVIEW"),
        "页码与版面区域",
        "READY",
        "先检查文字层；只对扫描页或低质量页使用 OCR",
        "逐页文字覆盖、乱码率、结构完整性和人工抽样",
    ),
    RouteRule(
        "word",
        "Word 与开放文档",
        (".docx", ".odt", ".rtf"),
        "OFFICE_OR_OPEN_DOCUMENT_DIRECT",
        ("EMBEDDED_IMAGE_OCR", "LAYOUT_SERVICE", "HUMAN_REVIEW"),
        "标题、段落、表格、批注与嵌入对象",
        "PARTIAL_READY",
        "DOCX 已可直接解析；ODT/RTF 走兼容转换适配器",
        "段落与表格计数、嵌入对象清单、转换前后抽样",
    ),
    RouteRule(
        "spreadsheet",
        "Excel 与表格文件",
        (".xlsx", ".xlsm", ".xls", ".ods", ".csv", ".tsv"),
        "SPREADSHEET_SCHEMA_AND_VALUES",
        ("LEGACY_OFFICE_CONVERTER", "EMBEDDED_IMAGE_OCR", "HUMAN_REVIEW"),
        "工作表、单元格区域、行列与公式",
        "PARTIAL_READY",
        "XLSX/XLSM/CSV 已可直接解析；大表采用画像加受控样本",
        "表头、类型、行列数、空值率、公式与截断确认",
    ),
    RouteRule(
        "presentation",
        "演示文稿",
        (".pptx", ".ppt", ".odp", ".key"),
        "SLIDE_OBJECT_DIRECT",
        ("SLIDE_RENDER_AND_OCR", "VISION_LAYOUT", "HUMAN_REVIEW"),
        "页码、文本框、表格、备注与图形区域",
        "CONNECTOR_REQUIRED",
        "优先读取幻灯片对象；图片化页面才渲染并 OCR",
        "页数、对象覆盖、备注完整性和视觉抽样",
    ),
    RouteRule(
        "image",
        "图片与扫描件",
        (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".heic"),
        "PADDLEOCR_OCR",
        ("PADDLEOCR_PP_STRUCTURE_V3", "VISION_MODEL", "HUMAN_REVIEW"),
        "像素区域、边界框和图片编号",
        "READY_MCP",
        "普通文字图片走 10827；表格、表单和复杂版面升级 10826",
        "分辨率、方向、文字置信度、区域覆盖与人工抽样",
    ),
    RouteRule(
        "text",
        "文本与轻量标记",
        (".txt", ".md", ".html", ".htm", ".xml", ".json", ".yaml", ".yml"),
        "TEXT_OR_MARKUP_DIRECT",
        ("ENCODING_REPAIR", "HTML_RENDER", "HUMAN_REVIEW"),
        "行号、标题、节点路径或字段路径",
        "READY",
        "直接解析字符与层级，不调用 OCR",
        "编码、结构闭合、空内容、脚本噪声与字段覆盖",
    ),
    RouteRule(
        "email",
        "邮件与会话导出",
        (".eml", ".msg", ".mbox"),
        "EMAIL_MIME_DIRECT",
        ("OUTLOOK_CONNECTOR", "ATTACHMENT_RECURSIVE_ROUTE", "HUMAN_REVIEW"),
        "邮件编号、主题、正文、附件和会话位置",
        "PARTIAL_READY",
        "EML 可直接解析；MSG 需要 Outlook/专用转换器",
        "发件人与时间、正文版本、附件清单和线程去重",
    ),
    RouteRule(
        "archive",
        "压缩包与资料包",
        (".zip", ".7z", ".rar", ".tar", ".gz"),
        "SAFE_ARCHIVE_EXPANSION",
        ("CHILD_FILE_RECURSIVE_ROUTE", "QUARANTINE_REVIEW"),
        "包内相对路径与子文件位置",
        "CONNECTOR_REQUIRED",
        "先检查压缩炸弹、路径穿越、加密和文件数量，再递归路由",
        "展开大小、压缩比、嵌套层级、重复文件和恶意内容扫描",
    ),
    RouteRule(
        "audio",
        "音频与会议录音",
        (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"),
        "ASR_WITH_DIARIZATION",
        ("DOMAIN_VOCABULARY_CORRECTION", "HUMAN_REVIEW"),
        "时间码、说话人和音轨",
        "SERVICE_REQUIRED",
        "分段并行转写，专业词表修正，不逐帧处理",
        "语音识别置信度、说话人区分、静音和人工抽样",
    ),
    RouteRule(
        "video",
        "视频与屏幕录制",
        (".mp4", ".mov", ".mkv", ".avi", ".webm"),
        "VIDEO_DEMUX_ASR_KEYFRAMES",
        ("FRAME_OCR", "VISION_SUMMARY", "HUMAN_REVIEW"),
        "时间码、关键帧、字幕和音轨",
        "SERVICE_REQUIRED",
        "音轨转写加场景变化抽帧，避免逐帧 OCR",
        "字幕覆盖、关键帧召回、音画对齐和人工抽样",
    ),
    RouteRule(
        "design_cad",
        "设计图、CAD 与工程模型",
        (".dwg", ".dxf", ".ifc", ".rvt", ".psd", ".ai", ".fig"),
        "DOMAIN_NATIVE_CONNECTOR",
        ("RENDERED_VIEW_OCR", "VISION_MODEL", "HUMAN_REVIEW"),
        "图层、构件、坐标、标注与视图",
        "SERVICE_REQUIRED",
        "必须先使用领域原生解析器，渲染图片只作为补充证据",
        "图层与构件数量、坐标系、单位、标注覆盖和专业复核",
    ),
    RouteRule(
        "unknown",
        "未知或专有格式",
        (),
        "QUARANTINE_AND_CLASSIFY",
        ("FORMAT_CONNECTOR", "SAFE_RENDER", "HUMAN_REVIEW"),
        "文件级定位",
        "REVIEW_REQUIRED",
        "不猜测格式、不直接执行，先隔离并识别真实内容类型",
        "文件签名、加密、可执行内容、来源授权和人工决定",
    ),
)

RULE_BY_EXTENSION = {
    extension: rule for rule in ROUTE_RULES for extension in rule.extensions
}
EXECUTABLE_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".xlsm",
    ".csv",
    ".tsv",
    ".txt",
    ".md",
    ".html",
    ".htm",
    ".xml",
    ".json",
    ".yaml",
    ".yml",
    ".eml",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
}

STRATEGY_LABELS = {
    "PDF_TEXT_LAYER_DIRECT": "读取 PDF 原生文字层",
    "PADDLEOCR_PP_STRUCTURE_V3": "识别扫描页的版面、表格与文字",
    "OFFICE_OR_OPEN_DOCUMENT_DIRECT": "读取 Office 文档原生结构",
    "SPREADSHEET_SCHEMA_AND_VALUES": "读取表格结构、字段和值",
    "SLIDE_OBJECT_DIRECT": "读取幻灯片原生对象",
    "PADDLEOCR_OCR": "识别图片中的文字",
    "TEXT_OR_MARKUP_DIRECT": "读取文本与标记结构",
    "EMAIL_MIME_DIRECT": "读取邮件正文、头信息与附件清单",
    "SAFE_ARCHIVE_EXPANSION": "安全展开后递归分流子文件",
    "ASR_WITH_DIARIZATION": "语音转写并区分说话人",
    "VIDEO_DEMUX_ASR_KEYFRAMES": "分离音轨并提取关键画面",
    "DOMAIN_NATIVE_CONNECTOR": "使用领域原生解析器读取",
    "QUARANTINE_AND_CLASSIFY": "隔离并确认真实格式",
}


def sniff_container(path: Path) -> str | None:
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return None
    if head.startswith(b"%PDF-"):
        return ".pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
            if "word/document.xml" in names:
                return ".docx"
            if "xl/workbook.xml" in names:
                return ".xlsx"
            if "ppt/presentation.xml" in names:
                return ".pptx"
            return ".zip"
        except (OSError, zipfile.BadZipFile):
            return ".zip"
    return None


def detect_extension(path: Path) -> tuple[str, str]:
    declared = path.suffix.lower()
    detected = sniff_container(path) or declared
    return declared, detected


def route_file(
    path: Path,
    *,
    signals: dict[str, Any] | None = None,
) -> RouteDecision:
    signals = signals or {}
    declared, detected = detect_extension(path)
    rule = RULE_BY_EXTENSION.get(detected) or RULE_BY_EXTENSION.get(declared) or ROUTE_RULES[-1]
    strategy = rule.primary
    reason = (
        f"文件签名或扩展名识别为 {rule.label}，"
        f"优先{STRATEGY_LABELS.get(rule.primary, '使用该格式的专业解析器')}。"
    )
    if rule.category == "pdf":
        if signals.get("direct_text_ready") is True:
            strategy = "PDF_TEXT_LAYER_DIRECT"
            reason = str(signals.get("reason") or "PDF 文字层完整，直接解析，不调用 OCR。")
        elif signals.get("direct_text_ready") is False:
            strategy = "PADDLEOCR_PP_STRUCTURE_V3"
            reason = str(signals.get("reason") or "PDF 文字层不足，升级到版面结构识别。")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return RouteDecision(
        category=rule.category,
        label=rule.label,
        detected_format=detected or "unknown",
        extension=declared,
        mime_type=mime_type,
        strategy=strategy,
        fallback_chain=rule.fallbacks,
        locator=rule.locator,
        availability=rule.availability,
        performance=rule.performance,
        quality_control=rule.quality_control,
        reason=reason,
    )


def route_catalog() -> list[dict[str, Any]]:
    return [asdict(rule) for rule in ROUTE_RULES]


def catalog_payload() -> dict[str, Any]:
    return {
        "router": "ORION_UNSTRUCTURED_ROUTER",
        "version": "1.0.0",
        "policy": "direct-first, specialist-second, OCR-or-ASR-fallback, human-gated-commit",
        "executable_extensions": sorted(EXECUTABLE_EXTENSIONS),
        "categories": route_catalog(),
    }


if __name__ == "__main__":
    print(json.dumps(catalog_payload(), ensure_ascii=False, indent=2))
