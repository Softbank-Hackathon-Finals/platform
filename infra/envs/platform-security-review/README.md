# Security Review platform prerequisites

The Security Review chart creates the `platform-state/security-review-audit` claim. The platform builder creates
its retained local volume and the node directory; neither is created by the GitOps workload.

1. On the current platform node, create the dedicated directory without reusing database or T15 storage:

   ```sh
   install -d -o 10001 -g 10001 -m 0700 /var/lib/bravo-security-review-audit
   ```

2. Render for the exact current node and use server dry-run before applying:

   ```sh
   python3 infra/envs/platform-security-review/render.py --node "$PLATFORM_NODE" > "$PRIVATE/security-review-audit-pv.json"
   kubectl apply --dry-run=server -f "$PRIVATE/security-review-audit-pv.json"
   kubectl apply -f "$PRIVATE/security-review-audit-pv.json"
   ```

The volume is `1Gi`, `ReadWriteOnce`, `Retain`, and bound only to
`platform-state/security-review-audit`. The node directory is owned by the service UID/GID `10001` with mode
`0700`. This storage preserves the local append-only review log across Pod replacement; it is not an HA or backup
claim.
