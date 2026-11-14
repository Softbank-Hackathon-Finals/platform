output "cluster" {
  description = "Access information for update-kubeconfig and the stack's cluster binding. No credential."
  value = {
    name              = aws_eks_cluster.this.name
    region            = var.region
    endpoint          = aws_eks_cluster.this.endpoint
    ca_data           = aws_eks_cluster.this.certificate_authority[0].data
    oidc_issuer       = aws_eks_cluster.this.identity[0].oidc[0].issuer
    oidc_provider_arn = aws_iam_openid_connect_provider.this.arn
    security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  }
}
