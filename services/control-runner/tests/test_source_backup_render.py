"""infra/envs/source-backup/render.py: one Backup name per T35 run, so a lost response is re-read, not repeated."""

import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "source_backup_render", Path(__file__).resolve().parents[3] / "infra/envs/source-backup/render.py"
)
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)

REG = {"writer": {"namespace": "source-dev", "name": "source-db"},
       "reader": {"namespace": "t35-restore-check", "name": "source-db-restore"}}


def test_same_run_renders_the_same_backup_name():
    first, again = render.backup(REG, "r20261105-a1b2c3"), render.backup(REG, "r20261105-a1b2c3")
    assert first == again
    (request,) = first
    assert request["metadata"]["name"] == "source-db-t35-r20261105-a1b2c3"
    assert request["metadata"]["annotations"] == {"bravo.t35/run-id": "r20261105-a1b2c3"}
    assert request["spec"]["cluster"] == {"name": "source-db"}
    assert render.backup(REG, "r20261105-zzzzzz")[0]["metadata"]["name"] != request["metadata"]["name"]


@pytest.mark.parametrize("run_id", ["", "Upper", "-x", "x-", "a" * 33, "a_b", None])
def test_run_id_is_required_and_dns_safe(run_id):
    with pytest.raises(ValueError):
        render.backup(REG, run_id)


def test_cli_requires_run_id(tmp_path):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(REG))
    with pytest.raises(SystemExit):
        render.main(["--registration", str(reg), "backup"])
    out = io.StringIO()
    with redirect_stdout(out):
        render.main(["--registration", str(reg), "backup", "--run-id", "r1"])
    assert json.loads(out.getvalue())["items"][0]["metadata"]["name"] == "source-db-t35-r1"
