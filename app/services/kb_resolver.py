"""
Knowledge base resolver — YAML KB loader + selective fragment injection.

Loads all YAML KBs once at startup. Per request, selects only the
relevant fragments (max N grades + N rules) based on:
  1. Explicit kb_context from the caller
  2. Entity field hints (polymer, material, Industry, etc.)

Audit fixes applied:
  - H11: KB is SUPPLEMENTARY, not a gate. Returns empty context if
         no KB matches — never raises, never blocks enrichment.
  - H12: Filters by polymer/domain, not global dump.
  - M9: Skips entries with empty descriptions.
"""

from __future__ import annotations

from app.utils.safe_convert import safe_float
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("kb_resolver")


@dataclass
class KBIndex:
    """Indexed knowledge base ready for fragment selection."""

    material_grades: dict[str, list[dict]] = field(default_factory=dict)
    inference_rules: list[dict] = field(default_factory=list)
    recycling_rules: dict[str, list[dict]] = field(default_factory=dict)
    contamination_profiles: dict[str, list[dict]] = field(default_factory=dict)
    quality_tiers: dict[str, dict] = field(default_factory=dict)
    raw: dict[str, dict] = field(default_factory=dict)
    files_loaded: list[str] = field(default_factory=list)

    @property
    def polymers(self) -> list[str]:
        return sorted(self.material_grades.keys())

    @property
    def total_grades(self) -> int:
        return sum(len(v) for v in self.material_grades.values())

    @property
    def total_rules(self) -> int:
        return len(self.inference_rules)

    @property
    def is_loaded(self) -> bool:
        return len(self.files_loaded) > 0


class KBResolver:
    """Loads KB once at startup. Resolves relevant fragments per request."""

    def __init__(self, kb_dir: str | Path) -> None:
        self.kb_dir = Path(kb_dir)
        self.index = KBIndex()
        self._load()

    # ── Loading ──────────────────────────────────────

    def _load(self) -> None:
        if not self.kb_dir.exists():
            logger.warning("KB directory %s does not exist — running without KB", self.kb_dir)
            return

        loaded: set[str] = set()

        for yf in sorted(self.kb_dir.glob("*.yaml")):
            if yf.name in loaded:
                continue
            try:
                with open(yf, encoding="utf-8") as fh:
                    kb = yaml.safe_load(fh)
                if not isinstance(kb, dict):
                    continue
            except Exception as exc:
                logger.warning("Skipping %s: %s", yf.name, exc)
                continue

            meta = kb.get("metadata", {})
            polymer = (
                meta.get("polymertype") or meta.get("materialtype") or meta.get("kbname") or yf.stem
            )
            key = polymer.upper().replace(" ", "_")

            self.index.raw[key] = kb
            self.index.files_loaded.append(yf.name)
            loaded.add(yf.name)

            for grade in kb.get("materialgrades", []):
                grade["_source_file"] = yf.name
                self.index.material_grades.setdefault(key, []).append(grade)

            for rule in kb.get("inferencerules", []):
                rule["_polymer"] = key
                rule["_source_file"] = yf.name
                self.index.inference_rules.append(rule)

            for rule in kb.get("recyclingrules", []):
                rule["_source_file"] = yf.name
                self.index.recycling_rules.setdefault(key, []).append(rule)

            for prof in kb.get("contaminationprofiles", []):
                prof["_source_file"] = yf.name
                self.index.contamination_profiles.setdefault(key, []).append(prof)

            tiers = kb.get("sourcequalitytiers", {})
            if isinstance(tiers, dict) and tiers:
                self.index.quality_tiers[key] = tiers

        logger.info(
            "KB loaded: %d files, %d polymers, %d grades, %d rules",
            len(self.index.files_loaded),
            len(self.index.polymers),
            self.index.total_grades,
            self.index.total_rules,
        )

    # ── Resolution ───────────────────────────────────

    def resolve(
        self,
        kb_context: str | None = None,
        entity: dict[str, Any] | None = None,
        max_fragments: int = 3,
        confidence_threshold: float = 0.85,
    ) -> dict[str, Any]:
        """
        Select relevant KB fragments for prompt injection.

        NEVER raises. Returns empty context if no match found.
        The enrichment pipeline runs regardless — KB is supplementary.
        """
        empty = {
            "context_text": "",
            "content_hash": hashlib.sha256(b"").hexdigest(),
            "fragment_ids": [],
            "kb_files": [],
        }

        if not self.index.is_loaded:
            return empty

        # ── Determine target domains ─────────────────
        target_keys: list[str] = []

        if kb_context:
            key = kb_context.upper().replace(" ", "_")
            if key in self.index.raw:
                target_keys.append(key)
            else:
                # Fuzzy prefix match
                for k in self.index.raw:
                    if kb_context.upper() in k and k not in target_keys:
                        target_keys.append(k)

        if entity:
            for hint in (
                "polymer",
                "material",
                "material_type",
                "Industry",
                "x_material_type",
                "material_profile",
            ):
                val = entity.get(hint)
                if val and isinstance(val, str):
                    key = val.upper().replace(" ", "_")
                    if key in self.index.raw and key not in target_keys:
                        target_keys.append(key)

        if not target_keys:
            return empty

        # ── Select fragments ─────────────────────────
        content_blocks: list[str] = []
        fragment_ids: list[str] = []
        kb_files: list[str] = []

        for key in target_keys[:2]:  # Max 2 polymer domains
            # Grades — skip empty descriptions (M9 fix)
            grades = self.index.material_grades.get(key, [])
            added = 0
            for grade in grades:
                if added >= max_fragments:
                    break
                gid = grade.get("gradeid", grade.get("id", "unknown"))
                desc = grade.get("description", "") or grade.get("gradename", "")
                if not desc.strip():
                    continue
                block = f"Grade[{gid}]: {desc[:200]}"
                content_blocks.append(block)
                fragment_ids.append(f"grade:{gid}")
                added += 1

                sf = grade.get("_source_file", "")
                if sf and sf not in kb_files:
                    kb_files.append(sf)

            # Rules — high confidence only, skip empty conclusions (M9 fix)
            rules = [
                r
                for r in self.index.inference_rules
                if r.get("_polymer") == key
                and safe_float(r.get("confidence", 0)) >= confidence_threshold
            ]
            rules.sort(key=lambda r: safe_float(r.get("confidence", 0)), reverse=True)

            added = 0
            for rule in rules:
                if added >= max_fragments:
                    break
                rid = rule.get("ruleid", rule.get("inferenceid", "unknown"))
                conclusion = rule.get("conclusion", {})
                if not conclusion:
                    continue
                content_blocks.append(f"Rule[{rid}]: {conclusion}")
                fragment_ids.append(f"rule:{rid}")
                added += 1

        if not content_blocks:
            return empty

        combined = "\n".join(content_blocks)
        content_hash = hashlib.sha256(combined.encode()).hexdigest()

        return {
            "context_text": combined,
            "content_hash": content_hash,
            "fragment_ids": fragment_ids,
            "kb_files": kb_files,
        }
