from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    database_url: str
    admin_token: str
    docs: bool = False
    max_body_bytes: int = 2 * 1024 * 1024

    @classmethod
    def from_env(cls):
        url = os.environ.get("DATABASE_URL", "")
        token = os.environ.get("CLOUD_ADMIN_TOKEN", "")
        if not url or len(token) < 32:
            raise RuntimeError("Set DATABASE_URL and a CLOUD_ADMIN_TOKEN of at least 32 characters")
        return cls(url, token, os.environ.get("CLOUD_DOCS", "false").lower() == "true")
