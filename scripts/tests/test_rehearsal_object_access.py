"""T30: rehearsal Pods reach the target object store on 8333, and nothing else opens.

Client labels are read from the T3 namespace template, the T3 isolation fixture and the T15 Job
renderer rather than copied, so a rename on their side fails here instead of surfacing later as a
blocked rehearsal. The evaluator applies NetworkPolicy semantics to the rendered objects; the live
enforcement check is infra/envs/target-workloads/testing/verify_rehearsal_object_access.py.
"""
import copy
import importlib.util
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SEARCH = [str(ROOT / "infra/envs/target-workloads"), str(ROOT / "services/rehearsal-runner/src")]

# objects.py imports a sibling named `render`, a name other infra directories also use.
with patch.object(sys, "path", [*SEARCH, *sys.path]), patch.dict(sys.modules):
    sys.modules.pop("render", None)
    import objects
    from rehearsal_runner import credential_job
    from rehearsal_runner.bucket_job import BucketFixture, SeaweedBucketJob
    from rehearsal_runner.isolation.fixtures import RUN_LABEL, Fixtures
    from rehearsal_runner.isolation.profile import read_env
    target_namespace = objects.database("fixture-node")[0]

spec = importlib.util.spec_from_file_location(
    "target_secrets_render_for_object_access", ROOT / "infra/envs/target-secrets/render.py")
target_secrets = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target_secrets)


def t3_namespace_labels():
    template = (ROOT / "services/rehearsal-runner/templates/00-namespace.yaml").read_text()
    return dict(re.findall(r"^    ([\w./-]+): (\S+)$", template.split("labels:\n", 1)[1], re.MULTILINE))


def t15_job_pod_labels():
    fixture = BucketFixture("http://seaweedfs.target-dev.svc.cluster.local:8333", "rehearsal-attachments",
                            "canary", "0" * 64, "registry.test/aws@sha256:" + "0" * 64)
    job = SeaweedBucketJob(None, None, None, fixture, None)._job(
        None, "bucket-control", "source.json", {"namespace": "rehearsal-run1"})
    return job["spec"]["template"]["metadata"]["labels"]


def t3_pod_labels(role):
    return Fixtures("rehearsal-run1", "run1", "busybox").pod("pod", "rehearsal-run1", role, ["true"])["metadata"]["labels"]


def namespace(name, labels=None):
    return {"kubernetes.io/metadata.name": name, **(labels or {})}


REHEARSAL = namespace("rehearsal-run1", t3_namespace_labels())
TARGET = namespace("target-dev", target_namespace["metadata"]["labels"])
UNLABELLED_REHEARSAL = namespace("rehearsal-unlabelled")
OTHER_PART_OF = namespace("other", {"app.kubernetes.io/part-of": "bravo-target"})
T3_VICTIM = namespace("isolation-victim-run1", {RUN_LABEL: "run1"})
VERIFIER, PROBE = t15_job_pod_labels(), t3_pod_labels("probe")
OBJECT_CLIENT = {"bravo-object-client": "true"}

ALLOWED = {
    "T15 verification Job": (REHEARSAL, VERIFIER),
    "T3 P4 isolation probe": (REHEARSAL, PROBE),
    "operational client in target-dev": (TARGET, OBJECT_CLIENT),
}
DENIED = {
    "unlabelled Pod in a rehearsal namespace": (REHEARSAL, {}),
    "T3 echo Pod in a rehearsal namespace": (REHEARSAL, t3_pod_labels("echo")),
    "candidate application Pod": (REHEARSAL, {"app.kubernetes.io/name": "demo-chat"}),
    "operational label outside target-dev": (REHEARSAL, OBJECT_CLIENT),
    "verifier in a rehearsal-named namespace without the label": (UNLABELLED_REHEARSAL, VERIFIER),
    "probe in a rehearsal-named namespace without the label": (UNLABELLED_REHEARSAL, PROBE),
    "verifier under another part-of value": (OTHER_PART_OF, VERIFIER),
    "probe in the T3 victim namespace": (T3_VICTIM, PROBE),
    "verifier label inside target-dev": (TARGET, VERIFIER),
    "probe label inside target-dev": (TARGET, PROBE),
    "unlabelled Pod inside target-dev": (TARGET, {}),
    "operational label in another namespace": (OTHER_PART_OF, OBJECT_CLIENT),
}
OTHER_SEAWEEDFS_PORTS = (8080, 8888, 9333, 18080, 18888, 19333)


def selects(selector, labels):
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expression in selector.get("matchExpressions", []):
        if expression["operator"] != "Exists":
            raise ValueError("selector operator not modelled by this test")
        if expression["key"] not in labels:
            return False
    return True


def peer_admits(peer, policy_namespace, client_namespace, client_pod):
    if "ipBlock" in peer:
        raise ValueError("ipBlock not modelled by this test")
    if "namespaceSelector" in peer:
        if not selects(peer["namespaceSelector"], client_namespace):
            return False
    elif client_namespace["kubernetes.io/metadata.name"] != policy_namespace:
        return False
    return selects(peer.get("podSelector", {}), client_pod)


def admitted(policies, client_namespace, client_pod, port, server_labels):
    """Whether any policy selecting the server admits this client; none selecting means open."""
    selecting = [p for p in policies if "Ingress" in p["spec"]["policyTypes"]
                 and selects(p["spec"]["podSelector"], server_labels)]
    if not selecting:
        return True
    for policy in selecting:
        for rule in policy["spec"].get("ingress", []):
            ports = rule.get("ports")
            if ports and not any(p["protocol"] == "TCP" and p["port"] == port for p in ports):
                continue
            peers = rule.get("from")
            if not peers or any(peer_admits(peer, policy["metadata"]["namespace"], client_namespace, client_pod)
                                for peer in peers):
                return True
    return False


def rendered():
    items = objects.objects("fixture-node")
    deployment = next(i for i in items if i["kind"] == "Deployment")
    return ([i for i in items if i["kind"] == "NetworkPolicy"],
            deployment["spec"]["template"]["metadata"]["labels"])


def violations(policies, server_labels):
    wrong = [f"blocked: {name}" for name, (ns, pod) in ALLOWED.items()
             if not admitted(policies, ns, pod, 8333, server_labels)]
    wrong += [f"opened: {name}" for name, (ns, pod) in DENIED.items()
              if admitted(policies, ns, pod, 8333, server_labels)]
    wrong += [f"opened port {port}: {name}" for name, (ns, pod) in ALLOWED.items()
              for port in OTHER_SEAWEEDFS_PORTS if admitted(policies, ns, pod, port, server_labels)]
    return wrong


def rehearsal_rule(policies):
    return next(p for p in policies if p["metadata"]["name"] == "target-object-rehearsal-clients")["spec"]["ingress"][0]


def namespace_only(rule):
    for peer in rule["from"]:
        peer.pop("podSelector")


def split_peer_into_or(rule):
    rule["from"] = [{key: peer[key]} for peer in rule["from"] for key in ("namespaceSelector", "podSelector")]


def any_namespace(rule):
    for peer in rule["from"]:
        peer["namespaceSelector"] = {}


def any_pod(rule):
    for peer in rule["from"]:
        peer["podSelector"] = {}


def every_source(rule):
    rule.pop("from")


def every_port(rule):
    rule.pop("ports")


def another_port(rule):
    rule["ports"].append({"protocol": "TCP", "port": 8888})


def only_verifier(rule):
    rule["from"] = rule["from"][:1]


def only_probe(rule):
    rule["from"] = rule["from"][1:]


def empty_sources(rule):
    rule["from"] = []  # Kubernetes reads an empty list as "every source", not "none".


def empty_ports(rule):
    rule["ports"] = []


WIDENING = (namespace_only, split_peer_into_or, any_namespace, any_pod, every_source, empty_sources, every_port,
            empty_ports, another_port)
NARROWING = (only_verifier, only_probe)


class RehearsalObjectAccessTests(unittest.TestCase):
    def test_only_the_intended_clients_reach_only_the_s3_port(self):
        policies, server_labels = rendered()
        self.assertEqual(violations(policies, server_labels), [])

    def test_the_matrix_names_what_each_mutation_opens_or_blocks(self):
        policies, server_labels = rendered()
        for mutations, expected in ((WIDENING, "opened"), (NARROWING, "blocked")):
            for mutate in mutations:
                with self.subTest(mutate.__name__):
                    mutated = copy.deepcopy(policies)
                    mutate(rehearsal_rule(mutated))
                    self.assertTrue(any(v.startswith(expected) for v in violations(mutated, server_labels)))

    def test_operational_policy_keeps_its_exact_shape(self):
        policies, _ = rendered()
        operational = next(p for p in policies if p["metadata"]["name"] == "target-object-clients")
        self.assertEqual(operational["spec"], {
            "podSelector": {"matchLabels": {"app.kubernetes.io/name": "target-seaweedfs"}},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [{"from": [{"podSelector": {"matchLabels": {"bravo-object-client": "true"}}}],
                         "ports": [{"protocol": "TCP", "port": 8333}]}],
            "egress": [],
        })

    def test_rehearsal_policy_adds_ingress_only(self):
        policies, _ = rendered()
        policy = next(p for p in policies if p["metadata"]["name"] == "target-object-rehearsal-clients")
        self.assertEqual(policy["metadata"]["namespace"], "target-dev")
        self.assertEqual(policy["metadata"]["labels"]["component"], "target")
        self.assertEqual(policy["spec"]["policyTypes"], ["Ingress"])
        self.assertNotIn("egress", policy["spec"])
        self.assertEqual(policy["spec"]["podSelector"], {"matchLabels": objects.SELECTOR})

    def test_namespace_selector_is_the_label_t3_stamps_and_t9_uses(self):
        policies, _ = rendered()
        for peer in rehearsal_rule(policies)["from"]:
            self.assertEqual(peer["namespaceSelector"], {"matchLabels": target_secrets.REHEARSAL_LABEL})
            self.assertTrue(selects(peer["namespaceSelector"], REHEARSAL))

    def test_t3_onprem_egress_points_at_the_same_pod_and_port(self):
        profile = read_env(ROOT / "services/rehearsal-runner/profiles/onprem.env")
        self.assertEqual(profile["SEAWEEDFS_NAMESPACE"], "target-dev")
        self.assertEqual({profile["SEAWEEDFS_LABEL_KEY"]: profile["SEAWEEDFS_LABEL_VALUE"]}, objects.SELECTOR)
        policies, _ = rendered()
        self.assertEqual(rehearsal_rule(policies)["ports"],
                         [{"protocol": "TCP", "port": int(profile["SEAWEEDFS_PORT"])}])

    def test_existing_positions_stay_for_callers(self):
        items = objects.objects("fixture-node")
        self.assertEqual(items[3]["kind"], "Deployment")  # scripts/tests/attachment_containers.py
        self.assertEqual(items[4]["metadata"]["name"], "target-object-clients")



def t15_credential_labels():
    """Labels T15 puts on the candidate app Pod and on verifier Job Pods (credential_job.py)."""
    app = credential_job._pod({"namespace": "rehearsal-run1", "labels": {}}, credential_job.APP_NAME,
                              "sa", {"name": "run", "image": "x"}, [])["metadata"]["labels"]
    source = Path(credential_job.__file__).read_text()
    # The verifier Job template label is inline in CredentialJob._job.
    assert '"metadata": {"labels": {"role": "credential-verifier", **(labels or {})}}' in source
    return app, {"role": "credential-verifier"}


CANDIDATE, CREDENTIAL_VERIFIER = t15_credential_labels()
CONTROL = namespace(target_secrets.WRITER_NAMESPACE,
                    target_secrets.rehearsal_writer_identity()["items"][0]["metadata"]["labels"])
OPENBAO_SERVER = {"app.kubernetes.io/name": "openbao", "app.kubernetes.io/instance": target_secrets.RELEASE}
OPENBAO_ALLOWED = {
    "candidate init container in an attempt namespace": (REHEARSAL, CANDIDATE),
    "T15 verifier / denial-test Job in an attempt namespace": (REHEARSAL, CREDENTIAL_VERIFIER),
    # Namespace-wide on purpose (#402): OpenBao, not the network, decides what a Pod may read.
    "unlabelled Pod in an attempt namespace": (REHEARSAL, {}),
    "writer Job in the management namespace": (CONTROL, {**CREDENTIAL_VERIFIER, **target_secrets.WRITER_POD_LABEL}),
}
OPENBAO_DENIED = {
    "candidate in a rehearsal-named namespace without the label": (UNLABELLED_REHEARSAL, CANDIDATE),
    "verifier in a rehearsal-named namespace without the label": (UNLABELLED_REHEARSAL, CREDENTIAL_VERIFIER),
    "candidate label in target-dev": (TARGET, CANDIDATE),
    "verifier label in target-dev": (TARGET, CREDENTIAL_VERIFIER),
    "verifier without the writer label in the management namespace": (CONTROL, CREDENTIAL_VERIFIER),
    "candidate label in the management namespace": (CONTROL, CANDIDATE),
    "verifier under another part-of value": (OTHER_PART_OF, CREDENTIAL_VERIFIER),
    "T3 probe in the T3 victim namespace": (T3_VICTIM, PROBE),
}


class OpenBaoRehearsalIngressTests(unittest.TestCase):
    """T9 OpenBao ingress for T15 attempt namespaces (#402), tied to T15 and T3."""

    def policies(self):
        return [target_secrets.network("10.96.0.1", "192.168.50.10")]

    def test_attempt_namespaces_and_the_writer_reach_only_8200(self):
        for name, (client_namespace, pod) in OPENBAO_ALLOWED.items():
            with self.subTest(allowed=name):
                self.assertTrue(admitted(self.policies(), client_namespace, pod, 8200, OPENBAO_SERVER))
                self.assertFalse(admitted(self.policies(), client_namespace, pod, 8201, OPENBAO_SERVER))
        for name, (client_namespace, pod) in OPENBAO_DENIED.items():
            with self.subTest(denied=name):
                self.assertFalse(admitted(self.policies(), client_namespace, pod, 8200, OPENBAO_SERVER))

    def test_the_management_namespace_is_not_an_attempt_namespace(self):
        self.assertNotEqual(CONTROL.get("app.kubernetes.io/part-of"), "bravo-rehearsal")

    def test_t3_onprem_egress_points_at_the_same_openbao_pod_and_port(self):
        profile = read_env(ROOT / "services/rehearsal-runner/profiles/onprem.env")
        self.assertEqual(profile["OPENBAO_NAMESPACE"], target_secrets.NAMESPACE)
        self.assertTrue(selects({"matchLabels": {profile["OPENBAO_LABEL_KEY"]: profile["OPENBAO_LABEL_VALUE"]}},
                                OPENBAO_SERVER))
        self.assertEqual(int(profile["OPENBAO_PORT"]), 8200)
        self.assertEqual(t3_namespace_labels().get("app.kubernetes.io/part-of"),
                         target_secrets.REHEARSAL_LABEL["app.kubernetes.io/part-of"])

if __name__ == "__main__":
    unittest.main()
