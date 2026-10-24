"""从环境变量读取配置。systemd 通过 EnvironmentFile 加载 /etc/jmcomic/jmcomic.env。"""
import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    channel_id: int
    fav_channel_id: int
    owner_id: int
    public_url: str
    data_dir: Path
    http_host: str
    http_port: int
    cache_mb: int
    image_threads: int
    pdf_quality: int
    pdf_volume_mb: int
    jm_username: str
    jm_password: str
    remote_host: str
    remote_port: int
    remote_min_mb: int
    remote_threads: int

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jmshelf.db"

    @property
    def session_path(self) -> Path:
        return self.data_dir / "bot"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def cover_dir(self) -> Path:
        return self.data_dir / "covers"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def remote_key(self) -> Path:
        return self.data_dir / "remote_key"

    @property
    def remote_known_hosts(self) -> Path:
        return self.data_dir / "remote_known_hosts"

    @property
    def remote_socket(self) -> Path:
        return self.data_dir / "remote.sock"


def load() -> Config:
    env = os.environ
    return Config(
        bot_token=env["TG_BOT_TOKEN"],
        api_id=int(env["TG_API_ID"]),
        api_hash=env["TG_API_HASH"],
        channel_id=int(env["TG_CHANNEL_ID"]),
        fav_channel_id=int(env["TG_FAV_CHANNEL_ID"]),
        owner_id=int(env["TG_OWNER_ID"]),
        public_url=env.get("JM_PUBLIC_URL", "").rstrip("/"),
        data_dir=Path(env.get("JM_DATA_DIR", "/var/lib/jmcomic")),
        http_host=env.get("JM_HTTP_HOST", "127.0.0.1"),
        http_port=_int("JM_HTTP_PORT", 8730),
        cache_mb=_int("JM_CACHE_MB", 1024),
        image_threads=_int("JM_IMAGE_THREADS", 4),
        pdf_quality=_int("JM_PDF_QUALITY", 85),
        pdf_volume_mb=_int("JM_PDF_VOLUME_MB", 300),
        jm_username=env.get("JM_USERNAME", "").strip(),
        jm_password=env.get("JM_PASSWORD", ""),
        remote_host=env.get("JM_REMOTE_HOST", "").strip(),
        remote_port=_int("JM_REMOTE_PORT", 22),
        remote_min_mb=_int("JM_REMOTE_MIN_MB", 30),
        remote_threads=_int("JM_REMOTE_THREADS", 16),
    )
