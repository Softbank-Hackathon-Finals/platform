import json

import pytest

from bravo_cutover.remote import Unconfirmed
from bravo_cutover.remote.kube import WriteBlock


class Kube:
    def __init__(self, value, *, changed=False):
        self.value, self.changed, self.calls = value, changed, []

    def running_pods(self, namespace, selector):
        return ["app-2"] if self.changed and self.calls else ["app-1"]

    def exec(self, namespace, pod, argv):
        self.calls.append((namespace, pod, argv))
        assert 'os.environ["ADMIN_TOKEN"]' in argv[2]
        return json.dumps(self.value)


def test_close_reports_ack_without_reading_app_token():
    kube = Kube({"closed": 3})
    assert WriteBlock(kube, "apps", "app=demo-chat").close_websockets() == {"app-1": 3}
    assert len(kube.calls) == 1


@pytest.mark.parametrize("value", [{}, {"closed": -1}, {"closed": True}, {"closed": "1"}])
def test_invalid_ack_is_unconfirmed(value):
    with pytest.raises(Unconfirmed):
        WriteBlock(Kube(value), "apps", "app=demo-chat").close_websockets()


def test_pod_replacement_during_close_is_unconfirmed():
    with pytest.raises(Unconfirmed, match="changed"):
        WriteBlock(Kube({"closed": 1}, changed=True), "apps", "app=demo-chat").close_websockets()
