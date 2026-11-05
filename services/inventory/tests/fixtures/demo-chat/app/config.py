import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_env: str
    db_host: str
    db_port: int
    db_name: str
    db_secret_id: str
    s3_bucket: str
    s3_endpoint_url: str | None
    s3_force_path_style: bool
    admin_token: str | None
    poll_interval: float = 1.0
    tx_timeout_ms: int = 200
    retry_after: int = 2
    db_wait_seconds: float = 5
    max_attachment_bytes: int = 10 * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        return cls(
            app_env=env.get("APP_ENV", "local"),
            db_host=env["DB_HOST"],
            db_port=int(env.get("DB_PORT", "5432")),
            db_name=env.get("DB_NAME", "chat"),
            db_secret_id=env["DB_SECRET_ID"],
            s3_bucket=env["S3_BUCKET"],
            s3_endpoint_url=env.get("S3_ENDPOINT_URL") or None,
            s3_force_path_style=env.get("S3_FORCE_PATH_STYLE", "false") == "true",
            admin_token=env.get("ADMIN_TOKEN") or None,
            poll_interval=float(env.get("POLL_INTERVAL", "1.0")),
            tx_timeout_ms=int(env.get("TX_TIMEOUT_MS", "200")),
            retry_after=int(env.get("WRITE_BLOCK_RETRY_AFTER", "2")),
            db_wait_seconds=float(env.get("DB_WAIT_SECONDS", "5")),
        )
