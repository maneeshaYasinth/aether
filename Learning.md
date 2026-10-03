# Aether — Learning Log

A running record of concepts learned while building Aether, written for my own future reference (and future interview prep). Updated as the project progresses.

---

## Phase 1: Local GitOps loop (k3s + Terraform + ArgoCD)

### The big picture problem

Modern apps run across many machines and need constant management — starting, restarting, scaling, updating. **Kubernetes** automates this: you declare the desired state ("3 copies of my app running") and it continuously makes that true.

But Kubernetes needs to be told *what* to run. Doing that by typing commands by hand doesn't scale and leaves no record of what happened — that's the gap GitOps fills.

### Core concepts

**Kubernetes** — A system that runs and manages containerized applications across a cluster of machines. You declare desired state; it reconciles reality to match.

**k3s** — A lightweight, real distribution of Kubernetes, good for a single laptop. Running its install script is the moment an actual cluster comes into existence.

**kubectl** — The command-line tool used to inspect and interact with whatever's running inside a cluster. Kubernetes is the engine; `kubectl` is the dashboard.

**Terraform** — Infrastructure-as-code. Instead of creating resources by clicking around, you write down what you want in a text file, and Terraform makes it real. Benefit: infrastructure becomes reviewable, versioned, and reproducible — not a fragile memory of manual steps.

**Helm** — A packaging format for Kubernetes applications. Complex software (like ArgoCD) needs dozens of interlocking pieces of config; a Helm "chart" bundles all of that so you only need to override a handful of settings rather than hand-write everything.

**Terraform's `helm` provider** — Distinct from the `helm` CLI. The CLI is a tool *I* run manually to research chart versions (`helm search`). The provider is Terraform code that manages Helm installs as part of `terraform apply` — keeping the "everything through Terraform" discipline consistent with GuardRail.

**ArgoCD** — Watches a git repository and continuously ensures the cluster's actual state matches what's declared there. If they ever drift (e.g. someone manually edits something), ArgoCD reverts it. This practice is called **GitOps**: git becomes the single source of truth for the running system, not just the codebase.

**Chart version vs. app version** — A Helm chart has its own version number (the packaging), separate from the version of the software it installs (e.g. chart `10.9.2` installs ArgoCD `v3.5.3`). Terraform's `helm_release` resource wants the *chart* version, not the app version — a common point of confusion.

**App-of-apps pattern** — Rather than registering each application in ArgoCD's UI by hand, you create *one* Application that points at a git folder containing other Application manifests. From then on, adding a new app to the platform means adding a YAML file to that folder and pushing — ArgoCD notices and deploys it automatically.

**Self-heal** — Part of ArgoCD's sync policy. If the live cluster state drifts from what's declared in git (e.g. someone manually scales a deployment), ArgoCD detects the mismatch and reverts it back to match git — automatically, without intervention.

### What I actually built and did, in order

1. Installed k3s → created a real Kubernetes cluster on my laptop.
2. Set up `kubectl`, pointed at the cluster's kubeconfig.
3. Installed Terraform and Helm CLI as local tools.
4. Wrote a Terraform module (`modules/argocd-bootstrap`) declaring "install ArgoCD via Helm."
5. `terraform init` — downloaded the `helm` and `kubernetes` provider plugins.
6. `terraform plan` — dry run, confirmed exactly 2 resources would be created (a namespace + the Helm release), nothing more.
7. `terraform apply` — actually installed ArgoCD onto the cluster.
8. Verified all ArgoCD pods were `Running`, logged into the web UI.
9. Wrote two YAML files defining a trivial test app (`hello-nginx`: a Deployment + a Service).
10. Wrote an ArgoCD `Application` manifest pointing at that folder in git.
11. Wrote a `root-app` Application that watches the *folder of Application manifests* (`gitops/apps/`) — the app-of-apps pattern.
12. Applied the root app manually, once. From that point on, all app changes flow through git commits, not manual `kubectl apply`.
13. Deliberately drifted the cluster (`kubectl scale --replicas=5`) and watched ArgoCD's self-heal revert it back to the git-declared `replicas: 1` — concrete proof the GitOps loop actually enforces, not just applies once.

### Key mistakes/gotchas hit along the way (worth remembering)

- `~/.kube/config` didn't exist until k3s was actually installed — Terraform's `kubernetes` provider needs this file to know how to reach the cluster.
- Helm wasn't installed by default on Ubuntu — needed the official install script (avoided the snap package due to potential sandboxing/permission issues with files like `~/.kube/config`).
- Chart version (`10.9.2`) ≠ ArgoCD app version (`v3.5.3`) — easy to plug the wrong number into Terraform.
- Terraform did **not** create the Kubernetes cluster in Phase 1 — k3s's installer did that. Terraform only manages what's deployed *onto* an already-existing cluster here. (This flips in Phase 2 — see below.)

---

## Phase 2: EKS (cloud)

The second phase moved the platform from a local k3s cluster to a real AWS EKS
cluster. Unlike Phase 1, Terraform provisions the Kubernetes control plane and
worker nodes here instead of installing software onto a cluster created by an
external installer.

### What I built

- A VPC with DNS support and DNS hostnames enabled.
- Public and private subnets across `us-east-1a` and `us-east-1b`.
- EKS subnet tags for public load balancers and internal load balancers.
- An internet gateway for public traffic and one NAT gateway for private nodes.
- An EKS cluster pinned to Kubernetes `1.31`.
- A managed node group using `t3.micro` instances in private subnets.
- Separate IAM roles and managed policies for the EKS control plane and nodes.
- Terraform Kubernetes and Helm providers authenticated through the EKS cluster
  endpoint and `aws_eks_cluster_auth`.
- The shared Argo CD bootstrap module, configured for NodePort access and plain
  HTTP during local development.

### What I verified

1. Terraform created the AWS networking resources, EKS control plane, and managed
   node group.
2. Argo CD pods reached `Running` and `Ready` on the EKS cluster.
3. The Argo CD UI was reached through:

   ```bash
   kubectl -n argocd port-forward svc/argocd-server 8080:80
   ```

4. The UI opened at `http://localhost:8080`.

### Lessons and troubleshooting

- EKS worker nodes belong in private subnets; the NAT gateway provides outbound
  access without assigning public IP addresses to the nodes.
- A single NAT gateway keeps this portfolio environment affordable, but it is a
  single point of failure and is not the production multi-AZ design.
- Argo CD was configured with `server.insecure: true`, so the local forwarded URL
  uses HTTP rather than HTTPS.
- `kubectl port-forward` can complete the API WebSocket upgrade and still lose
  the connection afterward if the pod-side connection is reset. In this case the
  Argo CD pod was healthy with zero restarts, and starting a fresh forward through
  the Service worked:

  ```bash
  kubectl -n argocd port-forward svc/argocd-server 8080:80
  ```

---

## Phase 4: Metrics pipeline (metrics-server)

### What it is

**metrics-server** reads current CPU and memory usage from every node's kubelet and publishes it through the Kubernetes **Metrics API** (`metrics.k8s.io`). That's the API behind `kubectl top`.

It only knows **right now** — no history. That was a deliberate scope decision: Phase 5 (rightsizing) only needs a current snapshot, so Prometheus (which stores history) is deferred until Phase 6 (Prophet forecasting) genuinely needs time-series data.

### Things worth remembering

- **`--kubelet-insecure-tls`** — k3s's kubelet uses a self-signed certificate that metrics-server doesn't trust by default. Without this flag, metrics-server installs fine but silently reports nothing. Fine for a laptop cluster; review it for production.
- **Installing from a public Helm repo** — `gitops/apps/metrics-server.yaml` points Argo CD straight at `https://kubernetes-sigs.github.io/metrics-server/` instead of at a folder in this repo. First time using that Argo CD capability.
- **k3s already ships a metrics-server.** The Helm version clashed with it (`field is immutable`, because Deployment selectors can't change after creation). Fixed with `Replace=true` and removing the k3s-owned copy. Lesson: check whether your Kubernetes distribution already installs something before adding it via GitOps. (Full story in `Aether-Learning-Guide.md` section 11.)

### How I verified Phase 4 was done

```bash
kubectl get applications -n argocd   # metrics-server: Synced / Healthy
kubectl top nodes                    # real CPU/memory numbers for the node
kubectl top pods -A                  # real numbers for every pod
```

---

## Phase 5: AI rightsizing analyzer

### The big picture

Phase 5 makes Aether actually *do* something with the metrics: an AI reads real cluster usage and **proposes** changes, as a GitHub pull request a human must approve. The full loop:

```
CronJob (every 6h, inside the cluster)
   │
   ▼
collector ──► reads metrics-server usage + each pod's requests/limits
   │
   ▼
analyzer ──► sends the numbers to Gemini, gets structured JSON advice back
   │
   ▼
proposer ──► validates the advice, edits the YAML, opens a GitHub PR
   │
   ▼
human reviews & merges ──► Argo CD sees the new commit ──► cluster updated
```

This is the difference from GuardRail: GuardRail's AI *explained* findings; Aether's AI *recommends concrete changes* — but never applies them itself.

### Kubernetes concepts

**Requests vs. limits** — every container can declare both, for CPU and memory:

| | What it means | What happens if exceeded |
|---|---|---|
| **Request** | The amount *reserved* for the container. The scheduler only places a pod on a node with this much free. | Nothing — it's a reservation, not a cap. |
| **Limit** | The maximum the container is *allowed* to use. | **CPU:** the container is slowed down (throttled). **Memory:** the container is killed (`OOMKilled`). |

That difference matters: going over a CPU limit makes you slow; going over a memory limit makes you crash. That's why memory limits get extra headroom (at least 1.5× the request).

**Rightsizing** — adjusting requests/limits to match real usage. Requests that are too high waste capacity: that CPU/memory is reserved and no other pod can use it, even if it sits idle. Requests that are too low (or missing) mean pods get squeezed or evicted under pressure.

**Units:**
- **CPU in millicores:** `1000m` = 1 full CPU core. `250m` = a quarter of a core. `10m` = 1% of a core.
- **Memory in mebibytes:** `Mi` = 1024 × 1024 bytes. Note `Mi` ≠ `MB` — Kubernetes rejects `32MB`; it must be `32Mi`.
- Kubernetes reports usage in mixed units (`12345n` nanocores, `9216Ki` kibibytes), so the collector converts everything to millicores and MiB first.

**Missing requests** — a container with no requests (most of Argo CD and k3s's own pods here) is a finding by itself: the scheduler is guessing, and those pods are the first to be evicted when the node runs short.

**Owner references (pod → ReplicaSet → Deployment)** — you never edit pods directly. A **Deployment** creates a **ReplicaSet**, which creates the **pods**. Each object records its "owner". The collector follows this chain so recommendations name the thing you'd actually edit (`Deployment/hello-nginx`), not a generated name like `hello-nginx-847949887d`. StatefulSets and DaemonSets own their pods directly.

**Rolling update** — when a Deployment's pod template changes (e.g. new resources), Kubernetes creates a *new* ReplicaSet with new pods and scales the old one to 0. Pod resources can't be edited in place, so every resources change = new pod with a new name.

**ServiceAccount** — an identity for a *pod* (as opposed to a human). When code inside a pod calls the Kubernetes API, it authenticates as its ServiceAccount. The Python client finds these credentials automatically (`load_incluster_config()`).

**RBAC (Role-Based Access Control)** — what an identity is allowed to do. Three pieces:
- **ClusterRole** — a list of allowed actions (`verbs`) on resource types. Ours allows only `get`/`list` on pods, `get` on replicasets, and `get`/`list` on pod metrics. Nothing that changes anything.
- **ClusterRoleBinding** — connects that role to the ServiceAccount.
- *Cluster*Role (not Role) because the analyzer reads pods in **every** namespace. A plain Role only covers one namespace.

Test permissions without running anything:
```bash
kubectl auth can-i list pods --all-namespaces --as=system:serviceaccount:aether-analyzer:aether-analyzer   # yes
kubectl auth can-i delete pods -n hello-nginx --as=system:serviceaccount:aether-analyzer:aether-analyzer    # no
```

**Principle of least privilege** — give each identity only the permissions it truly needs. The analyzer can read, never write; its only path to changing the cluster is a PR a human approves.

**CronJob** — runs a Job on a schedule (cron syntax). `0 */6 * * *` = minute 0 of every 6th hour. Settings used:
- `concurrencyPolicy: Forbid` — never start a new run while the previous is still going.
- `activeDeadlineSeconds: 600` — kill a stuck run after 10 minutes.
- `backoffLimit: 1` — retry a failed run once, not forever.
- `successfulJobsHistoryLimit` / `failedJobsHistoryLimit: 3` — keep the last 3 for debugging, auto-delete older ones.

Trigger one by hand instead of waiting for the schedule:
```bash
kubectl -n aether-analyzer create job --from=cronjob/aether-analyzer manual-test-N
```
Gotcha: `--from` copies the CronJob **as it is at that moment**. Create the Job *after* Argo CD has synced a change, or it runs the old version.

**Secret** — how Kubernetes passes sensitive values (API keys) into pods, here as environment variables via `envFrom`. Important: a Secret is only **base64-encoded, not encrypted**. Anyone who can read it can decode it — which is why it is created by hand with `kubectl` and **never committed to Git**. This is the one piece of Aether that isn't GitOps-managed. Proper fixes: **Sealed Secrets** or **External Secrets Operator** (future improvement).

**Pod securityContext** — hardening for the container:
- `runAsNonRoot` / `runAsUser: 10001` — not root, so a compromised container has fewer powers.
- `allowPrivilegeEscalation: false` — can't gain more privileges later.
- `capabilities: drop: ["ALL"]` — removes all special Linux powers.
- `readOnlyRootFilesystem: true` — the container can't modify its own files (only `/tmp`, mounted as an `emptyDir`).

**Image pulls & caching** — k3s has its own image store (containerd), separate from Docker on the laptop. The first pull of our image took 56s; the next version took 5.6s because only the changed code layer was downloaded.

### Python / code concepts

**Collector does the maths, AI does the reasoning.** LLMs are unreliable at arithmetic, so all unit conversions and percentages (`pct_of_request`) are computed in Python before Gemini sees anything.

**Structured output (`responseSchema`)** — instead of free text, Gemini is forced to return JSON matching a schema (e.g. `issue` must be one of `over_provisioned`, `under_provisioned`, `missing_requests`, `missing_limits`, `ok`). Structured output can be validated, sorted, rendered and turned into code changes. Free text can only be read.

**Prompt rules, not opinions** — the prompt states explicit rules (under 20% of request = over-provisioned; recommend ~2× usage; floors of 10m CPU / 32Mi memory). This makes results consistent between runs. `temperature: 0.2` reduces randomness further.

**Telling the AI the data's limits** — the prompt says this is a *single snapshot*, so peaks are invisible. Gemini responded by marking every recommendation **low confidence** — honest, and the real reason Phase 6 needs Prometheus history.

**AI output is untrusted input.** The proposer validates everything before writing:
- quantities must match Kubernetes notation (`"ten cores"` and `"32MB"` are rejected),
- a limit can't be below its request,
- only files in `EDITABLE_MANIFESTS` (currently just hello-nginx) can ever be edited,
- if the edit changes nothing, no PR is opened,
- only one rightsizing PR may be open at a time.

Proven in practice: the smaller fallback model once recommended "10m → 10m" (a no-op) and contradicted itself on coredns — the proposer saw the file didn't change and correctly opened no PR.

**HTTP status codes from Gemini:**

| Code | Meaning | Right response |
|---|---|---|
| 400 / 401 / 403 | Our request or key is wrong | Stop — retrying won't help |
| 404 | Model doesn't exist (e.g. retired) | Try a different model |
| 429 | Too many requests / quota used up | Usually a quota — move on to another model |
| 503 | Server overloaded | Wait and retry — usually temporary |

**Exponential backoff** — wait 2s, 4s, 8s, 16s between retries instead of hammering every second. An overloaded server recovers faster when clients back off. Standard for any cloud API.

**Model fallback** — if the preferred model keeps failing, ask the API which Flash models this key can use (`GET /models`) and try those. Same idea as GuardRail. The logs say which model actually answered, because a smaller model (flash-lite) gives noticeably vaguer reasoning.

**ruamel.yaml vs. PyYAML** — PyYAML throws away comments and reorders keys when it rewrites a file, which would make every PR diff noisy. ruamel.yaml preserves comments and layout, so the PR shows only the four lines that actually changed. (Side effect: it also preserves *outdated* comments — it can't know a comment is stale.)

**Virtual environment (venv)** — a private folder of Python packages for one project (`ai-analyzer/.venv/`), so nothing is installed system-wide. Ubuntu needs `python3.X-venv` installed before `python3 -m venv` works. `.venv/` is in `.gitignore`.

### GitHub concepts

**Opening a PR through the API** — the proposer does what you'd do in the UI, via the GitHub REST API:
1. read the file from `main` (`GET /contents/...`),
2. create a branch `aether/rightsizing-<timestamp>` (`POST /git/refs`),
3. commit the edited file to it (`PUT /contents/...`),
4. open the PR (`POST /pulls`) with the report as its description.

**Fine-grained personal access token** — scoped to one repo (`aether`) with only *Contents: write* and *Pull requests: write* (plus mandatory *Metadata: read*). If it leaks, the damage is limited to opening PRs on this one repo. Has an expiry date — renew it and update the Kubernetes Secret when it expires.

**GitHub Actions workflow triggers** (`.github/workflows/analyzer-image.yml`):
- `paths: ["ai-analyzer/**", ...]` — only runs when analyzer code changes, not on README edits.
- `pull_request` → build only (proves the Dockerfile still works). `push` to `main` → build **and** publish.
- The workflow's built-in `GITHUB_TOKEN` (with `packages: write`) logs into GHCR — no secret to create.

**GHCR (GitHub Container Registry)** — stores Docker images next to the repo: `ghcr.io/maneeshayasinth/aether-analyzer`. Names must be lowercase. Because the repo is public, the image became public too, so k3s can pull it without credentials.

### Docker concepts

**Dockerfile layer caching** — each instruction is a cached layer. `COPY requirements.txt` + `pip install` come *before* copying the code, so changing only Python code skips reinstalling dependencies (rebuild in seconds).

**`python:3.14-slim`** — ~50 MB base instead of ~1 GB for the full image. Final image ≈ 60 MB compressed.

**Non-root user** — `USER 10001` after installing packages: installs run as root during the build (that's what the pip "running as root" warning is about — harmless in a build), but the container *runs* as an unprivileged user. Check with `docker run --rm --entrypoint id aether-analyzer:local`.

**Image tags: `sha-<commit>` vs `latest`** — the CronJob is pinned to an exact tag like `sha-82c549e`, so you always know which code is running, and every upgrade is a Git commit. `latest` changes underneath you, so it's only for manual testing.

**Rolling out new analyzer code is two commits:**
1. Commit code → CI builds `sha-<new>`.
2. Wait for the green build, then edit the tag in `gitops/charts/ai-analyzer/cronjob.yaml` and commit → Argo CD rolls it out.

If you bump the tag before the image exists, pods fail with `ImagePullBackOff`. Tools like **Argo CD Image Updater** automate step 2 (future improvement).

### What I actually built and did, in order

1. Added deliberately over-provisioned requests to hello-nginx (250m / 128Mi while using ~0m / ~10Mi) — a test case with a known right answer.
2. Wrote the **collector** (`ai-analyzer/collector/collect.py`) and ran it against the cluster.
3. Taught it to follow pod → ReplicaSet → Deployment.
4. Wrote the **analyzer** (`ai-analyzer/analyzer/analyze.py`): prompt rules + JSON schema + Gemini REST call + Markdown report.
5. Wrote the **proposer** (`ai-analyzer/proposer/propose.py`) and `run.py` (whole pipeline).
6. Created a fine-grained GitHub token and opened **PR #1** from my laptop.
7. Merged PR #1 → Argo CD deployed commit `a41e55b` → hello-nginx now runs at 10m / 32Mi. Verified in Argo CD's *History and Rollback* view and via `kubectl get application hello-nginx -n argocd -o jsonpath='{.status.sync.revision}'`.
8. Wrote a **Dockerfile** and a **GitHub Actions** workflow that publishes to GHCR.
9. Wrote **RBAC** + **CronJob** manifests and an Argo CD Application (`gitops/apps/ai-analyzer.yaml`).
10. Created the namespace and **Secret** by hand, pushed, Argo CD deployed it.
11. Triggered manual Jobs; the analyzer ran inside the cluster, read 15 containers, called Gemini, and correctly opened no PR (nothing auto-editable to change).
12. Shipped a code change (always log the full report) through the two-commit image-tag flow.

### Problems hit and how they were solved

| Problem | Cause | Fix / lesson |
|---|---|---|
| `python3 -m venv` failed: "ensurepip is not available" | Ubuntu splits venv support into a separate package | `sudo apt install python3.14-venv` |
| `.gitignore` line became `kubectl.venv/` | The file had no trailing newline, so appended text joined the last line | Always check a file ends with a newline before appending |
| Gemini **404**: "gemini-2.5-flash is no longer available to new users" | Google retired the model for new API keys | Switched default to `gemini-3.8-flash`; the clear error message came from printing the response body |
| Gemini **503** "high demand", every attempt | Temporary overload on Google's side | Exponential backoff + automatic fallback to other Flash models |
| Report showed `null → 10m` | The model wrote the *word* "null" | Treat "null"/"none"/"n/a" as empty when rendering |
| VS Code: "Unable to resolve action `docker/setup-buildx-action@v3`" | The extension's lookup failed; v3 did exist — but versions were outdated | Bumped all actions to current majors; *Developer: Reload Window* cleared the stale error |
| Pushed, but no GitHub Action ran | `git add .github/...` was run from inside `ai-analyzer/`, so the path didn't exist and the workflow file was never committed | Git paths are relative to the current folder — run git from the repo root, check `git status` before committing |
| `kubectl logs -f` → `context deadline exceeded` | The container was still pulling the image (56s first time) | Not a failure; `kubectl describe pod` → *Events* shows `Pulling` / `Pulled` |
| `create job` → "already exists", logs had no report | `manual-test-2` was created before Argo CD synced the new image tag | Check the CronJob's image first, then use a new job name |
| Gemini 429 on two fallback models (~60s wasted retrying) | Free-tier quota on those models | Fixed: on a quota 429 (daily cap or zero quota), skip to the next model immediately |

### Useful Phase 5 commands

```bash
# Run the pipeline from the laptop (in ai-analyzer/)
.venv/bin/python collector/collect.py -n hello-nginx       # just the data
.venv/bin/python analyzer/analyze.py --dry-run             # just the prompt, no Gemini call
.venv/bin/python run.py --dry-run                          # full pipeline, show diff, touch nothing
.venv/bin/python run.py -i /tmp/recs.json                  # reuse saved advice, open a real PR

# The in-cluster analyzer
kubectl -n aether-analyzer get cronjob,jobs,pods
kubectl -n aether-analyzer get cronjob aether-analyzer -o jsonpath='{..image}'; echo
kubectl -n aether-analyzer create job --from=cronjob/aether-analyzer manual-test-N
kubectl -n aether-analyzer logs -f job/manual-test-N
kubectl -n aether-analyzer describe pod -l job-name=manual-test-N   # Events: image pull, start, errors

# RBAC checks
kubectl auth can-i list pods -A --as=system:serviceaccount:aether-analyzer:aether-analyzer

# Which Git commit is Argo CD running?
kubectl get application hello-nginx -n argocd -o jsonpath='{.status.sync.revision}'; echo

# Recreate the Secret (e.g. after the GitHub token expires)
kubectl -n aether-analyzer delete secret aether-analyzer-secrets
kubectl -n aether-analyzer create secret generic aether-analyzer-secrets \
  --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
  --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN"
```

---

## Housekeeping: one shared module, different settings per environment

**Problem:** `argocd-bootstrap` is used by both `environments/local` (k3s) and `environments/aws` (EKS). To get a reachable Argo CD on EKS, the Service type was hardcoded to `LoadBalancer`, which silently changed local as well. On k3s, Traefik already holds ports 80/443 through k3s's built-in load balancer (ServiceLB/Klipper), so a second LoadBalancer Service fights it for those ports, and `http://localhost:30080` disappears.

**Fix:** make the module take an *input variable* instead of a hardcoded value.

```hcl
# modules/argocd-bootstrap/variables.tf
variable "server_service_type" {
  type    = string
  default = "NodePort"          # safe, free default
  validation {
    condition     = contains(["NodePort", "LoadBalancer", "ClusterIP"], var.server_service_type)
    error_message = "..."
  }
}

# environments/aws/main.tf
module "argocd" {
  source              = "../../modules/argocd-bootstrap"
  server_service_type = "LoadBalancer"
}
```

**Concepts:**
- **A module is like a function; variables are its parameters.** If two callers need different behaviour, that difference should be a parameter, not an edit to the function body.
- **Pick the default that's cheap and safe.** On AWS a LoadBalancer Service creates a real, billed ELB. With `NodePort` as the default, a paid resource only appears when an environment asks for one explicitly.
- **`validation` blocks** catch typos (`"Loadbalancer"`) at `terraform plan` time instead of halfway through a Helm install.
- **Port 30080 isn't in our code.** It's the argo-cd Helm chart's default `server.service.nodePortHttp`. Worth knowing so you don't hunt for it.
- **Always read `terraform plan` before applying.** Here it also showed *unrelated* drift: an older commit (disabling dex/notifications/applicationSet) had never been applied to k3s. Plan shows the gap between code and reality, whatever caused it.

---

## Housekeeping: two analyzer follow-ups

**1. "Recommendations" that change nothing.** Sometimes Gemini labelled a container `over_provisioned` and then "recommended" exactly the values it already had. These are now relabelled `ok` in code (`mark_noops()` in `analyze.py`), so they drop out of the report and the PR.
- **Don't trust the model for things code can check.** The prompt already says "use ok when nothing needs to change", but an LLM follows instructions *most* of the time. A deterministic check after the call costs nothing and is always right.
- **Compare values, not strings.** `"10m"` and `"0.01"` are the same CPU; `"1Gi"` and `"1024Mi"` are the same memory. `parse_quantity()` converts both to plain numbers first.
- It runs in `run.py` too, so saved recommendations (`-i recs.json`) get the same treatment.
- Later extended: a row with **no** recommended values (Gemini saying "missing requests, but k3s manages this, leave it") is also relabelled `ok`. A recommendation you can't act on is noise.

**2. Not all 429s are equal.** HTTP 429 "Too Many Requests" covers two different situations:

| Kind | Example `quotaId` | Will waiting help? |
|---|---|---|
| Rate limit | `...PerMinute...` | Yes: back off and retry |
| Quota exhausted | `...PerDay...`, or `quotaValue: "0"` (model not on free tier) | No: not for hours, or never |

Google's error body has a `QuotaFailure` section that says which one it is. `quota_exhausted()` reads it, and for the second kind the code moves to the next model immediately instead of spending ~30s backing off on a model that can't answer.
- **Lesson:** read the error *body*, not just the status code. The status says "something went wrong"; the body usually says what to do about it.

**Gotcha found while applying:** after the apply, dex and notifications were gone but the ApplicationSet controller was still running. In this chart version (10.9.2) there is no `applicationSet.enabled` key, and **Helm silently ignores values it doesn't recognise**, so no error appeared. The fix is `applicationSet.replicas = 0`. Lesson: after changing chart values, check the result (`kubectl get pods`) instead of trusting a green apply, and look up keys in `helm show values <chart> --version <v>` for the exact version you pin.

**Acting on the analyzer's own finding:** the next run flagged every Argo CD pod for *missing requests*: the chart ships `resources: {}`. Those values live in Terraform (the Helm release), not in `gitops/`, so the analyzer can't open a PR for them. They were added by hand in `argocd-bootstrap/main.tf`, using a `values = [yamlencode({...})]` block instead of a dozen `set` blocks.
- **Use the AI as a starting point.** It suggested e.g. 10m/200Mi for the application controller from one snapshot. We went more generous (50m/256Mi) because the controller and repo-server spike during syncs, which a single snapshot can't see.
- **Memory limit, no CPU limit.** Going over a memory limit gets the container OOM-killed, so set one with headroom (at least 1.5x the request). Going over a CPU limit only *throttles* it, and throttling Argo CD just makes syncs slow, so many teams leave CPU limits off for control-plane-style components.
