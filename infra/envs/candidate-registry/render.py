"""ECR credentials for platform-built candidates (T49, 2026-11-08: no GitHub in the happy path).

The workflow host pushes the candidate it builds (services/platform-events candidate_build) with
`platform-state/platform-ecr-push`; REHEARSE copies `platform-state/platform-ecr-reader` into the target's
pull Secret (workflow_application pull_secret). A CronJob refreshes both every 6 hours (ECR tokens last 12):

- the push token comes from the platform host role (`bravo-platform-runtime`, which may push to the one
  candidate repository only: infra/envs/private-registry). Pods reach the instance metadata only on the
  host network (IMDSv2 hop limit 1), so this one Pod runs there, in its own privileged namespace;
- the pull token comes from `bravo-candidate-puller` (pull-only, assumed by the host role), so a
  destination's copy can never push;
- the Pod may only patch those two Secrets (pre-created once with --placeholders), nothing else.

Account-specific values come from the private Terraform output, never from Git:

    terraform -chdir=infra/envs/private-registry output -json candidate_build_contract > "$PRIVATE_CONTRACT"
    python3 infra/envs/candidate-registry/render.py --placeholders | kubectl create -f - || true  # once
    python3 infra/envs/candidate-registry/render.py --contract "$PRIVATE_CONTRACT" | kubectl apply -f -
    kubectl -n bravo-registry-credentials create job --from=cronjob/ecr-credentials ecr-credentials-now
"""

import argparse
import base64
import json
import re
import sys

NAMESPACE = "bravo-registry-credentials"
SECRETS = ("platform-ecr-push", "platform-ecr-reader")
# public.ecr.aws/aws-cli/aws-cli:2.31.37 (index; amd64 and arm64): aws, curl, base64 and sh, no credentials.
IMAGE = "public.ecr.aws/aws-cli/aws-cli@sha256:0af642c5d907ed2960f4b03c8741339bdd794f1021962f4984d456a87ff9721c"
LABELS = {"app.kubernetes.io/name": "ecr-credentials", "app.kubernetes.io/part-of": "bravo-platform",
          "component": "platform"}
SCRIPT = r"""set -eu
SA=/var/run/secrets/kubernetes.io/serviceaccount
config() { printf '{"auths":{"%s":{"auth":"%s"}}}' "$REGISTRY" "$(printf 'AWS:%s' "$1" | base64 -w0)" | base64 -w0; }
patch() {  # the token goes through a private file, never argv or the log
  umask 077
  printf '{"data":{".dockerconfigjson":"%s"}}' "$2" > /tmp/patch.json
  code=$(curl -sS -o /dev/null -w '%{http_code}' --cacert "$SA/ca.crt" -H "Authorization: Bearer $(cat "$SA/token")" \
    -H 'Content-Type: application/merge-patch+json' -X PATCH --data-binary @/tmp/patch.json \
    "https://kubernetes.default.svc/api/v1/namespaces/platform-state/secrets/$1")
  rm -f /tmp/patch.json
  [ "$code" = 200 ] || { echo "patch $1: HTTP $code" >&2; exit 1; }
}
push=$(aws ecr get-login-password --region "$REGION")
set -- $(aws sts assume-role --role-arn "$PULLER" --role-session-name candidate-pull --duration-seconds 900 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)
pull=$(AWS_ACCESS_KEY_ID=$1 AWS_SECRET_ACCESS_KEY=$2 AWS_SESSION_TOKEN=$3 aws ecr get-login-password --region "$REGION")
patch platform-ecr-push "$(config "$push")"
patch platform-ecr-reader "$(config "$pull")"
echo "refreshed platform-ecr-push platform-ecr-reader"
"""


def placeholders():
    empty = base64.b64encode(b'{"auths":{}}').decode()
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
         "metadata": {"name": name, "namespace": "platform-state", "labels": LABELS},
         "data": {".dockerconfigjson": empty}} for name in SECRETS]}


def render(contract):
    url, puller = contract.get("repository_url", ""), contract.get("puller_role_arn", "")
    match = re.fullmatch(r"([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/bravo/demo-chat", url)
    if (match is None or contract.get("platform_host_role") != "bravo-platform-runtime"
            or puller != f"arn:aws:iam::{match[1]}:role/bravo-candidate-puller"):
        raise ValueError("candidate_build_contract_unexpected")
    registry, region = url.split("/")[0], match[2]
    meta = {"namespace": NAMESPACE, "labels": LABELS}
    pod = {
        "serviceAccountName": "ecr-credentials", "restartPolicy": "Never",
        "hostNetwork": True, "dnsPolicy": "ClusterFirstWithHostNet",  # instance metadata: hop limit 1
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "refresh", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
            "command": ["sh", "-c", SCRIPT],
            "env": [{"name": "REGISTRY", "value": registry}, {"name": "REGION", "value": region},
                    {"name": "PULLER", "value": puller}, {"name": "HOME", "value": "/tmp"},
                    {"name": "AWS_EC2_METADATA_V1_DISABLED", "value": "true"}],
            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]}},
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
            "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
        "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": "16Mi"}}]}
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE, "labels": LABELS | {
            "pod-security.kubernetes.io/enforce": "privileged"}}},  # hostNetwork only; the Pod itself is restricted
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "ecr-credentials", **meta}},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
         "metadata": {"name": "ecr-credentials-refresh", "namespace": "platform-state", "labels": LABELS},
         "rules": [{"apiGroups": [""], "resources": ["secrets"], "resourceNames": list(SECRETS),
                    "verbs": ["get", "patch"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
         "metadata": {"name": "ecr-credentials-refresh", "namespace": "platform-state", "labels": LABELS},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "ecr-credentials-refresh"},
         "subjects": [{"kind": "ServiceAccount", "name": "ecr-credentials", "namespace": NAMESPACE}]},
        {"apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "ecr-credentials", **meta},
         "spec": {"schedule": "17 */6 * * *", "concurrencyPolicy": "Forbid", "successfulJobsHistoryLimit": 1,
                  "failedJobsHistoryLimit": 2,
                  "jobTemplate": {"spec": {"backoffLimit": 2, "activeDeadlineSeconds": 300,
                                           "ttlSecondsAfterFinished": 86400,
                                           "template": {"metadata": {"labels": LABELS}, "spec": pod}}}}}]}


def _check():
    contract = {"repository_url": "111111111111.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/demo-chat",
                "puller_role_arn": "arn:aws:iam::111111111111:role/bravo-candidate-puller",
                "platform_host_role": "bravo-platform-runtime"}
    items = {item["kind"]: item for item in render(contract)["items"]}
    assert items["Role"]["rules"] == [{"apiGroups": [""], "resources": ["secrets"],
                                       "resourceNames": ["platform-ecr-push", "platform-ecr-reader"],
                                       "verbs": ["get", "patch"]}]
    pod = items["CronJob"]["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
    assert pod["hostNetwork"] and env["REGISTRY"] == "111111111111.dkr.ecr.ap-northeast-2.amazonaws.com"
    assert "privileged" not in json.dumps(pod) and pod["containers"][0]["image"].startswith("public.ecr.aws/")
    for bad in ({**contract, "puller_role_arn": "arn:aws:iam::222222222222:role/bravo-candidate-puller"},
                {**contract, "repository_url": "111111111111.dkr.ecr.ap-northeast-2.amazonaws.com/other"},
                {**contract, "platform_host_role": "admin"}):
        try:
            render(bad)
            raise AssertionError("unexpected contract accepted")
        except ValueError:
            pass
    assert [s["metadata"]["name"] for s in placeholders()["items"]] == list(SECRETS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--contract", help="private-registry `candidate_build_contract` output (JSON)")
    parser.add_argument("--placeholders", action="store_true", help="the two empty Secrets, created once")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        _check()
        print("ok")
    elif args.placeholders:
        json.dump(placeholders(), sys.stdout, indent=1)
    elif args.contract:
        with open(args.contract) as stream:
            json.dump(render(json.load(stream)), sys.stdout, indent=1)
    else:
        parser.error("--contract, --placeholders or --self-check")
