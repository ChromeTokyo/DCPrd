"""运行配置：来自环境变量 / .env，仅作首次默认值；可在后台修改的项以数据库 settings 表为准。"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 .env 读取（不覆盖已存在的环境变量）。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    domain: str = "localhost:8000"
    tg_bot_token: str = ""
    tg_bot_username: str = "dcprd_bot"
    jira_base_url: str = "https://dcjira.opscom666.com/jira"
    secret_key: str = ""
    max_upload_mb: int = 300
    timezone: str = "Asia/Tokyo"
    data_dir: Path = field(default_factory=lambda: Path("/data"))
    dev_mode: bool = False
    app_version: str = "dev"
    site_name: str = "DCPrd 需求文档"

    # 派生路径
    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "dcpm.sqlite"

    @property
    def docs_dir(self) -> Path:
        return self.data_dir / "docs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def base_url(self) -> str:
        scheme = "http" if self.dev_mode else "https"
        return f"{scheme}://{self.domain}"

    def ensure_dirs(self) -> None:
        for d in (self.db_path.parent, self.docs_dir, self.backups_dir, self.tmp_dir):
            d.mkdir(parents=True, exist_ok=True)

    def ensure_secret_key(self) -> None:
        if self.secret_key:
            return
        key_file = self.data_dir / "secret_key"
        if key_file.is_file():
            self.secret_key = key_file.read_text(encoding="utf-8").strip()
        if not self.secret_key:
            self.secret_key = secrets.token_hex(32)
            key_file.write_text(self.secret_key, encoding="utf-8")
            try:
                key_file.chmod(0o600)
            except OSError:
                pass


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


def load_config() -> Config:
    _load_dotenv(Path(os.environ.get("DOTENV_PATH", ".env")))
    env = os.environ
    cfg = Config(
        domain=env.get("DOMAIN", "localhost:8000"),
        tg_bot_token=env.get("TG_BOT_TOKEN", ""),
        tg_bot_username=env.get("TG_BOT_USERNAME", "dcprd_bot"),
        jira_base_url=env.get("JIRA_BASE_URL", "https://dcjira.opscom666.com/jira").rstrip("/"),
        secret_key=env.get("SECRET_KEY", ""),
        max_upload_mb=int(env.get("MAX_UPLOAD_MB", "300") or 300),
        timezone=env.get("TIMEZONE", "Asia/Tokyo"),
        data_dir=Path(env.get("DATA_DIR", "/data")),
        dev_mode=_truthy(env.get("DEV_MODE")),
        app_version=env.get("APP_VERSION", "dev"),
        site_name=env.get("SITE_NAME", "DCPrd 需求文档"),
    )
    cfg.ensure_dirs()
    cfg.ensure_secret_key()
    return cfg
