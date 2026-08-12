"""Secret redaction and retention policy helpers.

The boundary is intentionally boring: every persisted or outbound string passes
through the same deterministic replacement rules. Unknown secrets are never
echoed as part of a diagnostic; custom project patterns are applied in addition
to the built-in token/key formats.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REDACTED = "[REDACTED]"
CUSTOM_REDACTED = "[REDACTED:CUSTOM]"

_BUILTINS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.I | re.S), REDACTED),
    (re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"), REDACTED),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"), REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"), REDACTED),
    (re.compile(r"(?i)(\b(?:api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*[\"']?)([^\s\"',;}{]{8,})"), r"\1" + REDACTED),
    (re.compile(r"(?i)(https?://[^/\s:@]+:)([^@\s]+)(@)"), r"\1" + REDACTED + r"\3"),
)

@dataclass(frozen=True)
class RetentionPolicy:
    runs_days: int = 30
    calls_days: int = 30
    events_days: int = 30
    diffs_days: int = 7
    attachments_days: int = 7

@dataclass(frozen=True)
class SecurityPolicy:
    custom_patterns: tuple[str, ...] = ()
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)

class Redactor:
    def __init__(self, policy: SecurityPolicy | None = None):
        self.policy = policy or SecurityPolicy()
        self._custom = []
        for pattern in self.policy.custom_patterns:
            try:
                self._custom.append(re.compile(pattern))
            except re.error:
                continue

    def text(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        result = value
        for pattern, replacement in _BUILTINS:
            result = pattern.sub(replacement, result)
        for pattern in self._custom:
            result = pattern.sub(CUSTOM_REDACTED, result)
        return result

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.value(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self.value(v) for v in value)
        return value

    def exception(self, exc: BaseException) -> str:
        return self.text(f"{type(exc).__name__}: {exc}")

    def result(self, result):
        result.summary = self.text(result.summary)
        result.raw = self.value(result.raw)
        return result

def _days(raw: Any, default: int) -> int:
    return int(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0 else default

def load_policy(engine_root: Path, target_root: Path | None = None) -> SecurityPolicy:
    engine_data = {}
    path = engine_root / "config/platform.yaml"
    if path.is_file():
        engine_data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    security = engine_data.get("security") or {}
    custom = security.get("redaction_patterns") or []
    if target_root is not None:
        target_path = target_root / ".ai-platform.yml"
        if target_path.is_file():
            target_data = yaml.safe_load(target_path.read_text(encoding="utf-8")) or {}
            custom = [*custom, *(target_data.get("redaction_patterns") or [])]
    custom = tuple(p for p in custom if isinstance(p, str) and p.strip())
    raw_retention = security.get("retention") or {}
    retention = RetentionPolicy(
        runs_days=_days(raw_retention.get("runs_days"), 30),
        calls_days=_days(raw_retention.get("calls_days"), 30),
        events_days=_days(raw_retention.get("events_days"), 30),
        diffs_days=_days(raw_retention.get("diffs_days"), 7),
        attachments_days=_days(raw_retention.get("attachments_days"), 7),
    )
    return SecurityPolicy(custom_patterns=custom, retention=retention)

def redactor(engine_root: Path, target_root: Path | None = None) -> Redactor:
    return Redactor(load_policy(engine_root, target_root))


def secure_directory(path: Path) -> Path:
    """Create an artifact/backup directory with owner-only permissions."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        import os
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
