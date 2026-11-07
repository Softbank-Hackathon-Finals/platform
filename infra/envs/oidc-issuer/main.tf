# T48 / ADR-0035: public OIDC issuers for clusters the internet cannot reach (on-prem, GCE RKE2), so
# Tailscale can verify their operator's ServiceAccount token. Per environment only two objects are
# public: the discovery document and the public keys. Keys come from Git (issuers/<env>/jwks.json), so a
# change is a reviewed PR and only this stack's apply writes the bucket (#659 review 4).

locals {
  dir    = coalesce(var.issuers_dir, "${path.module}/issuers")
  envs   = toset([for f in fileset(local.dir, "*/jwks.json") : dirname(f)])
  base   = "https://${var.bucket_name}.s3.${var.region}.amazonaws.com"
  issuer = { for env in local.envs : env => "${local.base}/${env}" }
  # Private RSA/EC/symmetric members: a JWKS that carries one would publish a signing key.
  private_members = ["d", "p", "q", "dp", "dq", "qi", "oth", "k"]
}

resource "aws_s3_bucket" "issuer" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_ownership_controls" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "issuer" {
  bucket                  = aws_s3_bucket.issuer.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = false # the policy opens two object paths per issuer, nothing else
  restrict_public_buckets = false
}

resource "aws_s3_bucket_versioning" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256" # anonymous readers cannot use a KMS key
    }
  }
}

resource "aws_s3_bucket_policy" "issuer" {
  bucket = aws_s3_bucket.issuer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "PublicIssuerDocuments"
        Effect    = "Allow"
        Principal = "*"
        Action    = "s3:GetObject"
        Resource = flatten([for env in sort(tolist(local.envs)) : [
          "${aws_s3_bucket.issuer.arn}/${env}/.well-known/openid-configuration",
          "${aws_s3_bucket.issuer.arn}/${env}/openid/v1/jwks",
        ]])
      },
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.issuer.arn, "${aws_s3_bucket.issuer.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.issuer]
}

resource "aws_s3_object" "discovery" {
  for_each     = local.envs
  bucket       = aws_s3_bucket.issuer.id
  key          = "${each.key}/.well-known/openid-configuration"
  content_type = "application/json"
  content = jsonencode({
    issuer                                = local.issuer[each.key]
    jwks_uri                              = "${local.issuer[each.key]}/openid/v1/jwks"
    response_types_supported              = ["id_token"]
    subject_types_supported               = ["public"]
    id_token_signing_alg_values_supported = ["RS256"]
  })
}

resource "aws_s3_object" "jwks" {
  for_each     = local.envs
  bucket       = aws_s3_bucket.issuer.id
  key          = "${each.key}/openid/v1/jwks"
  content_type = "application/json"
  content      = jsonencode(jsondecode(file("${local.dir}/${each.key}/jwks.json")))

  lifecycle {
    precondition {
      condition = alltrue([for key in jsondecode(file("${local.dir}/${each.key}/jwks.json")).keys :
      length(setintersection(keys(key), local.private_members)) == 0])
      error_message = "issuers/${each.key}/jwks.json holds a private key member: publish public keys only."
    }
  }
}
