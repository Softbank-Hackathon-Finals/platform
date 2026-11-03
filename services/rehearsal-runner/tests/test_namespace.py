import pytest

from rehearsal_runner.isolation.profile import read_env
from rehearsal_runner.namespace import TEMPLATES, render

PROFILE = read_env(TEMPLATES.parent / "profiles" / "k3d.env")


def test_renders_every_template_without_leftover_placeholders():
    out = render("rehearsal-plan1-1", PROFILE)
    assert "${" not in out
    assert "name: rehearsal-plan1-1" in out
    assert "app.kubernetes.io/name: seaweedfs-s3" in out
    assert out.count("kind: NetworkPolicy") == 4


def test_missing_profile_value_fails_instead_of_rendering_blank():
    partial = {k: v for k, v in PROFILE.items() if k != "OPENBAO_PORT"}
    with pytest.raises(KeyError):
        render("rehearsal-plan1-1", partial)


@pytest.mark.parametrize("name", ["Rehearsal", "rehearsal_1", "-x", "a" * 64, "a;rm"])
def test_rejects_invalid_namespace_names(name):
    with pytest.raises(ValueError):
        render(name, PROFILE)
