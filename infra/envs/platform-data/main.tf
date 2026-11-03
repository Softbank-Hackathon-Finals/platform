variable "account_id" {
  type      = string
  sensitive = true
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the private operator account mapping."
  }
}
variable "region" {
  type    = string
  default = "ap-northeast-2"
}
variable "instance_id" {
  type      = string
  sensitive = true
}
variable "vpc_id" {
  type      = string
  sensitive = true
}

data "aws_instance" "platform" { instance_id = var.instance_id }
data "aws_subnet" "platform" { id = data.aws_instance.platform.subnet_id }

# Static local PV on a separate, encrypted EBS disk. No node/SSM/IAM changes.
# This deliberately does not install a cluster-wide CSI controller for one node.
resource "aws_ebs_volume" "database" {
  availability_zone = data.aws_instance.platform.availability_zone
  size              = 16
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "bravo-platform-data", purpose = "platform-state" }
  lifecycle {
    prevent_destroy = true
    precondition {
      condition = (
        data.aws_subnet.platform.vpc_id == var.vpc_id &&
        lookup(data.aws_instance.platform.tags, "project", "") == "bravo-platform" &&
        lookup(data.aws_instance.platform.tags, "component", "") == "platform" &&
        data.aws_instance.platform.public_ip == ""
      )
      error_message = "Platform data must attach to the existing private Bravo platform node."
    }
  }
}
resource "aws_volume_attachment" "database" {
  device_name  = "/dev/sdf"
  volume_id    = aws_ebs_volume.database.id
  instance_id  = var.instance_id
  force_detach = false
  lifecycle { prevent_destroy = true }
}

output "storage" {
  sensitive = true
  value     = { volume_id = aws_ebs_volume.database.id, instance_id = var.instance_id, size_gib = 16 }
}
