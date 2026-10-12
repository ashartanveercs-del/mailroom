"""Settings from a TOML file (see `mailroom.example.toml`).

Strict on purpose: an unknown section or key is an error, not something silently ignored, so a
typo like `querry` fails at startup instead of quietly filing with the wrong settings.
Relative paths are resolved against the config file's directory. Secrets are never stored in
the file: the model API key is read from the environment variable the config names.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .defaults import DEFAULT_TYPES
from .pipeline import DEFAULT_QUERY
from .routing import DocType, Policy, TemplateError

MIB = 1024 * 1024
SECTIONS: dict[str, frozenset[str]] = {
    "mailbox": frozenset({"query", "allowed_senders", "max_attachment_mb"}),
    "drive": frozenset({"root_folder_id", "review_folder", "max_upload_mb"}),
    "google": frozenset({"credentials_file"}),
    "classifier": frozenset({"kind", "endpoint", "model", "api_key_env", "cross_check",
                             "grounding", "date_order", "vision_min_confidence", "vendors",
                             "owner_names", "max_attempts"}),
    "ledger": frozenset({"path"}),
    "split": frozenset({"enabled"}),
}
TYPE_KEYS = frozenset({"description", "folder", "filename", "keywords", "exclude",
                       "number_pattern", "min_confidence"})
_ID = re.compile(r"[A-Za-z0-9_-]{1,256}")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    root_folder_id: str
    credentials_file: Path
    ledger_path: Path
    query: str = DEFAULT_QUERY
    allowed_senders: tuple[str, ...] = ()
    max_attachment_bytes: int = 25 * MIB
    max_upload_bytes: int = 5 * MIB  # Drive's limit for a single multipart upload
    review_folder: str = "Needs review"
    classifier: str = "llm"
    endpoint: str = "https://api.openai.com/v1/chat/completions"
    model: str = ""
    api_key_env: str = "MAILROOM_LLM_API_KEY"
    cross_check: bool = True
    grounding: bool = True
    date_order: str = "MDY"
    vision_min_confidence: float = 0.95
    vendors: tuple[str, ...] = ()
    owner_names: tuple[str, ...] = ()
    max_attempts: int = 3
    split: bool = True
    types: Mapping[str, DocType] = field(default_factory=lambda: dict(DEFAULT_TYPES))

    def policy(self) -> Policy:
        return Policy(types=self.types, review_folder=self.review_folder,
                      cross_check=self.cross_check, grounding=self.grounding,
                      date_order=self.date_order,
                      vision_min_confidence=self.vision_min_confidence,
                      owner_names=self.owner_names)

    def api_key(self, env: Mapping[str, str]) -> str:
        value = env.get(self.api_key_env, "").strip()
        if not value:
            raise ConfigError(f"set {self.api_key_env} to the model provider's API key")
        return value

    @classmethod
    def load(cls, path: str | Path) -> Settings:
        path = Path(path)
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}") from None
        except tomllib.TOMLDecodeError as err:
            raise ConfigError(f"{path} is not valid TOML: {err}") from None
        return _parse(raw, path.resolve().parent)


def _expect(value: Any, kind: type | tuple[type, ...], name: str) -> Any:
    if isinstance(value, bool) and kind is not bool:
        raise ConfigError(f"{name} has the wrong type")
    if not isinstance(value, kind):
        raise ConfigError(f"{name} has the wrong type")
    return value


def _strings(value: Any, name: str) -> tuple[str, ...]:
    items = _expect(value, list, name)
    if not all(isinstance(v, str) for v in items):
        raise ConfigError(f"{name} must be a list of strings")
    return tuple(items)


def _positive(value: Any, name: str) -> float:
    number = _expect(value, (int, float), name)
    if number <= 0:
        raise ConfigError(f"{name} must be positive")
    return number


def _parse(raw: dict[str, Any], base: Path) -> Settings:
    for section, body in raw.items():
        if section == "types":
            continue
        if section not in SECTIONS:
            raise ConfigError(f"unknown section [{section}]")
        if not isinstance(body, dict):
            raise ConfigError(f"[{section}] must be a table")
        for key in body:
            if key not in SECTIONS[section]:
                raise ConfigError(f"unknown key '{section}.{key}'")
    mailbox, drive = raw.get("mailbox", {}), raw.get("drive", {})
    clf, google = raw.get("classifier", {}), raw.get("google", {})
    kwargs: dict[str, Any] = {}

    root = drive.get("root_folder_id")
    if not (isinstance(root, str) and _ID.fullmatch(root)):
        raise ConfigError("drive.root_folder_id is required: the id of the Drive folder to "
                          "file into (letters, digits, '-' and '_')")
    kwargs["root_folder_id"] = root
    kwargs["credentials_file"] = base / _expect(
        google.get("credentials_file", "token.json"), str, "google.credentials_file")
    kwargs["ledger_path"] = base / _expect(raw.get("ledger", {}).get("path", "mailroom.db"),
                                           str, "ledger.path")
    extra = _expect(mailbox.get("query", ""), str, "mailbox.query").strip()
    kwargs["query"] = f"{DEFAULT_QUERY} {extra}" if extra else DEFAULT_QUERY
    if "allowed_senders" in mailbox:
        kwargs["allowed_senders"] = _strings(mailbox["allowed_senders"],
                                             "mailbox.allowed_senders")
    if "max_attachment_mb" in mailbox:
        kwargs["max_attachment_bytes"] = int(
            _positive(mailbox["max_attachment_mb"], "mailbox.max_attachment_mb") * MIB)
    if "max_upload_mb" in drive:
        kwargs["max_upload_bytes"] = int(_positive(drive["max_upload_mb"],
                                                   "drive.max_upload_mb") * MIB)
    if "review_folder" in drive:
        kwargs["review_folder"] = _expect(drive["review_folder"], str, "drive.review_folder")

    kind = clf.get("kind", "llm")
    if kind not in ("llm", "rules"):
        raise ConfigError("classifier.kind must be 'llm' or 'rules'")
    kwargs["classifier"] = kind
    for key in ("endpoint", "model", "api_key_env", "date_order"):
        if key in clf:
            kwargs[key] = _expect(clf[key], str, f"classifier.{key}")
    for key in ("cross_check", "grounding"):
        if key in clf:
            kwargs[key] = _expect(clf[key], bool, f"classifier.{key}")
    if "vision_min_confidence" in clf:
        kwargs["vision_min_confidence"] = float(_expect(
            clf["vision_min_confidence"], (int, float), "classifier.vision_min_confidence"))
    if "vendors" in clf:
        kwargs["vendors"] = _strings(clf["vendors"], "classifier.vendors")
    if "owner_names" in clf:
        kwargs["owner_names"] = _strings(clf["owner_names"], "classifier.owner_names")
    if "max_attempts" in clf:
        kwargs["max_attempts"] = int(_positive(clf["max_attempts"], "classifier.max_attempts"))
    if kind == "llm":
        if not kwargs.get("model"):
            raise ConfigError("classifier.model is required for the llm classifier")
        if not kwargs.get("endpoint", Settings.endpoint).startswith("https://"):
            raise ConfigError("classifier.endpoint must be an https URL")
    if "split" in raw:
        kwargs["split"] = _expect(raw["split"].get("enabled", True), bool, "split.enabled")
    if "types" in raw:
        kwargs["types"] = _types(raw["types"])
    settings = Settings(**kwargs)
    try:
        settings.policy()
    except (TemplateError, ValueError) as err:
        raise ConfigError(f"types: {err}") from None
    return settings


def _types(raw: Any) -> dict[str, DocType]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError("[types] must hold at least one [types.<name>] table")
    out = {}
    for name, body in raw.items():
        if not isinstance(body, dict):
            raise ConfigError(f"types.{name} must be a table")
        unknown = sorted(set(body) - TYPE_KEYS)
        if unknown:
            raise ConfigError(f"unknown key 'types.{name}.{unknown[0]}'")
        for key in ("description", "folder", "filename"):
            if not isinstance(body.get(key), str):
                raise ConfigError(f"types.{name}.{key} is required")
        pattern = body.get("number_pattern")
        if pattern is not None:
            try:
                re.compile(_expect(pattern, str, f"types.{name}.number_pattern"))
            except re.error:
                raise ConfigError(f"types.{name}.number_pattern is not a valid regular "
                                  "expression") from None
        out[name] = DocType(
            name=name, description=body["description"], folder=body["folder"],
            filename=body["filename"],
            keywords=_strings(body.get("keywords", []), f"types.{name}.keywords"),
            exclude=_strings(body.get("exclude", []), f"types.{name}.exclude"),
            number_pattern=pattern,
            min_confidence=float(_expect(body.get("min_confidence", 0.85), (int, float),
                                         f"types.{name}.min_confidence")))
    return out
