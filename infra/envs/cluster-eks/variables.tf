variable "account_id" {
  type      = string
  sensitive = true
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
  description = "Cluster name; must differ from bravo-source-runtime when sharing the account."
  type        = string
  default     = "bravo-eks"
}

variable "component" {
  type    = string
  default = "cluster-eks"
}

# Wired from the aws-foundation step by infra/stacks/cluster-eks (vars.from).
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "private_route_table_id" { type = string }

variable "create_nat" {
  description = "false when the VPC's private route table already has a NAT default route (e.g. source-eks's VPC)."
  type        = bool
  default     = true
}

variable "egress_subnet_cidr" {
  type    = string
  default = "10.80.253.0/24"
}

variable "endpoint_public_access_cidrs" {
  type    = list(string)
  default = []
}

variable "kubernetes_version" {
  type    = string
  default = "1.35"
}

variable "node_release_version" {
  type    = string
  default = null
}

variable "instance_type" {
  type    = string
  default = "t3.large"
}

variable "operator_principal_arn" {
  type      = string
  sensitive = true
}

variable "secret_prefix" {
  description = "Only Secrets Manager secrets named <prefix>* are readable by the ESO controller."
  type        = string
  default     = "bravo/"
  validation {
    condition     = can(regex("^[a-z][a-z0-9/_-]{1,30}$", var.secret_prefix))
    error_message = "Use a short, non-empty secret name prefix: never grant every secret in the account."
  }
}
