"""판정 · 실행 모듈은 LLM 클라이언트를 쓰지 않는다 (AGENTS.md 설계 원칙, ADR-0006).

세 겹으로 본다: 소스의 import 문, 잠금 파일의 전체 의존성(간접 의존 포함), 실제로 import된 모듈.
검사기 자체가 잡아내는지도 시험한다. 항상 통과하는 검사가 되지 않게 하기 위해서다.
"""

import ast
import importlib
import pkgutil
import sys
import tomllib
from pathlib import Path

import pytest

import bravo_cutover

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "bravo_cutover"

# import 이름 (최상위 모듈)
LLM_MODULES = {
    "anthropic",
    "openai",
    "claude_agent_sdk",
    "claude_code_sdk",
    "langchain",
    "langchain_core",
    "langchain_community",
    "langchain_openai",
    "langchain_anthropic",
    "langgraph",
    "llama_index",
    "litellm",
    "vertexai",
    "mistralai",
    "cohere",
    "groq",
    "ollama",
    "transformers",
}
# 최상위가 공용 이름이라 하위 모듈로 본다
LLM_SUBMODULES = ("google.generativeai", "google.genai")
# 배포 패키지 이름 (uv.lock, 정규화한 이름)
LLM_DISTS = {
    "anthropic",
    "openai",
    "claude-agent-sdk",
    "claude-code-sdk",
    "langgraph",
    "litellm",
    "google-generativeai",
    "google-genai",
    "google-cloud-aiplatform",
    "mistralai",
    "cohere",
    "groq",
    "ollama",
    "transformers",
}
LLM_DIST_PREFIXES = ("langchain", "llama-index")


def _is_llm_module(name: str) -> bool:
    return name.split(".")[0] in LLM_MODULES or any(
        name == sub or name.startswith(sub + ".") for sub in LLM_SUBMODULES
    )


def forbidden_imports(source: str) -> list[str]:
    """소스에서 LLM 모듈 import와 Bedrock 클라이언트 생성을 찾는다."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _is_llm_module(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if _is_llm_module(node.module):
                found.append(node.module)
            elif any(_is_llm_module(f"{node.module}.{a.name}") for a in node.names):
                found.append(node.module)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            arg = node.args[0]
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                continue
            if name in {"import_module", "__import__"} and _is_llm_module(arg.value):
                found.append(arg.value)
            # boto3.client("bedrock-runtime") 같은 클라이언트 생성
            if name in {"client", "resource"} and "bedrock" in arg.value:
                found.append(arg.value)
    return found


def _normalize(dist: str) -> str:
    return dist.lower().replace("_", "-").replace(".", "-")


def forbidden_dists(lock_text: str) -> list[str]:
    names = [_normalize(p["name"]) for p in tomllib.loads(lock_text).get("package", [])]
    return [n for n in names if n in LLM_DISTS or n.startswith(LLM_DIST_PREFIXES)]


def test_source_has_no_llm_imports():
    files = sorted(SRC.rglob("*.py"))
    assert files
    found = {str(f.relative_to(ROOT)): forbidden_imports(f.read_text(encoding="utf-8")) for f in files}
    assert {f: v for f, v in found.items() if v} == {}


def test_lock_has_no_llm_packages():
    # 잠금 파일은 간접 의존까지 담는다. 없으면 검사할 수 없으므로 실패로 본다.
    assert forbidden_dists((ROOT / "uv.lock").read_text(encoding="utf-8")) == []


def test_importing_package_loads_no_llm_module():
    for mod in pkgutil.walk_packages(bravo_cutover.__path__, "bravo_cutover."):
        importlib.import_module(mod.name)
    assert sorted(m for m in sys.modules if _is_llm_module(m)) == []


@pytest.mark.parametrize(
    "source",
    [
        "import openai",
        "from anthropic import Anthropic",
        "import google.genai as genai",
        "from google import genai",
        "from langchain_core.messages import HumanMessage",
        "import claude_agent_sdk",
        'importlib.import_module("openai")',
        'boto3.client("bedrock-runtime")',
    ],
)
def test_checker_catches_llm_usage(source):
    assert forbidden_imports(source)


def test_checker_allows_executor_dependencies():
    source = "import psycopg\nimport boto3\nfrom google.protobuf import json_format\nboto3.client('secretsmanager')\n"
    assert forbidden_imports(source) == []


def test_lock_checker_catches_llm_packages():
    lock = '[[package]]\nname = "fastapi"\n[[package]]\nname = "OpenAI"\n[[package]]\nname = "langchain_text_splitters"\n'
    assert forbidden_dists(lock) == ["openai", "langchain-text-splitters"]
