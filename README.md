# Copilot CLI Kubernetes MVP (local kind)

Development-only starter for WSL2 + Docker Engine + kind. It runs analysis/proposal jobs in a separate Pod and requires an explicit human approval before a second job attempts to apply the patch and open a PR.

## Important limitations
- This is a development MVP, not production-ready. The web UI/API has no user authentication or CSRF protection. Keep it reachable only through `kubectl port-forward` on the local machine.
- The proposal patch and logs are returned via Kubernetes Pod logs and stored in SQLite. Review the patch independently before approval.
- For approval/apply, `repo-write` must contain a write-enabled token accepted by GitHub CLI. Use a test repository and least privilege. Protect the default branch with required reviews/checks.
- GitHub Enterprise Server: add its hostname to `ALLOWED_GIT_HOSTS` and configure network/TLS trust for the runner image. `gh` may require `GH_HOST` or Enterprise-specific setup; validate against your GHES version before enabling write mode.
- The `copilot-auth` token needs to be a token type supported by Copilot CLI (for example a fine-grained PAT with Copilot Requests permission). `repo-access` is optional for public clones; for private repos add key `REPO_TOKEN`. For write/PR creation add `repo-write` with key `WRITE_TOKEN`.
- Secrets passed as environment variables can be read by sufficiently privileged cluster users. This is local-dev only; use a secret manager and Kubernetes encryption/RBAC in shared environments.
- Copilot tool restrictions are defense in depth, not a complete sandbox. Treat repository content as untrusted and avoid running against sensitive repos until threat-model controls are in place.

## Build images
Run from this folder in WSL:

```bash
docker build -t copilot-runner:0.2 ./runner
docker build -t copilot-api:0.1 -f backend/Dockerfile .
kind load docker-image copilot-runner:0.2 --name copilot-test
kind load docker-image copilot-api:0.1 --name copilot-test
```

## Secrets
Create secrets in the target namespace. Do not commit values to files or shell history.

```bash
kubectl create namespace copilot-mvp
read -rsp 'Copilot token: ' T; echo
kubectl -n copilot-mvp create secret generic copilot-auth --from-literal=COPILOT_GITHUB_TOKEN="$T"
unset T
# For private repository clone (optional for public repos):
read -rsp 'Read token: ' T; echo
kubectl -n copilot-mvp create secret generic repo-access --from-literal=REPO_TOKEN="$T"
unset T
# Only for approved apply + PR; use a dedicated least-privilege test token:
read -rsp 'Write/PR token: ' T; echo
kubectl -n copilot-mvp create secret generic repo-write --from-literal=WRITE_TOKEN="$T"
unset T
```

For public-repo read-only tests, only `copilot-auth` is required; the optional `repo-access` and `repo-write` references do not prevent analysis/proposal Jobs from starting.

## Deploy

The hostPath-backed local PV lives inside the kind node container. Give the non-root API UID write access before starting the deployment:

```bash
docker exec copilot-test-control-plane sh -c 'mkdir -p /mnt/data/copilot-mvp && chown 10001:10001 /mnt/data/copilot-mvp'
```

Then apply the manifests:

```bash
kubectl apply -f k8s/namespace-rbac.yaml
kubectl apply -f k8s/app.yaml
kubectl -n copilot-mvp rollout status deployment/copilot-api
kubectl -n copilot-mvp port-forward service/copilot-api 8080:8080
```

Open http://localhost:8080. API health: `curl http://localhost:8080/api/health`.

## Conversations, fixes, and pull requests

1. Describe the analysis or fix you need. Select an Agent profile (general, security, reliability) and optionally up to three skills (input validation, error handling, regression tests). There is no mode selector: the request and profiles guide the response; fixes can produce a patch for review.
2. Select **Conversazione** on a request to read its persisted turns. Send another message for clarification, reanalysis, or a fix, selecting the appropriate Agent and skills. Only one job per conversation may run at a time.
3. Review the proposed patch in the conversation or request details. Select **Verifica patch e crea PR**, enter the approver identity and a **new branch name**, then confirm. Approval applies the saved patch; it does not ask Copilot to generate a different fix.
4. The apply runner checks that the repository's default-branch commit still matches the proposal, rejects an existing remote branch, creates the user-selected branch in the original repository, and attempts to open a PR targeting the default branch. The push uses an empty-ref lease so even a concurrently created branch cannot be overwritten.
5. Job errors and the resulting PR link appear in the conversation. A failed apply can be retried with a different branch name. Active/completed apply records prevent duplicate approval. If the base commit changed, generate a new proposal instead.

### Context and profiles

- Each turn is a fresh isolated clone, not a resumed Copilot CLI filesystem/session. SQLite stores the complete history, but the next prompt includes at most the six most recent turns, up to 4,000 task characters and 8,000 report characters per turn, with a final 48,000-byte context cap. Earlier patches are not automatically applied to later analysis/proposal clones. Describe the desired refinement in the new message.
- Agent/skill instructions are Markdown files in [backend/agents](backend/agents) and [backend/skills](backend/skills). Edit the files to change instructions; add a `.md` file to add a profile. The filename without extension is its API/UI identifier: use lowercase letters, digits and single hyphens (for example `code-review.md`). Keep `general.md` as the default Agent. Files are read on each configuration/prompt request, without restarting the backend. Only server-managed regular files are loaded, not symlinks or paths supplied by users. These are application instruction profiles, not native `--agent` configurations or a multi-agent system; they do not expand tool permissions or guarantee tests were executed.
- Repository custom instructions and built-in MCP servers are disabled for the assistant invocation. Shell execution remains denied. Treat model output and repository content as untrusted; these restrictions do not constitute a complete sandbox.
- Native agents, trusted skill bundles, and true CLI session resume remain future extensions: they require a server-managed allowlist, versioned trusted files, and protected per-conversation session storage. Do not load repository-supplied plugins or grant all tools merely to enable a profile.
- Read/propose jobs do not receive `WRITE_TOKEN`. Only approved apply jobs receive it; those jobs run Git/GitHub CLI, not Copilot. Use a read-only `REPO_TOKEN` and least-privilege write credentials.
- Existing SQLite data is migrated in place and retained. Deleting a request removes that turn from the stored conversation as well; deletion is not an immutable audit mechanism.
- If pushing the branch succeeds but PR creation fails, the remote branch remains. Check GitHub before retrying, choose a different new branch if needed, and remove unused branches manually after review.

### Deployment and verification

Run `bash deploy.sh` from WSL to rebuild/load **both API and runner images** with a unique tag, update `RUNNER_IMAGE`, and roll out the API. Back up your SQLite volume before updating. No runner-image rebuild is needed for a change to instruction-profile text alone, but runner script/CLI changes require it. Existing Jobs continue using their original image.

The Markdown profiles are bundled in the API image. After editing local files, rebuild and roll out that image to update Kubernetes; editing files already mounted into a running backend takes effect on the next request.

The conversation API adds `GET /api/requests/{id}/conversation` and `POST /api/requests/{id}/followup`. Creation/followup bodies accept `agent` and `skills`, without `mode`. Internally these turns use `propose`, allowing either a report or a patch; historical modes remain stored, and `apply` is only launched through approval. Analysis-only behavior is guided by the prompt, not enforced by a separate read-only tool permission. Approval requires both `approved_by` and `branch_name`.

Security boundaries for this feature:

| Requirement | Verification / limitation | Security domain |
|---|---|---|
| Human approval before repository writes | Saved patch + base commit required; explicit reviewer and branch | Access Control / IAC |
| No branch/command injection or overwrites | Server branch allowlist, quoted Bash variables, Git ref checks, empty-ref lease | Input Validation / WEB |
| Restrict write credentials | `WRITE_TOKEN` injected only for apply | Data Protection / DCH |
| Bound untrusted conversation input | Request/profile validation, escaped UI rendering, bounded prompt context | API Security / WEB |
| Preserve review history and visible failures | SQLite migration and error persistence; deletions remain destructive | Logging / MON |
| Local-only access | No authenticated user identity, authorization, or centralized immutable audit; not suitable for shared/public deployment | Authentication / IAC |

No production authentication, stronger sandbox, or secret-manager integration is added by this feature. These remain prerequisites before processing sensitive repositories or exposing the service to other users. Test coverage uses temporary local Git repositories and fake Copilot/GitHub CLI commands; it does not validate a real Copilot response, GitHub token permissions, or a live PR creation.

## Request history

- Recent requests use the full page width. Select **Apri dettagli** to read the complete repository URL, task, job name, status, timestamp, base commit, report, errors, and pull request link in a larger view.
- Select **Elimina** and confirm to permanently delete a request and its stored report/patch from SQLite. This does not modify the repository, pull requests, or Kubernetes Jobs.
- Active jobs cannot be deleted from history. Requests whose Jobs have already expired can be deleted. If Kubernetes cannot be reached, deletion is refused until the job status can be verified.
- The API endpoint is `DELETE /api/requests/{request_id}`. Like the rest of this development-only API, it has no user authentication; keep it accessible only through local port-forwarding.
- Rebuild and redeploy both the backend and frontend to enable deletion. If using the included deployment script, run `bash deploy.sh` from WSL, then reload the page at http://localhost:8080.

Run the backend and local runner tests without requiring a cluster or third-party Python packages (Git and Bash are required for runner tests):

```bash
python3 -m unittest discover -s backend -p test_requests.py -v
```

## Troubleshooting

```bash
kubectl -n copilot-mvp get pods,jobs,pvc
kubectl -n copilot-mvp logs deployment/copilot-api
kubectl -n copilot-mvp get events --sort-by=.lastTimestamp
kubectl -n copilot-mvp logs job/<job-name>
```

### GitHub connection failures with corporate DNS search domains

If WSL and the Kind node can reach GitHub but a pod cannot, compare DNS resolution inside and outside the pod. Kubernetes normally uses `ndots:5`; inherited corporate search domains can make `github.com` resolve through a search suffix to an incorrect address (including `127.0.0.1`). If the absolute DNS name `github.com.` resolves correctly, this is a search-order issue, not a repository-token error.

Runner Jobs explicitly use `dnsConfig.options: [{name: ndots, value: "1"}]` so dotted external names are resolved before search suffixes. This preserves cluster DNS, corporate resolvers, and TLS verification; do not hardcode GitHub IP addresses or disable certificate checks. Rebuild/redeploy the API with `bash deploy.sh`, then submit a new request. Existing failed Jobs are not restarted or changed by this setting.

To configure GHES, edit `ALLOWED_GIT_HOSTS` in `k8s/app.yaml`, rebuild/redeploy if you change application code, and ensure the runner can reach the GHES hostname and trusts its certificate. Do not disable TLS verification.
