# Rehearsal host kubectl

`main.go` is the unchanged Apache-2.0 entry point from Kubernetes v1.35.10:
https://github.com/kubernetes/kubernetes/blob/v1.35.10/cmd/kubectl/kubectl.go
The Kubernetes modules remain v0.35.10, compatible with the registered v1.35 target.
The host builder compiles Linux/amd64 with Go 1.26.10, CGO disabled, the committed
module graph and `-mod=readonly -trimpath -buildvcs=false`.

The official v1.35.10 binary embeds `golang.org/x/net` v0.56.0 and `golang.org/x/text`
v0.39.0. The locked graph uses v0.60.0 and v0.41.0 for CVE-2026-78669 and
CVE-2026-56851. No command implementation, API behavior, TLS checks or auth
plugins are replaced. The reported version is `v1.35.10-bravo.1` with a modified
build state; this is not represented as the official upstream binary.

`go.sum` verifies module content. The host build receipt includes the compiled
binary SHA256 and the image source commit. Build and inspect the resulting
image before deployment; a successful source build does not establish runtime
security or cluster connectivity. No scanner exception is added here.
