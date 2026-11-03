"""알림 규칙마다 런북 절이 있다. promtool 단위 시험은 testing/ 스크립트가 돌린다(README)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RULES = (ROOT / "services/observability/deploy/platform/rules/alerts.yml").read_text()
RUNBOOKS = (ROOT / "docs/observability/runbooks.md").read_text()


def test_every_alert_links_to_an_existing_runbook_section():
    alerts = re.findall(r"^\s+- alert: (\S+)$", RULES, re.M)
    links = re.findall(r"runbook: docs/observability/runbooks\.md#(\S+)$", RULES, re.M)
    headings = {h.lower() for h in re.findall(r"^## (\S+)$", RUNBOOKS, re.M)}
    assert alerts and len(links) == len(alerts)
    assert [a.lower() for a in alerts] == links
    assert set(links) <= headings
