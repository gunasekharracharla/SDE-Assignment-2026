"""Workflow schema definitions and disposition catalogue loader."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from solution.config import DISPOSITIONS_PATH, SCHEMAS_DIR


def normalize_header(header: str) -> str:
    """Normalize a header name per specification:
    
    1. Lower-case it
    2. Trim leading/trailing whitespace
    3. Replace every run of '_' or whitespace with a single space
    """
    clean = header.strip().lower()
    return re.sub(r"[\s_]+", " ", clean)


@dataclass(frozen=True)
class FieldSchema:
    name: str
    type: str  # 'string', 'phone', 'date', 'decimal', 'enum'
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: List[str] = field(default_factory=list)
    values: List[str] = field(default_factory=list)
    default: Optional[str] = None


@dataclass
class WorkflowSchema:
    workflow_id: str
    description: str
    fields: List[FieldSchema]  # Preserves exact schema order

    @property
    def reference_field_name(self) -> str:
        for f in self.fields:
            if f.reference:
                return f.name
        raise ValueError(f"No reference field found in schema {self.workflow_id}")

    def build_header_map(self) -> Dict[str, str]:
        """Build mapping from normalized header alias to schema field name."""
        mapping: Dict[str, str] = {}
        for f in self.fields:
            # Map canonical name
            mapping[normalize_header(f.name)] = f.name
            # Map all aliases
            for alias in f.aliases:
                mapping[normalize_header(alias)] = f.name
        return mapping


@dataclass(frozen=True)
class DispositionInfo:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


def load_schemas(schemas_dir: Path = SCHEMAS_DIR) -> Dict[str, WorkflowSchema]:
    """Load all workflow schemas from JSON files in schemas_dir."""
    schemas: Dict[str, WorkflowSchema] = {}
    for json_file in schemas_dir.glob("*.json"):
        with open(json_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            wf_id = data["workflow_id"]
            fields: List[FieldSchema] = []
            for item in data.get("fields", []):
                fields.append(
                    FieldSchema(
                        name=item["name"],
                        type=item["type"],
                        required=item.get("required", False),
                        pii=item.get("pii", False),
                        reference=item.get("reference", False),
                        aliases=item.get("aliases", []),
                        values=item.get("values", []),
                        default=item.get("default"),
                    )
                )
            schemas[wf_id] = WorkflowSchema(
                workflow_id=wf_id,
                description=data.get("description", ""),
                fields=fields,
            )
    return schemas


def load_dispositions(dispositions_path: Path = DISPOSITIONS_PATH) -> Dict[str, Dict[str, DispositionInfo]]:
    """Load dispositions catalogue from CSV.
    
    Returns nested dictionary: {workflow_id: {disposition_code: DispositionInfo}}
    """
    disps: Dict[str, Dict[str, DispositionInfo]] = {}
    with open(dispositions_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wf_id = row["workflow_id"].strip()
            code = row["code"].strip()
            label = row["label"].strip()
            is_terminal = row["is_terminal"].strip().lower() == "true"
            is_success = row["is_success"].strip().lower() == "true"
            if wf_id not in disps:
                disps[wf_id] = {}
            disps[wf_id][code] = DispositionInfo(
                workflow_id=wf_id,
                code=code,
                label=label,
                is_terminal=is_terminal,
                is_success=is_success,
            )
    return disps
