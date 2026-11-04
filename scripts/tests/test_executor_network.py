"""Network rules require the caller's namespace AND Pod labels on the DB port."""
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("executor_network", ROOT / "infra/envs/executor-network/render.py")
render = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render)


def permits(rules, direction, namespace, labels, protocol, port):
    """Evaluate only matchLabels peers used here, independent of the renderer."""
    for rule in rules:
        if not any(p["protocol"] == protocol and p["port"] == port for p in rule["ports"]):
            continue
        for peer in rule[direction]:
            expected_namespace = peer["namespaceSelector"]["matchLabels"]
            expected_labels = peer["podSelector"]["matchLabels"]
            if expected_namespace == {"kubernetes.io/metadata.name": namespace} and all(
                labels.get(key) == value for key, value in expected_labels.items()
            ):
                return True
    return False


class ExecutorNetworkTests(unittest.TestCase):
    def setUp(self):
        self.policies = {p["metadata"]["namespace"]: p for p in render.state_database()}

    def test_db_ingress_rejects_matching_label_in_another_namespace(self):
        rules = self.policies["platform-state"]["spec"]["ingress"]
        caller = {"app.kubernetes.io/name": "cutover-executor"}
        self.assertTrue(permits(rules, "from", "bravo-platform", caller, "TCP", 5432))
        cases = [("target-dev", caller, "TCP", 5432),
                 ("bravo-platform", {"app.kubernetes.io/name": "collector"}, "TCP", 5432),
                 ("bravo-platform", caller, "TCP", 5433),
                 ("bravo-platform", caller, "UDP", 5432)]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(permits(rules, "from", *case))

    def test_executor_egress_allows_state_db_and_dns_but_no_other_database(self):
        policy = self.policies["bravo-platform"]["spec"]
        self.assertEqual(policy["podSelector"], {"matchLabels": {"app.kubernetes.io/name": "cutover-executor"}})
        rules = policy["egress"]
        self.assertTrue(permits(rules, "to", "platform-state", {"cnpg.io/cluster": "platform-db"}, "TCP", 5432))
        for protocol in ("TCP", "UDP"):
            self.assertTrue(permits(rules, "to", "kube-system", {"k8s-app": "kube-dns"}, protocol, 53))
        cases = [("source-dev", {"cnpg.io/cluster": "platform-db"}, "TCP", 5432),
                 ("platform-state", {"cnpg.io/cluster": "source-db"}, "TCP", 5432),
                 ("bravo-platform", {"k8s-app": "kube-dns"}, "UDP", 53),
                 ("kube-system", {"k8s-app": "kube-dns"}, "TCP", 443)]
        for case in cases:
            with self.subTest(case=case):
                self.assertFalse(permits(rules, "to", *case))


if __name__ == "__main__":
    unittest.main()
