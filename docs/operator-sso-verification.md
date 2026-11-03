# T1 operator SSO verification

Status on 2026-11-03: **SSO not verified**. The current AWS CLI uses an existing individual IAM user. The reviewed deployment account returned `AWSOrganizationsNotInUseException`; Identity Center instance lists in Seoul and Virginia both returned zero. No SSO profile exists in the operator's local AWS config. These two regional reads are not a worldwide instance inventory. No organization, SSO user, invitation, permission set or account assignment was created by this work.

AWS account SSO requires an organization instance. An account instance used for supported applications is not an account-permission-set replacement. The operator needs to enable/reuse the intended organization instance in the reviewed account and region, prepare only their own identity/permission set/account assignment, and complete the interactive login. Root recovery, an IAM console session and a successful Terraform apply are not evidence of SSO login. [AWS enablement](https://docs.aws.amazon.com/singlesignon/latest/userguide/enable-identity-center.html), [organization relationship](https://docs.aws.amazon.com/singlesignon/latest/userguide/identity-center-and-orgs.html).

## Verification tool

```sh
aws configure sso --profile bravo-operator
aws sso login --profile bravo-operator
# Load the expected account from reviewed private deployment inputs, not the live STS response.
BRAVO_EXPECTED_AWS_ACCOUNT='<reviewed account>' \
  python3 scripts/verify_operator_sso.py --profile bravo-operator
```

The tool requires an explicit modern SSO profile/session, matching expected account and permission-set role name. It removes ambient access-key/web-identity/container credentials, disables metadata fallback, and calls only STS GetCallerIdentity through that profile. It does not create users, reset passwords, send invitations, change policies or publish configuration. It emits status booleans/reason codes, never tokens/account IDs/ARNs/user names/raw AWS errors. Missing/expired/unusable credentials return nonzero; IAM or another-account sessions cannot pass as SSO.

`SSO_SESSION_OBSERVED` means a current STS assumed-role identity matches the selected SSO profile, account and reserved permission-set role-name format. It does not prove fresh MFA, all SCP/permission-boundary behavior, Kubernetes RBAC, DB control, or that every team member can sign in. The local profile and AWS trust remain part of the operator's trusted configuration; this is not an independent identity-provider attestation service.

## Follow-on access

After the individual SSO login passes, resolve the stable IAM role ARN (not the STS session ARN), bind it to the reviewed T6 EKS access entry, and test allowed and denied operations through the private endpoint. RKE2 individual login/RBAC and expiry need a separate mechanism; IAM Identity Center does not automatically authenticate a RKE2 user. Never distribute the RKE2 administrator kubeconfig or server join token as a shared human credential.

Team invitations and access-information distribution remain deferred by the user. The operator's own session should pass before any later team rollout.

## Evidence

- Seven unit tests cover missing profile, mixed sources, IAM fallback, account/permission-set mismatch, no-call early failure and ambient-key removal/failed-login handling.
- Live AWS service discovery found the prerequisites above missing.
- Local execution with the requested operator profile returned `SSO_PROFILE_MISSING`, exit 2.
- Current T6/T7 foundation validation uses the existing individual IAM operator and must be reported separately from SSO.

This is a reviewable preparation and honest blocked-state report, not a completed T1 SSO rollout.
