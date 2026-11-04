mock_provider "aws" {
  mock_data "aws_partition" { defaults = { partition = "aws" } }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "111122223333" }
  }
  mock_data "aws_eks_cluster" {
    defaults = {
      arn           = "arn:aws:eks:ap-northeast-2:111122223333:cluster/example-source"
      status        = "ACTIVE"
      access_config = [{ authentication_mode = "API" }]
      vpc_config    = [{ endpoint_private_access = true, endpoint_public_access = false }]
    }
  }
  mock_data "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_ExampleDevelopment_0123456789abcdef" }
  }
}

variables {
  account_id   = "111122223333"
  region       = "ap-northeast-2"
  cluster_name = "example-source"
  sso_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_ExampleDevelopment_0123456789abcdef"
}

run "existing_private_cluster_human_access" {
  command = plan
  assert {
    condition = (
      aws_eks_access_entry.human.cluster_name == var.cluster_name &&
      aws_eks_access_entry.human.principal_arn == var.sso_role_arn &&
      aws_eks_access_entry.human.type == "STANDARD" &&
      aws_eks_access_entry.human.tags.component == "access"
    )
    error_message = "Register only the explicit human SSO role on the selected cluster and tag its access entry."
  }
  assert {
    condition = (
      aws_eks_access_policy_association.human.principal_arn == var.sso_role_arn &&
      aws_eks_access_policy_association.human.cluster_name == var.cluster_name &&
      aws_eks_access_policy_association.human.policy_arn == "arn:${data.aws_partition.current.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy" &&
      aws_eks_access_policy_association.human.access_scope[0].type == "cluster"
    )
    error_message = "The approved human development role receives exactly the cluster-wide EKS admin policy."
  }
}

run "wrong_caller_account_rejected" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "222222222222" }
  }
  expect_failures = [aws_eks_access_entry.human]
}

run "foreign_role_rejected" {
  command = plan
  variables { sso_role_arn = "arn:aws:iam::222222222222:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_ExampleDevelopment_0123456789abcdef" }
  expect_failures = [var.sso_role_arn]
}

run "ordinary_iam_role_rejected" {
  command = plan
  variables { sso_role_arn = "arn:aws:iam::111122223333:role/operator" }
  expect_failures = [var.sso_role_arn]
}

run "iam_user_rejected" {
  command = plan
  variables { sso_role_arn = "arn:aws:iam::111122223333:user/operator" }
  expect_failures = [var.sso_role_arn]
}

run "session_arn_rejected" {
  command = plan
  variables { sso_role_arn = "arn:aws:sts::111122223333:assumed-role/AWSReservedSSO_ExampleDevelopment_0123456789abcdef/session" }
  expect_failures = [var.sso_role_arn]
}

run "wildcard_role_rejected" {
  command = plan
  variables { sso_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/AWSReservedSSO_ExampleDevelopment_*" }
  expect_failures = [var.sso_role_arn]
}

run "role_with_different_path_rejected" {
  command = plan
  override_data {
    target = data.aws_iam_role.human
    values = { arn = "arn:aws:iam::111122223333:role/AWSReservedSSO_ExampleDevelopment_0123456789abcdef" }
  }
  expect_failures = [aws_eks_access_entry.human]
}

run "wrong_cluster_account_rejected" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.existing
    values = {
      arn           = "arn:aws:eks:ap-northeast-2:222222222222:cluster/example-source"
      status        = "ACTIVE"
      access_config = [{ authentication_mode = "API" }]
      vpc_config    = [{ endpoint_private_access = true, endpoint_public_access = false }]
    }
  }
  expect_failures = [aws_eks_access_entry.human]
}

run "different_eks_region_rejected" {
  command = plan
  variables { region = "ap-northeast-1" }
  expect_failures = [aws_eks_access_entry.human]
}

run "empty_region_rejected" {
  command = plan
  variables { region = "" }
  expect_failures = [var.region]
}

run "inactive_cluster_rejected" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.existing
    values = {
      arn           = "arn:aws:eks:ap-northeast-2:111122223333:cluster/example-source"
      status        = "UPDATING"
      access_config = [{ authentication_mode = "API" }]
      vpc_config    = [{ endpoint_private_access = true, endpoint_public_access = false }]
    }
  }
  expect_failures = [aws_eks_access_entry.human]
}

run "config_map_authentication_rejected" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.existing
    values = {
      arn           = "arn:aws:eks:ap-northeast-2:111122223333:cluster/example-source"
      status        = "ACTIVE"
      access_config = [{ authentication_mode = "CONFIG_MAP" }]
      vpc_config    = [{ endpoint_private_access = true, endpoint_public_access = false }]
    }
  }
  expect_failures = [aws_eks_access_entry.human]
}

run "public_endpoint_rejected" {
  command = plan
  override_data {
    target = data.aws_eks_cluster.existing
    values = {
      arn           = "arn:aws:eks:ap-northeast-2:111122223333:cluster/example-source"
      status        = "ACTIVE"
      access_config = [{ authentication_mode = "API" }]
      vpc_config    = [{ endpoint_private_access = true, endpoint_public_access = true }]
    }
  }
  expect_failures = [aws_eks_access_entry.human]
}

run "identity_center_home_region_is_independent" {
  command = plan
  variables { sso_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/eu-west-2/AWSReservedSSO_ExampleDevelopment_0123456789abcdef" }
  override_data {
    target = data.aws_iam_role.human
    values = { arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/eu-west-2/AWSReservedSSO_ExampleDevelopment_0123456789abcdef" }
  }
}

run "us_east_1_identity_center_path_supported" {
  command = plan
  variables { sso_role_arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_ExampleDevelopment_0123456789abcdef" }
  override_data {
    target = data.aws_iam_role.human
    values = { arn = "arn:aws:iam::111122223333:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_ExampleDevelopment_0123456789abcdef" }
  }
}
