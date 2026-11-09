import copy
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("readback", Path(__file__).parents[1] / "t7_runtime_readback.py")
readback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(readback)
host_spec = importlib.util.spec_from_file_location("host", Path(__file__).parents[1] / "t7_runtime_checks.py")
host = importlib.util.module_from_spec(host_spec)
host_spec.loader.exec_module(host)


class RuntimeReadbackTests(unittest.TestCase):
    def host(self):
        return {"schema_version": 2, "cloud_init_status": "done", "bootstrap_marker_exists": True,
                "rke2_service_active": True, "rke2_version": "v1.35.9+rke2r1", "cis_profile_configured": True,
                "kubeconfig_owner_only": True, "api_ready": True, "nodes_query_succeeded": True,
                "node_count": 1, "ready_node_count": 1, "system_pods_query_succeeded": True,
                "system_pod_count": 10, "system_pods_pending_or_failed": 0, "system_pods_not_ready": 0,
                "smoke_can_get_secret": "no", "smoke_can_create_configmap": "yes", "smoke_can_write_other_namespace": "no",
                "ghcr_http_status": "401", "human_kubernetes_login_verified": False,
                "ghcr_image_pull_verified": False, "cis_benchmark_verified": False}

    def test_ready_host_does_not_claim_person_login_pull_or_benchmark(self):
        body = self.host(); readback.validate_host(body)
        self.assertTrue(readback.operator_path_ready(body))
        for key in ("human_kubernetes_login_verified", "ghcr_image_pull_verified", "cis_benchmark_verified"):
            self.assertFalse(body[key])
            invalid = {**body, key: True}
            with self.assertRaisesRegex(readback.Failure, "overclaims"):
                readback.validate_host(invalid)

    def test_unknown_or_failed_kubernetes_check_is_not_ready(self):
        for patch in ({"api_ready": False}, {"cloud_init_status": "error"}, {"node_count": 0, "ready_node_count": 0}, {"ready_node_count": 0},
                      {"nodes_query_succeeded": False}, {"system_pod_count": 0}, {"system_pods_pending_or_failed": 1},
                      {"system_pods_not_ready": 1},
                      {"smoke_can_get_secret": "UNKNOWN"}, {"smoke_can_get_secret": "yes"},
                      {"smoke_can_write_other_namespace": "yes"}, {"smoke_can_create_configmap": "no"}):
            self.assertFalse(readback.operator_path_ready({**self.host(), **patch}))

    def test_secret_like_or_unexpected_fields_are_never_serialized(self):
        for patch in ({"token": "SHOULD_NOT_PRINT"}, {"schema_version": True}, {"rke2_version": "SHOULD_NOT_PRINT"},
                      {"cloud_init_status": "raw-log"}, {"ghcr_http_status": "raw-header"},
                      {"smoke_can_get_secret": "raw-error"}, {"api_ready": "true"}, {"node_count": True}):
            with self.assertRaises(readback.Failure):
                readback.validate_host({**self.host(), **patch})

    def test_running_pod_requires_ready_but_completed_job_is_allowed(self):
        self.assertFalse(host.pod_ready({"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "False"}],
                                                  "containerStatuses": [{"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}))
        self.assertFalse(host.pod_ready({"status": {"phase": "Running"}}))
        self.assertTrue(host.pod_ready({"status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]}}))
        self.assertTrue(host.pod_ready({"status": {"phase": "Succeeded"}}))

    def test_target_identity_ownership_and_private_vpc_are_required(self):
        config = {"instance_id": "i-example", "vpc_id": "vpc-example"}
        instance = {"InstanceId": "i-example", "VpcId": "vpc-example", "State": {"Name": "running"},
                    "IamInstanceProfile": {"Arn": "arn:aws:iam::111122223333:instance-profile/bravo-platform-runtime"},
                    "Tags": [{"Key": k, "Value": v} for k, v in {"project": "bravo-platform", "component": "platform", "managed-by": "terraform"}.items()]}
        readback.validate_instance(instance, config)
        for patch in ({"VpcId": "vpc-other"}, {"InstanceId": "i-other"}, {"PublicIpAddress": "192.0.2.1"},
                      {"IamInstanceProfile": {}}, {"Tags": []}, {"State": {"Name": "stopped"}}):
            with self.assertRaises(readback.Failure):
                readback.validate_instance({**copy.deepcopy(instance), **patch}, config)


if __name__ == "__main__":
    unittest.main()
