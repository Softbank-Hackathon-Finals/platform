import copy
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("human_access", Path(__file__).parents[1] / "provision_human_access.py")
human = importlib.util.module_from_spec(spec)
spec.loader.exec_module(human)

CONFIG = {"expected_account": "111122223333", "members": [
    {"alias": "member-01", "github_login": "owner", "user_name": "existing-owner", "principal_type": "human", "reuse_existing": True},
    {"alias": "member-02", "github_login": "developer", "user_name": "bravo-developer", "principal_type": "human"},
]}


class FakeAWS:
    def __init__(self):
        self.account = CONFIG["expected_account"]
        self.users = {"existing-owner": {"Path": "/", "Tags": []}}
        self.group = None
        self.policy = False
        self.writes = []

    def call(self, operation, **p):
        name = p.get("user_name")
        if operation in human.WRITES:
            self.writes.append((operation, p))
        if operation == "get-caller-identity": return {"Account": self.account}
        if operation == "get-group":
            if self.group is None: raise human.Failure("NoSuchEntity")
            return {"Group": {"Path": human.PATH}, "Users": [{"UserName": x} for x in self.group]}
        if operation == "list-users": return {"Users": [{"UserName": k} for k in self.users]}
        if operation == "list-user-tags": return {"Tags": self.users[name]["Tags"]}
        if operation == "get-user":
            if name not in self.users: raise human.Failure("NoSuchEntity")
            return {"User": self.users[name]}
        if operation == "list-groups-for-user": return {"Groups": [{"GroupName": human.GROUP}] if self.group and name in self.group else []}
        if operation == "list-attached-user-policies": return {"AttachedPolicies": [{"PolicyArn": human.ADMIN}] if name == "existing-owner" else []}
        if operation == "list-attached-group-policies": return {"AttachedPolicies": [{"PolicyArn": human.ADMIN}] if self.policy else []}
        if operation == "get-login-profile": raise human.Failure("NoSuchEntity")
        if operation == "list-access-keys": return {"AccessKeyMetadata": [{}] if name == "existing-owner" else []}
        if operation == "create-group": self.group = []; return {}
        if operation == "attach-group-policy": self.policy = True; return {}
        if operation == "create-user": self.users[name] = {"Path": p["path"], "Tags": p["tags"]}; return {}
        if operation == "add-user-to-group": self.group.append(name); return {}
        raise AssertionError(operation)


class HumanProvisioningTests(unittest.TestCase):
    def test_apply_is_idempotent_and_preserves_existing_admin(self):
        aws = FakeAWS()
        before = human.snapshot(aws, CONFIG)
        actions = human.plan(before, False)
        human.apply(aws, CONFIG, before, actions)
        after = human.snapshot(aws, CONFIG)
        self.assertEqual(human.plan(after, False), [])
        self.assertNotIn("existing-owner", aws.group)
        self.assertEqual(len(aws.writes), 4)
        receipt = human.receipt(before, after, actions, "apply_readback")
        public = json.dumps(receipt)
        for value in ("111122223333", "existing-owner", "bravo-developer", "arn:"):
            self.assertNotIn(value, public)
        self.assertTrue(all(r["administrator_access_policy_attached"] for r in receipt["members"]))
        self.assertTrue(all(r["new_access_key_count"] == 0 for r in receipt["members"]))
        self.assertFalse(receipt["effective_access_and_human_login_verified"])

    def test_wrong_account_prevents_inventory_and_changes(self):
        aws = FakeAWS(); aws.account = "222222222222"
        with self.assertRaisesRegex(human.Failure, "account_boundary"):
            human.snapshot(aws, CONFIG)
        self.assertEqual(aws.writes, [])

    def test_runtime_member_in_existing_group_blocks_admin_attachment(self):
        aws = FakeAWS(); aws.group = ["runtime-worker"]
        with self.assertRaisesRegex(human.Failure, "group_scope"):
            human.snapshot(aws, CONFIG)
        self.assertEqual(aws.writes, [])

    def test_existing_person_mapping_blocks_duplicate_creation(self):
        aws = FakeAWS(); aws.users["previous-person"] = {"Path": "/", "Tags": [{"Key": "github-login", "Value": "developer"}]}
        with self.assertRaisesRegex(human.Failure, "identity_mapping"):
            human.snapshot(aws, CONFIG)
        self.assertEqual(aws.writes, [])

    def test_unmanaged_name_collision_is_not_adopted(self):
        aws = FakeAWS(); aws.users["bravo-developer"] = {"Path": "/other/", "Tags": []}
        with self.assertRaisesRegex(human.Failure, "outside_managed"):
            human.snapshot(aws, CONFIG)

    def test_keychain_failure_prevents_all_mutation(self):
        aws = FakeAWS(); state = human.snapshot(aws, CONFIG)
        with patch.object(human, "native", side_effect=human.Failure("keychain_read_failed")), self.assertRaisesRegex(human.Failure, "keychain"):
            human.apply(aws, CONFIG, state, human.plan(state, True), Path("helper"))
        self.assertEqual(aws.writes, [])

    def test_existing_profile_never_gets_password_reset(self):
        aws = FakeAWS(); state = human.snapshot(aws, CONFIG)
        for row in state["members"]: row["console_profile_exists"] = True
        self.assertNotIn("create_initial_console_profile", [a["action"] for a in human.plan(state, True)])

    def test_missing_sdk_prevents_all_mutation(self):
        aws = FakeAWS(); state = human.snapshot(aws, CONFIG)
        with patch.object(human, "native", return_value={"keychain_probe": "PASS"}), self.assertRaisesRegex(human.Failure, "sdk_python_required"):
            human.apply(aws, CONFIG, state, human.plan(state, True), Path("helper"))
        self.assertEqual(aws.writes, [])

    def test_worker_principal_and_duplicate_roster_are_rejected(self):
        config = copy.deepcopy(CONFIG); config["members"][1]["principal_type"] = "worker"
        with self.assertRaisesRegex(human.Failure, "human_identity"):
            human.validate(config)
        config = copy.deepcopy(CONFIG); config["members"][1]["user_name"] = "existing-owner"
        with self.assertRaisesRegex(human.Failure, "duplicate_user"):
            human.validate(config)
        config = copy.deepcopy(CONFIG); config["members"][0]["github_login"] = "DEVELOPER"
        with self.assertRaisesRegex(human.Failure, "duplicate_person"):
            human.validate(config)

    def test_cli_cannot_generate_access_keys_or_reset_profiles(self):
        with patch.object(human.subprocess, "run") as run:
            for operation in ("create-access-key", "update-login-profile", "attach-role-policy"):
                with self.assertRaisesRegex(human.Failure, "operation_not_allowed"):
                    human.AWS().call(operation)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
