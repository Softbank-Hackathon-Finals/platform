mock_provider "aws" {
  mock_data "aws_instance" {
    defaults = {
      availability_zone = "ap-northeast-2a"
      subnet_id         = "subnet-0123456789abcdef0"
      public_ip         = ""
      tags              = { project = "bravo-platform", component = "platform" }
    }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
}
variables {
  account_id  = "111122223333"
  instance_id = "i-0123456789abcdef0"
  vpc_id      = "vpc-0123456789abcdef0"
}
run "retained_encrypted_data_only" {
  command = plan
  assert {
    condition     = aws_ebs_volume.database.encrypted && aws_ebs_volume.database.size == 16 && aws_ebs_volume.database.type == "gp3"
    error_message = "The dedicated data volume must be encrypted, bounded gp3 storage."
  }
  assert {
    condition     = !aws_volume_attachment.database.force_detach
    error_message = "Active database storage cannot be force detached."
  }
}
run "wrong_owner_refused" {
  command = plan
  override_data {
    target = data.aws_instance.platform
    values = { tags = { project = "another-project", component = "platform" } }
  }
  expect_failures = [aws_ebs_volume.database]
}
run "public_host_refused" {
  command = plan
  override_data {
    target = data.aws_instance.platform
    values = { public_ip = "203.0.113.10" }
  }
  expect_failures = [aws_ebs_volume.database]
}
run "wrong_vpc_refused" {
  command = plan
  variables { vpc_id = "vpc-00000000000000000" }
  expect_failures = [aws_ebs_volume.database]
}
