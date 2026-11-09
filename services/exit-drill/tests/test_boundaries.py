"""설계 원칙을 코드로 지킨다: 판정에 LLM을 쓰지 않고, 도구 안에 실행 · 파괴 명령을 두지 않는다.

검사기가 실제로 잡아내는지도 함께 시험한다. 항상 통과하는 검사가 되지 않게 하기 위해서다.
"""

import ast
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "bravo_exit_drill"

LLM_MODULES = {
    "anthropic", "openai", "claude_agent_sdk", "claude_code_sdk", "langchain", "langgraph",
    "llama_index", "litellm", "vertexai", "mistralai", "cohere", "groq", "ollama", "transformers",
}
LLM_DIST_PREFIXES = ("anthropic", "openai", "claude-", "langchain", "langgraph", "llama-index", "litellm", "google-genai", "google-generativeai", "mistralai", "cohere", "groq", "ollama", "transformers")
# 이 도구는 계획과 판정을 내놓을 뿐이다. 프로세스 실행 · 클러스터 접속 라이브러리를 들이지 않는다.
EXECUTION_MODULES = {"subprocess", "kubernetes", "boto3", "botocore", "paramiko", "pty", "shutil"}
# 접속은 probe.py만 한다.
NETWORK_MODULES = {"socket", "http", "urllib", "requests", "httpx", "ssl", "asyncio"}
DESTRUCTIVE = re.compile(
    r"rm\s+-rf|\bDROP\s+(TABLE|DATABASE|SUBSCRIPTION|PUBLICATION|ROLE)\b|\bTRUNCATE\b|terraform\s+destroy"
    r"|kubectl\s+(delete|drain|replace)|delete\s+namespace|helm\s+uninstall|s3\s+rb\b|\.delete_bucket|docker\s+rm",
    re.IGNORECASE,
)


def imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {"import_module", "__import__", "system", "popen", "exec", "eval"}:
            names.add("<dynamic>")
    return names


def source_files() -> list[Path]:
    return sorted(SRC.glob("*.py"))


def test_there_is_source_to_check():
    assert len(source_files()) >= 6


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_llm_and_no_execution_imports(path):
    found = imported_modules(path.read_text(encoding="utf-8"))
    assert not found & LLM_MODULES
    assert not found & EXECUTION_MODULES
    assert "<dynamic>" not in found


@pytest.mark.parametrize("path", [p for p in source_files() if p.name != "probe.py"], ids=lambda p: p.name)
def test_only_probe_touches_the_network(path):
    assert not imported_modules(path.read_text(encoding="utf-8")) & NETWORK_MODULES


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_destructive_command_in_tool(path):
    assert not DESTRUCTIVE.search(path.read_text(encoding="utf-8"))


def test_operator_script_has_no_llm_and_no_destructive_command():
    """kubectl을 부르는 곳은 src 밖의 운영자 도구뿐이다. 그래도 판정 쪽 규칙은 같다."""
    source = (ROOT / "scripts" / "real_run.py").read_text(encoding="utf-8")
    assert not imported_modules(source) & LLM_MODULES
    assert not DESTRUCTIVE.search(source)


def test_lock_has_no_llm_dependency():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    names = {package["name"] for package in lock["package"]}
    assert "bravo-exit-drill" in names  # 잠금 파일을 제대로 읽었는지 확인
    assert not [n for n in names if n.startswith(LLM_DIST_PREFIXES)]


@pytest.mark.parametrize(
    ("source", "wanted"),
    [
        ("import anthropic", "anthropic"),
        ("from openai.types import X", "openai"),
        ("import subprocess", "subprocess"),
        ("import importlib\nimportlib.import_module('openai')", "<dynamic>"),
        ("import os\nos.system('x')", "<dynamic>"),
    ],
)
def test_import_checker_catches_violations(source, wanted):
    assert wanted in imported_modules(source)


@pytest.mark.parametrize("text", ["kubectl delete networkpolicy x", "rm -rf /", "DROP SUBSCRIPTION s", "terraform destroy"])
def test_destructive_checker_catches_commands(text):
    assert DESTRUCTIVE.search(text)
