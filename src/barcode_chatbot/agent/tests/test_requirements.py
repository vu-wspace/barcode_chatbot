import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _name(spec: str) -> str:
    # 'uvicorn[standard]>=0.38.0' -> 'uvicorn'
    return re.split(r'[\[<>=!~ ]', spec.strip(), maxsplit=1)[0].lower()


def test_requirements_txt_matches_pyproject():
    # requirements.txt (cho người dùng pip) phải khớp pyproject.toml (nguồn chính, dùng với uv)
    deps = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']['dependencies']
    reqs = [line for line in (ROOT / 'requirements.txt').read_text(encoding='utf-8').splitlines()
            if line.strip() and not line.startswith(('#', '-'))]
    assert {_name(d) for d in deps} == {_name(r) for r in reqs}
