"""Small diagnostic for validating the configured OpenAI-compatible endpoint."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from music_companion.ai_client import _build_ssl_context


def _load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def main() -> int:
    _load_env()
    api_key = os.getenv("OPENAI_API_KEY", "")
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        print("OPENAI_API_KEY is empty")
        return 2

    print(f"Testing base_url={base_url} model={model} key_prefix={api_key[:8]}...")
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "只回复：ok"}],
            "max_tokens": 20,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20, context=_build_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            print("OK:", content)
            return 0
    except urllib.error.HTTPError as exc:
        print("HTTP", exc.code, exc.read().decode("utf-8", errors="replace")[:500])
        return 1
    except Exception as exc:
        print(type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
