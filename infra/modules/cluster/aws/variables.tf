variable "account_id" {
  description = "Expected AWS account (checked against the operator principal). Supplied privately."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Supply the reviewed deployment account privately."
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name" {
  description = "Cluster name; a source and a target cluster in one account need different names."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,38}$", var.name))
    error_message = "Use 3-39 lower-case letters, digits or hyphens, starting with a letter."
  }
}

variable "component" {
  description = "component tag of the instances and volumes the launch template creates (provider default_tags cover the rest)."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,31}$", var.component))
    error_message = "Use a short lower-case component name."
  }
}

variable "vpc_id" { type = string }

variable "private_subnet_ids" {
  type = list(string)
  validation {
    condition     = length(var.private_subnet_ids) == 2 && length(toset(var.private_subnet_ids)) == 2
    error_message = "Supply two distinct private subnets in different AZs."
  }
}

variable "private_route_table_id" {
  description = "Route table of the private subnets; a NAT default route is added when create_nat is true."
  type        = string
}

variable "create_nat" {
  description = "Create an internet gateway, public egress subnet and NAT for the private subnets."
  type        = bool
  default     = true
}

variable "egress_subnet_cidr" {
  type    = string
  default = "10.80.254.0/24"
}

variable "endpoint_public_access_cidrs" {
  description = "CIDRs allowed to the public API endpoint (IAM and RBAC still apply). Empty keeps the endpoint private."
  type        = list(string)
  default     = []
  validation {
    condition     = !contains(var.endpoint_public_access_cidrs, "0.0.0.0/0")
    error_message = "Do not open the API endpoint to the whole internet."
  }
}

variable "api_ingress_cidr" {
  description = "Optional private CIDR (e.g. a peered platform VPC) allowed to the private endpoint on 443."
  type        = string
  default     = null
}

variable "kubernetes_version" {
  type    = string
  default = "1.35"
}

variable "node_release_version" {
  description = "Exact EKS AL2023 release (null: the latest for kubernetes_version)."
  type        = string
  default     = null
}

variable "instance_type" {
  type    = string
  default = "t3.large"
}

variable "node_count" {
  type    = number
  default = 1
}

variable "operator_principal_arn" {
  description = "Individual operator IAM role (SSO preferred) granted cluster admin through an access entry."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/[^*?]+$", var.operator_principal_arn))
    error_message = "Use a stable individual IAM principal ARN, not a shared key or session ARN."
  }
}

variable "vpc_cni_version" {
  description = "Exact vpc-cni add-on version for kubernetes_version (null: the default)."
  type        = string
  default     = null
}

variable "ebs_csi_version" {
  description = "Exact aws-ebs-csi-driver add-on version (null: the default). CNPG volumes use it."
  type        = string
  default     = null
}
