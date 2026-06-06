import os
from pathlib import Path

def load_dotenv(env_path: str | Path | None = None) -> bool:
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
            if line.startswith("export "):
                line = line[7:]
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    return True

load_dotenv()
