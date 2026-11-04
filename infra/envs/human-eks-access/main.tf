data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}
data "aws_eks_cluster" "existing" { name = var.cluster_name }
data "aws_iam_role" "human" { name = reverse(split("/", var.sso_role_arn))[0] }

# Ownership is limited to this one human access entry and its policy association.
resource "aws_eks_access_entry" "human" {
  cluster_name  = data.aws_eks_cluster.existing.name
  principal_arn = var.sso_role_arn
  type          = "STANDARD"
  tags          = { component = "access" }

  lifecycle {
    precondition {
      condition     = data.aws_caller_identity.current.account_id == var.account_id
      error_message = "The authenticated caller must belong to the reviewed account."
    }
    precondition {
      condition     = data.aws_eks_cluster.existing.arn == "arn:aws:eks:${var.region}:${var.account_id}:cluster/${var.cluster_name}"
      error_message = "The existing EKS cluster must match the exact account, region, and cluster name."
    }
    precondition {
      condition     = data.aws_eks_cluster.existing.status == "ACTIVE"
      error_message = "The existing EKS cluster must be ACTIVE before registering human access."
    }
    precondition {
      condition     = try(data.aws_eks_cluster.existing.access_config[0].authentication_mode == "API", false)
      error_message = "The existing cluster must already use API authentication; this stack does not change its authentication mode."
    }
    precondition {
      condition     = try(data.aws_eks_cluster.existing.vpc_config[0].endpoint_private_access && !data.aws_eks_cluster.existing.vpc_config[0].endpoint_public_access, false)
      error_message = "The existing EKS endpoint must already be private-only; this stack does not change networking."
    }
    precondition {
      condition     = data.aws_iam_role.human.arn == var.sso_role_arn
      error_message = "IAM must return the exact reviewed SSO role ARN, including its reserved path and suffix."
    }
  }
}

resource "aws_eks_access_policy_association" "human" {
  cluster_name  = aws_eks_access_entry.human.cluster_name
  principal_arn = aws_eks_access_entry.human.principal_arn
  policy_arn    = "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}
