resource "aws_iam_role" "actions" {
  name                 = "${var.name}-smoke"
  max_session_duration = 3600
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
    Principal = { Federated = var.github_oidc_provider_arn },
    Condition = { StringEquals = {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub" = sort(tolist(var.github_oidc_subjects))
    } }
  }] })
}
resource "aws_ssm_document" "smoke" {
  name          = "${var.name}-smoke"
  document_type = "Command"
  content = jsonencode({ schemaVersion = "2.2", description = "Fixed, non-secret RKE2 readiness and namespace RBAC smoke. No command or manifest input.",
    mainSteps = [{ action = "aws:runShellScript", name = "smoke", inputs = {
      timeoutSeconds = "120"
      runCommand = [<<-SH
        set -eu
        test -f /var/lib/bravo-runtime-ready
        export KUBECONFIG=/etc/rancher/rke2/rke2.yaml
        K=/var/lib/rancher/rke2/bin/kubectl
        $K get --raw=/readyz >/dev/null
        $K wait node --all --for=condition=Ready --timeout=30s >/dev/null
        SUBJECT=system:serviceaccount:platform-smoke:deployer
        test "$($K auth can-i get secrets -n platform-smoke --as=$SUBJECT 2>/dev/null || true)" = no
        test "$($K auth can-i create configmaps -n default --as=$SUBJECT 2>/dev/null || true)" = no
        printf '%s\n' '{"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"oidc-smoke","namespace":"platform-smoke"},"data":{"result":"OIDC_TO_RKE2"}}' | $K --as=$SUBJECT apply -f - >/dev/null
        test "$($K --as=$SUBJECT -n platform-smoke get configmap oidc-smoke -o jsonpath='{.data.result}')" = OIDC_TO_RKE2
        printf '%s\n' '{"rke2_ready":true,"namespace_deploy":true,"secret_denied":true,"other_namespace_denied":true}'
      SH
      ]
    } }]
  })
}
resource "aws_iam_role_policy" "actions" {
  role = aws_iam_role.actions.id
  name = "fixed-smoke-only"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ssm:SendCommand", Resource = [aws_ssm_document.smoke.arn, aws_instance.node.arn] },
    { Effect = "Allow", Action = "ssm:GetCommandInvocation", Resource = "*" }
  ] })
}
