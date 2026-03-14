from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


def _config_candidates() -> list[Path]:
    base = Path(__file__).resolve().parent
    return [base / ".." / "config" / "config.json", base / "config" / "config.json"]


def _resolve_config_dir() -> Path:
    for path in _config_candidates():
        if path.exists():
            return path.parent.resolve()
    return (_config_candidates()[0].parent).resolve()


def _load_raw_config() -> dict:
    candidates = _config_candidates()
    for path in candidates:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
    searched = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"Missing config.json. Searched: {searched}")


def _resolve_path(raw_path: str, domain: str | None = None) -> str:
    resolved = raw_path.replace("${config}", str(_resolve_config_dir()))
    if domain:
        resolved = resolved.replace("${domain}", domain)
    return resolved


def _to_string_list(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _to_lower_string_list(values: list[str]) -> list[str]:
    return [value.strip().lower() for value in values if value.strip()]


def _load_sleep_range(cfg: dict, key: str, default: tuple[float, float]) -> tuple[float, float]:
    raw = cfg.get(key, list(default))
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError(f"{key} must be a two-item JSON array.")
    return (float(raw[0]), float(raw[1]))


@dataclass(frozen=True)
class ImapConfig:
    host: str
    port: int
    username: str
    password: str
    filter_recipient: list[str]

    @property
    def allowed_froms(self) -> list[str]:
        return _to_lower_string_list(self.filter_recipient)


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str | None
    from_header: str


@dataclass(frozen=True)
class DbConfig:
    db_path: str

    @property
    def resolved_path(self) -> str:
        return _resolve_path(self.db_path)


@dataclass(frozen=True)
class WebConfig:
    bind: str
    port: int
    domain: str
    tls_cert: str
    tls_key: str
    public_base_url: str
    token_secret: str
    unsubscribe_path: str
    manage_path: str
    admin_user: str
    admin_pass_bcrypt: str

    @property
    def resolved_tls_cert(self) -> str:
        return _resolve_path(self.tls_cert, self.domain)

    @property
    def resolved_tls_key(self) -> str:
        return _resolve_path(self.tls_key, self.domain)


@dataclass(frozen=True)
class RelayConfig:
    poll_seconds: int
    batch_size: int
    per_recipient_sleep_seconds: tuple[float, float]
    per_message_sleep_seconds: tuple[float, float]
    between_batches_sleep_seconds: tuple[float, float]


@dataclass(frozen=True)
class TestConfig:
    enabled: bool
    contacts: list[str]
    test_db: str | None

    @property
    def normalized_contacts(self) -> list[str]:
        return _to_lower_string_list(self.contacts)

    @property
    def resolved_db_path(self) -> str | None:
        if not self.test_db:
            return None
        return _resolve_path(self.test_db)


@dataclass(frozen=True)
class AppConfig:
    imap: ImapConfig
    smtp: SmtpConfig
    db: DbConfig
    web: WebConfig
    relay: RelayConfig
    test: TestConfig

    @property
    def resolved_db_path(self) -> str:
        if self.test.enabled and self.test.resolved_db_path:
            return self.test.resolved_db_path
        return self.db.resolved_path

    @classmethod
    def load(cls) -> AppConfig:
        raw = _load_raw_config()
        imap_raw = raw["imap"]
        smtp_raw = raw["smtp"]
        db_raw = raw["db"]
        web_raw = raw["web"]
        relay_raw = raw["relay"]
        test_raw = raw.get("test", {})
        return cls(
            imap=ImapConfig(
                host=imap_raw["host"],
                port=int(imap_raw["port"]),
                username=imap_raw["username"],
                password=imap_raw["password"],
                filter_recipient=_to_string_list(imap_raw["filter_recipient"]),
            ),
            smtp=SmtpConfig(
                host=smtp_raw["host"],
                port=int(smtp_raw["port"]),
                username=smtp_raw["username"],
                password=smtp_raw.get("password"),
                from_header=smtp_raw["from"],
            ),
            db=DbConfig(db_path=db_raw["db_path"]),
            web=WebConfig(
                bind=web_raw["bind"],
                port=int(web_raw["port"]),
                domain=web_raw["domain"],
                tls_cert=web_raw["tls_cert"],
                tls_key=web_raw["tls_key"],
                public_base_url=web_raw["public_base_url"],
                token_secret=web_raw["token_secret"],
                unsubscribe_path=web_raw["unsubscribe_path"],
                manage_path=web_raw["manage_path"],
                admin_user=web_raw["admin_user"],
                admin_pass_bcrypt=web_raw["admin_pass_bcrypt"],
            ),
            relay=RelayConfig(
                poll_seconds=int(relay_raw["poll_seconds"]),
                batch_size=int(relay_raw.get("batch_size", 50)),
                per_recipient_sleep_seconds=_load_sleep_range(
                    relay_raw, "per_recipient_sleep_seconds", (25.0, 40.0)
                ),
                per_message_sleep_seconds=_load_sleep_range(
                    relay_raw, "per_message_sleep_seconds", (5.0, 12.0)
                ),
                between_batches_sleep_seconds=_load_sleep_range(
                    relay_raw, "between_batches_sleep_seconds", (300.0, 900.0)
                ),
            ),
            test=TestConfig(
                enabled=bool(test_raw.get("enabled", False)),
                contacts=_to_string_list(test_raw.get("contacts", [])),
                test_db=test_raw.get("test_db"),
            ),
        )


def load_config() -> AppConfig:
    return AppConfig.load()
