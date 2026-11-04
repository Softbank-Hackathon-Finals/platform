# T1 operator SSO verification

Status on 2026-11-04 (KST): **individual SSO login not verified**. The user authorized an organization instance in Seoul for the current development environment, with the operator signing in before the specifically requested teammate is invited. Organizations now contains the existing management account only. A Seoul Identity Center organization instance is `ACTIVE`, with an Identity Store. This is prerequisite creation, not a successful personal login or completed T1 access rollout.

The activation selections were **single Region: Seoul (`ap-northeast-2`), AWS owned key, AWS account access through permission sets enabled, no additional Region selected**. The initial console screen in this account displayed Seoul plus Oregon as a multi-Region configuration; the operator explicitly selected the approved single-Region configuration before enabling it. This describes that observed screen and selection, not a universal console default or proof of every downstream service's processing location. Keep current development placement and a future Japan deployment contract separate: [regional deployment boundary](regional-deployment-boundary.md).

The pre-change account-plan readback was `PAID` / `ACTIVE`, with no remaining Free Tier credits. Organizations creation added no member account and moved no workload. Account-plan status does not estimate future service costs or prove the status of other credit programs. Organization and instance identifiers, console federation material and identity details stay in private evidence; public reports contain the observation time, method and redacted outcome.

The earlier 2026-11-03 discovery remains historical evidence: it used an individual IAM user, returned `AWSOrganizationsNotInUseException`, found zero Identity Center instances in Seoul and Virginia, and had no local SSO profile. Those two regional reads were not a worldwide inventory. The subsequent organization and instance creation supersede those prerequisite failures; the IAM session and temporary console federation still do not establish personal SSO.

AWS account SSO requires an organization instance. An account instance used for supported applications is not an account-permission-set replacement. The operator identity, a human-only `BravoDevelopmentAdministrator` permission set with `AdministratorAccess` and an eight-hour AWS role session, and the single-account assignment are now created; assignment readback is successful. The console accepted the operator email invitation, and a separate local SSO profile was added without changing existing profiles. Invitation acceptance, MFA enrollment and an actual SSO session remain unverified. Complete the interactive login and run the verifier below. Root recovery, an IAM console session and a successful Terraform apply are not evidence of SSO login. [AWS enablement](https://docs.aws.amazon.com/singlesignon/latest/userguide/enable-identity-center.html), [organization relationship](https://docs.aws.amazon.com/singlesignon/latest/userguide/identity-center-and-orgs.html).

## Verification tool

```sh
aws configure sso --profile bravo-operator
aws sso login --profile bravo-operator
# Load the expected account from reviewed private deployment inputs, not the live STS response.
BRAVO_EXPECTED_AWS_ACCOUNT='<reviewed account>' \
  python3 scripts/verify_operator_sso.py --profile bravo-operator
```

The tool requires an explicit modern SSO profile/session, matching expected account and permission-set role name. It removes ambient access-key/web-identity/container credentials, disables metadata fallback, and calls only STS GetCallerIdentity through that profile. It does not create users, reset passwords, send invitations, change policies or publish configuration. It emits status booleans/reason codes, never tokens/account IDs/ARNs/user names/raw AWS errors. Missing/expired/unusable credentials return nonzero; IAM or another-account sessions cannot pass as SSO.

The AWS CLI subprocess always receives `AWS_IGNORE_CONFIGURED_ENDPOINT_URLS=true`. This overrides an inherited false setting and ignores global/service endpoint overrides from both environment variables and profile/services configuration. The rule also applies to the SSO credential exchange used by STS, so a configured arbitrary endpoint cannot receive an SSO token or supply a synthetic successful identity. The tool never accepts or supplies a command-line endpoint override. AWS CLI 2's default endpoint resolution and TLS verification remain in force. [AWS endpoint precedence](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-endpoints.html).

Configuration reads are explicit UTF-8. Missing or unreadable files return `SSO_CONFIG_UNAVAILABLE`; malformed INI or encoding returns `SSO_CONFIG_INVALID`, before any AWS call. Unexpected identity JSON returns `SSO_RESPONSE_INVALID`. These failures emit only the safe JSON result and exit 2, without raw parser text, file paths or tracebacks.

`SSO_SESSION_OBSERVED` means a current STS assumed-role identity matches the selected SSO profile, account and reserved permission-set role-name format. It does not prove fresh MFA, all SCP/permission-boundary behavior, Kubernetes RBAC, DB control, or that every team member can sign in. The local profile and AWS trust remain part of the operator's trusted configuration; this is not an independent identity-provider attestation service.

## Follow-on access

After the individual SSO login passes, resolve the stable IAM role ARN (not the STS session ARN), bind it to the reviewed T6 EKS access entry, and test allowed and denied operations through the private endpoint. RKE2 individual login/RBAC and expiry need a separate mechanism; IAM Identity Center does not automatically authenticate a RKE2 user. Never distribute the RKE2 administrator kubeconfig or server join token as a shared human credential.

The user has authorized the operator-first sequence and then the named teammate's invitation. At the observation boundary above, the operator login and invitation acceptance are unverified. The teammate invitation and account assignment have not been performed, and personal EKS/RKE2 checks remain outstanding. Resolve the existing personal identity before creating another one; an existing individual IAM console user is not an Identity Center user or proof of SSO. Do not turn this one-person handoff into a blanket team invitation or a shared kubeconfig distribution.

## Evidence

- Unit and CLI tests cover missing profile, mixed sources, IAM fallback, account/permission-set mismatch, no-call early failure, ambient-key removal, unreadable/malformed/non-UTF-8 configuration and unexpected identity JSON. Synthetic CLI failures check nonzero exit and absence of raw input/path/traceback output.
- Native AWS CLI regression tests cover global/service environment overrides and profile/services overrides for both SSO and STS, with conflicting false ignore settings. Synthetic endpoints must receive no requests; official HTTPS attempts are stopped by a local rejecting proxy. The test uses no real credentials, cached SSO tokens or external connection. It skips when AWS CLI is absent; local and CI execution must be reported separately.
- Historical service discovery found the prerequisites missing; subsequent Organizations and Seoul `ListInstances` readback confirmed the bounded progress above. Instance readiness does not attest to user login, session expiry, EKS RBAC or RKE2 access.
- Historical local execution with the requested operator profile returned `SSO_PROFILE_MISSING`, exit 2. The new profile has since been added and interactive CLI login handed to the operator. A successful verifier run must produce fresh evidence; do not relabel that earlier result.
- Current T6/T7 foundation validation uses the existing individual IAM operator and must be reported separately from SSO.

This records prerequisite creation and remaining personal verification. T1 is not complete at this boundary. The verifier's behavior and its limits are unchanged.
