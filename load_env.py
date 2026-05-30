import os
from pathlib import Path

def load_dotenv(env_path: str | Path | None = None) -> bool:
    """自动加载同目录下的 civitai-bot.env（如果存在）"""
    if env_path is None:
        env_path = Path(__file__).parent / "civitai-bot.env"
    else:
        env_path = Path(env_path)

    if not env_path.exists():
        return False

    with open(env_path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if (value.startswith(') and value.endswith(')) or (value.startswith(") and value.endswith(")):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    return True

load_dotenv()
