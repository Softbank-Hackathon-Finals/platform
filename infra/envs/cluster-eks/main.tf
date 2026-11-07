# EKS for either role (A10). The cluster comes from cluster/aws; this root adds the build side of the kind's
# identity (both.identity): the ESO controller reads Secrets Manager through EKS Pod Identity, so no
# account-bearing role ARN enters the manifests, and only secrets under a name prefix.
module "cluster" {
  source                       = "../../modules/cluster/aws"
  account_id                   = var.account_id
  region                       = var.region
  name                         = var.name
  component                    = var.component
  vpc_id                       = var.vpc_id
  private_subnet_ids           = var.private_subnet_ids
  private_route_table_id       = var.private_route_table_id
  create_nat                   = var.create_nat
  egress_subnet_cidr           = var.egress_subnet_cidr
  endpoint_public_access_cidrs = var.endpoint_public_access_cidrs
  kubernetes_version           = var.kubernetes_version
  node_release_version         = var.node_release_version
  instance_type                = var.instance_type
  operator_principal_arn       = var.operator_principal_arn
}

data "aws_partition" "current" {}

data "aws_iam_policy_document" "eso_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "eso_read" {
  statement {
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = ["arn:${data.aws_partition.current.partition}:secretsmanager:${var.region}:${var.account_id}:secret:${var.secret_prefix}*"]
  }
}

resource "aws_iam_role" "eso" {
  name               = "${var.name}-eso"
  assume_role_policy = data.aws_iam_policy_document.eso_trust.json
}

resource "aws_iam_role_policy" "eso" {
  name   = "read-prefixed-secrets"
  role   = aws_iam_role.eso.id
  policy = data.aws_iam_policy_document.eso_read.json
}

resource "aws_eks_pod_identity_association" "eso" {
  cluster_name    = module.cluster.cluster.name
  namespace       = "external-secrets"
  service_account = "external-secrets"
  role_arn        = aws_iam_role.eso.arn
}

output "cluster" {
  description = "update-kubeconfig and the stack's cluster binding (endpoint). No credential."
  sensitive   = true
  value       = merge(module.cluster.cluster, { secret_prefix = var.secret_prefix })
}
