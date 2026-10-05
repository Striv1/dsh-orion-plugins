"""Deterministic Chinese labels for ontology design drafts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .reporting import ONTOLOGY_NAME_LABELS

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def normalize_ontology_design_localization(
    project_dir: Path, ontology_design: dict[str, Any]
) -> dict[str, Any]:
    """Fill deterministic display labels; leave uncertain translations to S4 gates."""

    design = json.loads(json.dumps(ontology_design, ensure_ascii=False))
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    project_name = str(project.get("project_name") or "").strip()
    domain = str(project.get("domain") or "").strip()
    default_title = project_name if _CJK.search(project_name) else domain
    if default_title and "本体" not in default_title:
        default_title = f"{default_title}本体"
    design.setdefault("title_zh", default_title)
    design.setdefault(
        "comment_zh",
        f"面向{domain or default_title}构建的企业本体；由正式证据、Mapping 和本体施工图追溯生成。",
    )
    kind_labels = {
        "classes": "业务类",
        "object_properties": "对象属性",
        "data_properties": "数据属性",
    }
    for collection_name, kind_label in kind_labels.items():
        for entity in design.get(collection_name) or []:
            name = str(entity.get("name") or "").strip()
            label_zh = str(
                entity.get("label_zh")
                or entity.get("business_name_zh")
                or ONTOLOGY_NAME_LABELS.get(name)
                or ""
            ).strip()
            if label_zh:
                entity["label_zh"] = label_zh
                entity.setdefault(
                    "comment_zh",
                    f"{label_zh}的{kind_label}定义；来源于正式 Mapping。",
                )
    return design
