"""Local-only configuration. Secrets are never included in API responses."""
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
except ImportError:
    pass

from .campaign import configure_environment
configure_environment()


def local_key():
    explicit = os.getenv('DEEPSEEK_API_KEY', '')
    if explicit:
        return explicit.strip()
    source = ROOT / 'api密钥.rtfd' / 'TXT.rtf'
    if source.exists():
        match = re.search(r'sk-[A-Za-z0-9_-]{20,}', source.read_text(errors='ignore'))
        if match:
            return match.group(0)
    return ''


@dataclass
class Settings:
    frontend_dir: Path = Path(os.getenv('APP_FRONTEND_DIST', str(ROOT / 'frontend' / 'dist')))
    db_path: Path = Path(os.getenv('APP_DB_PATH', str(ROOT / 'data' / 'shop.sqlite3')))
    upload_dir: Path = Path(os.getenv('APP_UPLOAD_DIR', str(ROOT / 'data' / 'uploads')))
    deepseek_api_key: str = local_key()
    deepseek_base_url: str = os.getenv('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
    deepseek_model: str = os.getenv('DEEPSEEK_MODEL', 'deepseek-flash')
    model_timeout: float = float(os.getenv('MODEL_TIMEOUT', '90'))
    agent_enabled: bool = os.getenv('AGENT_ENABLED', '1').lower() not in ('0', 'false', 'off')


settings = Settings()
