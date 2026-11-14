# EKS for either role (source or target), generalised from infra/envs/source-eks (which stays unchanged).
# - IRSA: an IAM OIDC provider for the cluster issuer, so workloads can assume roles by service account.
# - Pod Identity agent: lets a root grant a role to a namespace/service account without putting an
#   account-bearing role ARN into manifests (used for ESO and the EBS CSI driver).
# - EBS CSI driver: CNPG volumes on gp3.
# The module declares no provider: the root's account guard and default tags (component) apply.
data "aws_partition" "current" {}

data "aws_subnet" "private" {
  for_each = toset(var.private_subnet_ids)
  id       = each.value
}

locals {
  partition = data.aws_partition.current.partition
  issuer    = replace(aws_iam_openid_connect_provider.this.url, "https://", "")
  nat_count = var.create_nat ? 1 : 0
}

resource "aws_internet_gateway" "egress" {
  count  = local.nat_count
  vpc_id = var.vpc_id
}

resource "aws_subnet" "egress" {
  count                   = local.nat_count
  vpc_id                  = var.vpc_id
  cidr_block              = var.egress_subnet_cidr
  availability_zone       = data.aws_subnet.private[var.private_subnet_ids[0]].availability_zone
  map_public_ip_on_launch = false
}

resource "aws_route_table" "egress" {
  count  = local.nat_count
  vpc_id = var.vpc_id
}

resource "aws_route" "internet" {
  count                  = local.nat_count
  route_table_id         = aws_route_table.egress[0].id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.egress[0].id
}

resource "aws_route_table_association" "egress" {
  count          = local.nat_count
  subnet_id      = aws_subnet.egress[0].id
  route_table_id = aws_route_table.egress[0].id
}

resource "aws_eip" "egress" {
  count  = local.nat_count
  domain = "vpc"
}

resource "aws_nat_gateway" "egress" {
  count         = local.nat_count
  allocation_id = aws_eip.egress[0].id
  subnet_id     = aws_subnet.egress[0].id
  depends_on    = [aws_route.internet]
}

resource "aws_route" "private_egress" {
  count                  = local.nat_count
  route_table_id         = var.private_route_table_id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.egress[0].id
}

resource "aws_iam_role" "cluster" {
  name = var.name
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["sts:AssumeRole", "sts:TagSession"], Principal = { Service = "eks.amazonaws.com" }
  }] })
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_kms_key" "cluster" {
  description             = "EKS envelope encryption and audit logs of ${var.name}"
  enable_key_rotation     = true
  deletion_window_in_days = 7
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "AccountAdministration", Effect = "Allow", Principal = { AWS = "arn:${local.partition}:iam::${var.account_id}:root" }, Action = "kms:*", Resource = "*" },
    { Sid       = "LogEncryption", Effect = "Allow", Principal = { Service = "logs.${var.region}.amazonaws.com" },
      Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"], Resource = "*",
      Condition = { ArnEquals = { "kms:EncryptionContext:aws:logs:arn" = "arn:${local.partition}:logs:${var.region}:${var.account_id}:log-group:/aws/eks/${var.name}/cluster" } }
    }
  ] })
}

resource "aws_cloudwatch_log_group" "cluster" {
  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = 7
  kms_key_id        = aws_kms_key.cluster.arn
}

resource "aws_eks_cluster" "this" {
  name                      = var.name
  role_arn                  = aws_iam_role.cluster.arn
  version                   = var.kubernetes_version
  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }
  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = length(var.endpoint_public_access_cidrs) > 0
    public_access_cidrs     = length(var.endpoint_public_access_cidrs) > 0 ? var.endpoint_public_access_cidrs : null
  }
  encryption_config {
    resources = ["secrets"]
    provider { key_arn = aws_kms_key.cluster.arn }
  }
  upgrade_policy { support_type = "STANDARD" }
  depends_on = [aws_iam_role_policy_attachment.cluster, aws_cloudwatch_log_group.cluster]
  lifecycle {
    precondition {
      condition     = alltrue([for subnet in data.aws_subnet.private : subnet.vpc_id == var.vpc_id && subnet.map_public_ip_on_launch == false]) && length(toset([for subnet in data.aws_subnet.private : subnet.availability_zone])) == 2
      error_message = "Both private subnets must belong to the VPC in different AZs."
    }
    precondition {
      condition     = split(":", var.operator_principal_arn)[4] == var.account_id
      error_message = "The operator principal must belong to the reviewed deployment account."
    }
  }
}

resource "aws_vpc_security_group_ingress_rule" "api" {
  count             = var.api_ingress_cidr == null ? 0 : 1
  security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  description       = "TLS API from a peered private network; IAM/RBAC still required"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = var.api_ingress_cidr
}

resource "aws_eks_access_entry" "operator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = var.operator_principal_arn
  type          = "STANDARD"
}

resource "aws_eks_access_policy_association" "operator" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_eks_access_entry.operator.principal_arn
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope { type = "cluster" }
}

# IRSA: the cluster's OIDC issuer as an IAM identity provider.
resource "aws_iam_openid_connect_provider" "this" {
  url            = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "cni" {
  name = "${var.name}-cni"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = aws_iam_openid_connect_provider.this.arn },
    Condition = { StringEquals = {
      "${local.issuer}:aud" = "sts.amazonaws.com",
      "${local.issuer}:sub" = "system:serviceaccount:kube-system:aws-node"
    } }
  }] })
}

resource "aws_iam_role_policy_attachment" "cni" {
  role       = aws_iam_role.cni.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_eks_addon" "cni" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "vpc-cni"
  addon_version               = var.vpc_cni_version
  configuration_values        = jsonencode({ enableNetworkPolicy = "true" })
  service_account_role_arn    = aws_iam_role.cni.arn
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  depends_on                  = [aws_iam_role_policy_attachment.cni]
}

resource "aws_eks_addon" "pod_identity" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "eks-pod-identity-agent"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  depends_on                  = [aws_eks_node_group.this]
}

data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_eks_addon" "ebs_csi" {
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  addon_version               = var.ebs_csi_version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"
  pod_identity_association {
    role_arn        = aws_iam_role.ebs_csi.arn
    service_account = "ebs-csi-controller-sa"
  }
  depends_on = [aws_eks_addon.pod_identity, aws_iam_role_policy_attachment.ebs_csi]
}

resource "aws_iam_role" "node" {
  name = "${var.name}-node"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" }
  }] })
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each   = toset(["AmazonEKSWorkerNodePolicy", "AmazonEC2ContainerRegistryPullOnly"])
  role       = aws_iam_role.node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.value}"
}

resource "aws_launch_template" "node" {
  name_prefix = "${var.name}-"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_type           = "gp3"
      volume_size           = 30
      encrypted             = true
      delete_on_termination = true
    }
  }
  dynamic "tag_specifications" {
    for_each = toset(["instance", "volume"])
    content {
      resource_type = tag_specifications.value
      tags          = { project = "bravo-platform", component = var.component, "managed-by" = "terraform" }
    }
  }
}

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "base"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  ami_type        = "AL2023_x86_64_STANDARD"
  version         = var.kubernetes_version
  release_version = var.node_release_version
  instance_types  = [var.instance_type]
  scaling_config {
    desired_size = var.node_count
    min_size     = 1
    max_size     = var.node_count + 1
  }
  update_config { max_unavailable = 1 }
  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }
  depends_on = [aws_iam_role_policy_attachment.node, aws_eks_addon.cni, aws_route.private_egress]
}
