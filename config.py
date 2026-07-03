from __future__ import annotations
import os
from dataclasses import dataclass

@dataclass
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    session_string: str
    admin_id: int | None
    max_duration: int = 600
    max_threads: int = 250
    scan_limit: int = 50
    scan_cooldown: int = 5

    @classmethod
    def from_env(cls):
        # Railway pe direct os.getenv() kaam karega
        required = ["API_ID", "API_HASH", "BOT_TOKEN", "SESSION_STRING"]
        missing = [k for k in required if not os.getenv(k)]
        if missing:
            raise ValueError(f"Missing env vars: {missing}")
        
        return cls(
            api_id=int(os.getenv("API_ID")),
            api_hash=os.getenv("API_HASH"),
            bot_token=os.getenv("BOT_TOKEN"),
            session_string=os.getenv("SESSION_STRING"),
            admin_id=int(os.getenv("ADMIN_ID")) if os.getenv("ADMIN_ID") else None,
            max_duration=int(os.getenv("MAX_DURATION", 600)),
            max_threads=int(os.getenv("MAX_THREADS", 250)),
            scan_limit=int(os.getenv("SCAN_LIMIT", 50)),
            scan_cooldown=int(os.getenv("SCAN_COOLDOWN", 5))
        )
