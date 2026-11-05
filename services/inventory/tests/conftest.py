from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def repo() -> Path:
    return FIXTURES / "demo-chat"


@pytest.fixture
def exports() -> Path:
    return FIXTURES / "exports" / "aws"


@pytest.fixture
def profiles() -> list[Path]:
    root = Path(__file__).resolve().parents[1] / "profiles"
    return [root / "onprem-rke2.json", root / "onprem-minimal.json"]
