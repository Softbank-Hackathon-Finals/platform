# T48: only the two issuer documents per environment are public, and a private key is never published.
mock_provider "aws" {
  override_resource {
    target          = aws_s3_bucket.issuer
    values          = { arn = "arn:aws:s3:::bravo-oidc-issuer", id = "bravo-oidc-issuer" }
    override_during = plan
  }
}

variables {
  account_id = "000000000000"
  region     = "ap-northeast-2"
}

run "public_documents_only" {
  command = plan

  assert {
    condition     = jsondecode(aws_s3_object.discovery["t49"].content).issuer == "https://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/t49"
    error_message = "the issuer must be the bucket's https URL with the environment prefix"
  }

  assert {
    condition     = jsondecode(aws_s3_object.discovery["t49"].content).jwks_uri == "https://bravo-oidc-issuer.s3.ap-northeast-2.amazonaws.com/t49/openid/v1/jwks"
    error_message = "jwks_uri must sit under the issuer"
  }

  assert {
    condition = jsondecode(aws_s3_bucket_policy.issuer.policy).Statement[0].Resource == [
      "arn:aws:s3:::bravo-oidc-issuer/t49/.well-known/openid-configuration",
      "arn:aws:s3:::bravo-oidc-issuer/t49/openid/v1/jwks",
    ]
    error_message = "the public statement must name exactly the two issuer documents"
  }

  assert {
    condition     = aws_s3_bucket_public_access_block.issuer.block_public_acls && aws_s3_bucket_public_access_block.issuer.ignore_public_acls
    error_message = "object ACLs must never make anything else public"
  }
}

run "private_key_is_refused" {
  command = plan

  variables {
    issuers_dir = "tests/fixtures/private"
  }

  expect_failures = [aws_s3_object.jwks]
}
