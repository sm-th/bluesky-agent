"""Validated runtime configuration for the Bluesky research agent."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Mapping
from urllib.parse import urlparse


_ENV_PREFIX = "BLUESKY_AGENT_"
_SECRET_FIELDS = frozenset({"agent_app_password", "github_token"})
_REQUIRED_FIELDS = frozenset(
    {
        "operator_handle",
        "operator_did",
        "agent_handle",
        "agent_app_password",
        "wiki_repo_url",
        "wiki_site_url",
        "github_token",
    }
)
_DEFAULTS: dict[str, object] = {
    "bluesky_service": "https://bsky.social",
    "bluesky_public_api": "https://public.api.bsky.app",
    "state_dir": ".bluesky-agent",
    "poll_interval": 30.0,
    "omp_model": "openai-codex/gpt-5.6-sol",
    "omp_timeout": 1800.0,
    "intent_model": "auto",
    "intent_timeout": 10.0,
    "intent_confidence_threshold": 0.70,
    "wiki_ready_timeout": 120.0,
    "wiki_ready_interval": 2.0,
    "chromium_bin": "chromium",
    "git_user_name": "Bluesky Research Agent",
    "git_user_email": "bluesky-agent@localhost",
}
_FLOAT_FIELDS = frozenset(
    {
        "poll_interval",
        "omp_timeout",
        "intent_timeout",
        "intent_confidence_threshold",
        "wiki_ready_timeout",
        "wiki_ready_interval",
    }
)


class ConfigError(ValueError):
    """The supplied configuration is absent, malformed, or unsafe."""


@dataclass(frozen=True)
class Config:
    """Complete immutable runtime configuration.

    Non-secret values may come from a TOML file and are overridden by matching
    ``BLUESKY_AGENT_*`` environment variables. Credentials are deliberately
    accepted from the environment only.
    """

    operator_handle: str
    operator_did: str
    agent_handle: str
    agent_app_password: str
    bluesky_service: str
    bluesky_public_api: str
    wiki_repo_url: str
    wiki_site_url: str
    state_dir: Path
    poll_interval: float
    omp_model: str
    omp_timeout: float
    wiki_ready_timeout: float
    wiki_ready_interval: float
    chromium_bin: str
    git_user_name: str
    git_user_email: str
    github_token: str
    intent_model: str = "auto"
    intent_timeout: float = 10.0
    intent_confidence_threshold: float = 0.70

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, str) and not value.strip():
                raise ConfigError(f"{field.name} must not be empty")
        if not self.operator_did.startswith("did:"):
            raise ConfigError("operator_did must be a DID")
        for name in ("bluesky_service", "bluesky_public_api", "wiki_site_url"):
            value = str(getattr(self, name))
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ConfigError(f"{name} must be an absolute HTTP(S) URL")
        repo = urlparse(self.wiki_repo_url)
        if not (
            (repo.scheme in {"http", "https", "ssh"} and bool(repo.netloc))
            or self.wiki_repo_url.startswith("git@")
        ):
            raise ConfigError("wiki_repo_url must be an absolute Git repository URL")
        for name in _FLOAT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise ConfigError(f"{name} must be greater than zero")
        if not 0 < self.intent_confidence_threshold <= 1:
            raise ConfigError("intent_confidence_threshold must be greater than zero and at most one")
        if not isinstance(self.state_dir, Path):
            raise ConfigError("state_dir must be a pathlib.Path")

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> Config:
        """Load and validate configuration.

        TOML may either be flat or place all keys below ``[bluesky_agent]``.
        Supplying either secret in TOML is rejected even when an environment
        value would override it.
        """

        env = os.environ if environ is None else environ
        configured_path = path or env.get(f"{_ENV_PREFIX}CONFIG")
        values: dict[str, object] = dict(_DEFAULTS)
        if configured_path:
            config_path = Path(configured_path).expanduser()
            try:
                with config_path.open("rb") as handle:
                    document = tomllib.load(handle)
            except FileNotFoundError as exc:
                raise ConfigError(f"configuration file not found: {config_path}") from exc
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
            if "bluesky_agent" in document:
                if set(document) != {"bluesky_agent"}:
                    extra = ", ".join(sorted(set(document) - {"bluesky_agent"}))
                    raise ConfigError(f"unexpected top-level TOML keys: {extra}")
                document = document["bluesky_agent"]
                if not isinstance(document, dict):
                    raise ConfigError("bluesky_agent must be a TOML table")
            known = {field.name for field in fields(cls)}
            unknown = set(document) - known
            if unknown:
                raise ConfigError(f"unknown configuration keys: {', '.join(sorted(unknown))}")
            forbidden = set(document) & _SECRET_FIELDS
            if forbidden:
                names = ", ".join(sorted(forbidden))
                raise ConfigError(f"secrets must be supplied through the environment: {names}")
            values.update(document)

        known = {field.name for field in fields(cls)}
        environment_aliases = {"agent_app_password": "BLUESKY_AGENT_APP_PASSWORD"}
        for name in known:
            environment_name = environment_aliases.get(name, f"{_ENV_PREFIX}{name.upper()}")
            if environment_name in env:
                values[name] = env[environment_name]

        missing = [name for name in sorted(_REQUIRED_FIELDS) if not values.get(name)]
        if missing:
            names = ", ".join(environment_aliases.get(name, f"{_ENV_PREFIX}{name.upper()}") for name in missing)
            raise ConfigError(f"missing required configuration: {names}")

        for name in _FLOAT_FIELDS:
            try:
                values[name] = float(values[name])
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"{name} must be a number") from exc
        values["state_dir"] = Path(str(values["state_dir"])).expanduser()
        for name in ("bluesky_service", "bluesky_public_api", "wiki_site_url"):
            values[name] = str(values[name]).rstrip("/")

        try:
            return cls(**values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ConfigError(str(exc)) from exc
