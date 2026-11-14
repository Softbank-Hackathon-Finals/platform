"""Shared source policies for the neutral translation tests. Identifiers are documentation examples."""

import json

from rehearsal_runner.neutral import Workload
from rehearsal_runner.neutral.export import S3_ARN

BUCKET = "demo-chat-attachments"
APP = Workload("demo-chat", "demo-chat")
AWS_ROLE = "arn:aws:iam::111122223333:role/demo-chat"  # AWS documentation example account
GKE_PRINCIPAL = ("principal://iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/"
                 "example-project.svc.id.goog/subject/ns/demo-chat/sa/demo-chat")

# Each source grants the app read/write/delete/list, the public read, and one principal the
# deploy trace does not know.
SOURCES = {
    "aws": json.dumps({"Version": "2012-10-17", "Statement": [
        {"Sid": "App", "Effect": "Allow", "Principal": {"AWS": AWS_ROLE},
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"], "Resource": f"{S3_ARN}{BUCKET}/*"},
        {"Sid": "AppList", "Effect": "Allow", "Principal": {"AWS": AWS_ROLE}, "Action": "s3:ListBucket",
         "Resource": f"{S3_ARN}{BUCKET}"},
        {"Sid": "Public", "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
         "Resource": f"{S3_ARN}{BUCKET}/*"},
        {"Sid": "Batch", "Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::111122223333:role/batch"},
         "Action": "s3:GetObject", "Resource": f"{S3_ARN}{BUCKET}/*"},
    ]}),
    "gcp": json.dumps({"version": 1, "resourceId": f"projects/_/buckets/{BUCKET}", "bindings": [
        {"role": "roles/storage.objectUser", "members": [GKE_PRINCIPAL]},
        {"role": "roles/storage.legacyObjectReader", "members": ["allUsers"]},
        {"role": "roles/storage.legacyObjectReader",
         "members": ["serviceAccount:batch@ops.example"]},
    ]}),
    "onprem": json.dumps({"identities": [
        {"name": "demo-chat", "actions": [f"Read:{BUCKET}", f"Write:{BUCKET}", f"List:{BUCKET}"]},
        {"name": "anonymous", "actions": [f"Read:{BUCKET}"]},
        {"name": "batch", "actions": [f"Read:{BUCKET}"]},
    ]}),
}
SOURCE_PRINCIPALS = {"aws": {AWS_ROLE: APP}, "gcp": {}, "onprem": {"demo-chat": APP}}

# Target-side identity of the app, per target kind.
TARGET_ROLE = "arn:aws:iam::222222222222:role/demo-chat-target"
TARGET_PRINCIPALS = {
    "aws": lambda w: TARGET_ROLE if w == APP else None,
    "gcp": lambda w: (GKE_PRINCIPAL if w == APP else None),
    "onprem": lambda w: "demo-chat" if w == APP else None,
}
ONPREM_BASE = {"identities": [{"name": "demo-chat", "actions": []}]}
