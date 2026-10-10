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

**Two more lessons from re-running the analyzer:**
- **Models route around soft rules.** Once value-less rows were filtered, Gemini started giving k3s components real numbers while its own text still said "managed by k3s, leave it". Our code already knows what k3s owns (`CONFIG_SOURCES`), so those rows are now dropped by a hard check in `mark_noops()`. When the code knows the answer, let the code decide; the prompt is guidance, not enforcement.
- **Single snapshots flip-flop.** Three runs a few minutes apart told us to cut the application controller's CPU to 10m, then 12m, then *raise* it to 100m; repo-server memory went "raise to 174Mi" then "cut to 64Mi". Each run reacts to whatever that one second looked like. Chasing it would mean changing values forever, so the Argo CD values stay as they are. Recommendations need history (p95/max over days), which is exactly what Phase 6's Prometheus is for.

---

## Phase 6: usage history with Prometheus

### Why
metrics-server only knows *right now*. Back-to-back analyzer runs gave contradictory advice because each saw a different second. Prometheus scrapes the same numbers every minute and **stores** them, so we can ask "what was the peak over the last 7 days?" instead of "what is it this second?".

### What was deployed (`gitops/apps/prometheus.yaml`)
- The `prometheus-community/prometheus` Helm chart, pinned (29.35.0), installed by Argo CD like metrics-server. Adding the file to `gitops/apps/` was enough: the root app picks up any new Application in that folder.
- **Only the Prometheus server.** The chart can also install Alertmanager (sends alerts), Pushgateway (for batch jobs to push metrics), node-exporter (machine-level metrics) and kube-state-metrics (object state, e.g. requested resources). We don't need any of them yet, so they're off.
- **Data source: cAdvisor.** It's built into the kubelet on every node and reports per-container CPU/memory. The chart's default scrape job `kubernetes-nodes-cadvisor` already collects it.
- **Retention:** 15 days or 4GB, whichever comes first, on a 5Gi PersistentVolumeClaim.

### Kubernetes concepts
- **PersistentVolumeClaim (PVC):** a pod asking for disk that outlives the pod. On k3s the `local-path` StorageClass satisfies it with a folder on the laptop's disk.
- **`WaitForFirstConsumer`:** the PVC stays `Pending` until a pod that uses it is scheduled, so the volume gets created on the node where the pod lands. `Pending` for the first few seconds is normal.
- **Argo CD polls Git about every 3 minutes.** Right after a push the new app may not exist yet; press Refresh (or annotate the app) to skip the wait.
- **`kubectl get -w` watches only one resource type:** `get pods -w` works, `get pods,pvc -w` errors.

### Prometheus concepts
- **Counter vs gauge.** `container_cpu_usage_seconds_total` only goes up (total CPU-seconds used), so you need `rate(...[5m])` to turn it into "cores in use". That needs several samples, so it returns nothing for the first few minutes. `container_memory_working_set_bytes` is a gauge (current value) and works immediately.
- **Working set** is the memory number the kubelet uses for OOM/eviction decisions, and what `kubectl top` shows, so it's the one to size memory requests against.
- **Filter `container!=""`:** cAdvisor also reports pod-level and node-level totals with an empty `container` label; skip them to avoid double counting.

### Useful commands
```bash
# Is it scraping? (every job should be "up")
kubectl get --raw /api/v1/namespaces/monitoring/services/prometheus-server:80/proxy/api/v1/targets | head -c 500

# The UI: then open http://localhost:9090
kubectl -n monitoring port-forward svc/prometheus-server 9090:80
```
Queries to try in the UI:
```promql
sum by (namespace) (container_memory_working_set_bytes{container!=""}) / 2^20
sum by (namespace) (rate(container_cpu_usage_seconds_total{container!=""}[5m])) * 1000
```

### Step 2: sizing from history instead of a snapshot

**What changed:** the collector asks Prometheus for each container's **p95** and **max** CPU/memory over the last 7 days (new file `ai-analyzer/collector/history.py`), and Gemini sizes requests from p95 and limits from max.

**p95 vs max, and why both:**
- **p95** = the value usage stays under 95% of the time. It's "normal busy", ignoring rare spikes, so it's what a **request** should be based on (requests are what the scheduler reserves).
- **max** = the single worst moment. That's what the **memory limit** must survive, because going over a memory limit kills the container (OOMKilled). Going over a CPU limit only slows it down (throttling).
- That's also why "no CPU limit" is now treated as a deliberate choice, not a problem: without a CPU limit a container can borrow idle CPU, and nothing gets killed.

**PromQL concepts:**
- **Subquery `(...)[7d:5m]`:** "evaluate the inner expression every 5 minutes over the last 7 days". It turns a rate (which is only a value *now*) into a series of values you can then summarise.
- **`quantile_over_time(0.95, ...)` / `max_over_time(...)`:** take a series over time and reduce it to one number per container.
- **`count_over_time(...)`:** how many of those 5-minute points had data. Points × 5 minutes = hours of real history. Gaps (laptop asleep, k3s stopped) don't count.
- **`sum by (namespace, pod, container)`:** collapse duplicate series for the same container (cAdvisor sometimes keeps an old one around after a restart).

**Pods come and go, workloads stay.** Every rollout creates new pods with new random names (`hello-nginx-847949887d-x2x7p`). If history were keyed by pod, it would reset every time a rightsizing PR merged. Old pods can't be looked up anymore, but their names start with the workload name, so the collector groups history by that prefix (longest match wins, so `foo-bar-…` isn't credited to `foo`).

**Fallback, never failure:** under 24h of history (can't include a daily peak), or Prometheus unreachable → that container uses the metrics-server snapshot like before, and the prompt tells Gemini to keep confidence low. A run never fails because of history.

**Cluster DNS:** inside the cluster, the analyzer reaches Prometheus at `http://prometheus-server.monitoring.svc` (`<service>.<namespace>.svc`). It's a plain HTTP call to a Service, not a Kubernetes API call, so no RBAC change was needed.

**Report fix:** the table used to show only CPU/memory *requests*, so a change that was really to a limit looked like `10m → 10m`. It now has one "Changes" column listing only the fields that actually change, limits included.

**Problem hit:** k3s was installed with its service **disabled**, so after the laptop rebooted it didn't start, and Prometheus collected nothing for days. "Let history accumulate" only works if the cluster is running. Fix: `sudo systemctl enable --now k3s` (start now *and* on every boot).

**Problem hit: k3s crash-looped on start.** `systemctl status k3s` showed `activating (auto-restart)`: start, die after ~10s, repeat. Hundreds of "connection refused" log lines hid the real one: `failed to find interface with specified node ip`. `/etc/rancher/k3s/config.yaml` pinned `node-ip: 10.165.32.246`, an address from an earlier Wi-Fi network; the laptop was now `192.168.8.140`. k3s uses the node IP to pick the network card for pod networking (flannel), so with no such card it quits. Fix: delete the `node-ip` line so k3s uses whatever interface has the default route. Lesson: on a laptop, never pin an IP that DHCP hands out; and when a log is flooded, grep for `fatal|Shutdown` rather than reading the errors at the top.

**Problem hit: CronJob runs failed right after boot.** A CronJob that missed its schedule while the cluster was off runs **once, as soon as the cluster starts**. That catch-up run hit the metrics API before metrics-server was ready → `503 Service Unavailable` → job failed (and its one retry failed the same way seconds later). Fix: the collector now retries a 503 with backoff (5, 10, 20, 40, 60s). Other errors such as 403 still fail at once, since waiting won't fix a permission problem.

**Problem hit: a run crashed on a Gemini timeout.** The retry code caught *HTTP errors* (a 503 is a reply: "I'm busy"). A timeout is different: no reply ever comes, and Python raises `TimeoutError`, which nothing caught, so the job died. Fix: treat timeouts and network errors as "this model isn't answering" and move to the next model. Not retrying the same model is deliberate: each hang costs the full wait (now 90s), and the job has a 10-minute deadline (`activeDeadlineSeconds: 600`), so 4 models × 90s still fits.

**Surprise: history hours stopped growing at ~24h.** The 7-day window *slides*: every new hour that comes in, the hour from exactly 7 days ago drops out. Our oldest data (about 4h from Oct 3–4) was falling off the back as fast as new data arrived, so the total sat at 23.9h. Checked by splitting the window: `[1d:5m] offset 6d` (the oldest day) had 4.0h, `[6d:5m]` had 19.9h. Once that old data has fully aged out, the total grows again. Also: a sleeping laptop freezes k3s, so history and CronJob runs only happen while it's awake.

**Result: the first history-based report (2026-10-10, 24.4h of history).** Confidence went from `low` to `high`, and the advice stopped flip-flopping:
- **repo-server memory:** snapshot runs said "cut 128Mi → 64Mi", then "raise to 200Mi" (it had caught a spike). History: p95 78Mi against a 128Mi request, so no change. Settled.
- **argocd-server memory:** a real problem no snapshot noticed. p95 66.5Mi is already *above* the 64Mi request, and the max of 120.7Mi was 94% of the 128Mi limit, close to an OOMKill. Recommended 128Mi request / 256Mi limit.
- **prometheus-server CPU:** p95 6.3m but max 95m (spiky). The request goes to 10m anyway: the request is the *typical* reservation, and with no CPU limit the spikes simply borrow idle CPU.

**Job → CronJob.** A CronJob creates a new Job for every run (`aether-analyzer-29860230`), and the Job creates the pod. The collector followed pod → Job and stopped, so each run looked like a brand-new workload with no config file. It now does one more hop to the CronJob, like ReplicaSet → Deployment (and needed `get jobs` added to the ClusterRole). Caveat: the analyzer only runs ~1 minute every 6 hours, so it will take a very long time to reach 24h of history; a time threshold suits long-running pods better than batch jobs.

```bash
# Run the pipeline with history from the laptop (in ai-analyzer/)
kubectl -n monitoring port-forward svc/prometheus-server 9090:80     # terminal 1
.venv/bin/python collector/collect.py --prometheus http://localhost:9090 | less   # terminal 2: check usage_basis / history_hours
PROMETHEUS_URL=http://localhost:9090 .venv/bin/python run.py --dry-run
```
```promql
# hours of history per container (what the collector checks against 24)
count_over_time((sum by (namespace, pod, container) (container_memory_working_set_bytes{container!="",container!="POD"}))[7d:5m]) * 5 / 60
```

---

## Aether from A to Z (the whole system in one pass)

Everything above is organised by phase, in the order I built it. This section is organised by **how the system actually works when it runs**, so I can explain it end to end without jumping between phases.

### Layer 0: the machine
- **Local:** an Ubuntu laptop. k3s runs as a **systemd service** (`systemctl status k3s`, logs with `journalctl -u k3s`). If the service isn't enabled, or the laptop sleeps, the whole cluster stops: no pods, no Prometheus scrapes, no CronJob runs.
- **AWS:** EC2 instances (`t3.micro`) in a managed node group. AWS runs the control plane; I only pay for and see the worker nodes.

### Layer 1: the network (AWS only, `modules/networking`)
```
Internet
   │
Internet Gateway ── public subnets (1a, 1b) ── NAT gateway (one, in a public subnet)
                                                    │  outbound only
                                     private subnets (1a, 1b) ── EKS worker nodes
```
- **VPC** = my own private network inside AWS. DNS support/hostnames on, because EKS needs them.
- **Public subnet** = its route table sends `0.0.0.0/0` to the **Internet Gateway**. Load balancers live here.
- **Private subnet** = its route table sends `0.0.0.0/0` to the **NAT gateway**. Nodes can download images and call AWS APIs, but nothing on the internet can start a connection to them.
- **Two AZs** because EKS requires subnets in at least two Availability Zones.
- **Subnet tags** (`kubernetes.io/role/elb`, `kubernetes.io/role/internal-elb`) tell AWS which subnets to put public vs internal load balancers in.
- **One NAT gateway** saves money, but if that AZ goes down, the private subnets in the other AZ lose internet access too.

### Layer 2: the cluster
- **Local:** k3s's installer creates the cluster. Terraform does *not*.
- **AWS:** Terraform creates it (`modules/eks-cluster`): an IAM role for the control plane, an IAM role for the nodes (worker, ECR read-only, CNI policies), the `aws_eks_cluster`, and an `aws_eks_node_group`.
- **Pod networking differs:** k3s uses **flannel** (pods get IPs from a private overlay range). EKS uses the **AWS VPC CNI** (every pod gets a real VPC IP from the node's network interfaces), which is why a `t3.micro` could only fit ~4 pods.
- **How I talk to it:** `kubectl` reads `~/.kube/config`. For k3s that's a static file with a certificate. For EKS, `aws eks update-kubeconfig` writes an entry that asks AWS for a short-lived token every time, and Terraform does the same with `aws_eks_cluster_auth`. Whoever created the EKS cluster is made admin automatically.

### Layer 3: the GitOps engine
1. `terraform apply` in `environments/local` (or `aws`) runs the shared `argocd-bootstrap` module: a namespace + a Helm release of Argo CD.
2. I `kubectl apply` **one** file by hand, once: `gitops/root-app.yaml`.
3. From then on, Argo CD polls GitHub (~every 3 min). `root-app` watches `gitops/apps/`; every Application file in there points at something to deploy (a folder in this repo or a public Helm chart).
4. `automated: prune + selfHeal`: anything added to Git is created, anything removed is deleted, any manual change in the cluster is reverted.

**So the only ways to change the running system are:** a Git commit (normal path), Terraform (for Argo CD itself and AWS), or a hand-made Secret (the one exception).

### Layer 4: the workloads
| App (`gitops/apps/`) | What it is | Source |
|---|---|---|
| `hello-nginx` | Test app: Deployment + Service | `gitops/charts/hello-nginx` (raw YAML) |
| `metrics-server` | Live CPU/memory → Metrics API → `kubectl top` | public Helm chart |
| `prometheus` | Scrapes cAdvisor every minute, stores 15 days | public Helm chart |
| `ai-analyzer` | CronJob + RBAC for the analyzer | `gitops/charts/ai-analyzer` |

### Layer 5: the analyzer loop (every 6 hours)
1. **CronJob fires** → creates a Job → creates a pod running `ghcr.io/maneeshayasinth/aether-analyzer:sha-…`. k3s pulls the image (public, so no credentials).
2. Pod starts as ServiceAccount `aether-analyzer`. Env vars come from the hand-made Secret (Gemini key, GitHub token).
3. **Collect:** lists all pods (Kubernetes API, allowed by the read-only ClusterRole), reads live usage (Metrics API), asks Prometheus for 7-day p95/max (plain HTTP to `prometheus-server.monitoring.svc`, found via cluster DNS). Converts units, works out pod → ReplicaSet → Deployment.
4. **Analyze:** builds a prompt with fixed sizing rules, calls Gemini's REST API with a JSON schema. Retries with backoff; falls back to other models on 404/429/503/timeout.
5. **Check in code:** drop no-op rows and rows for k3s-managed things; validate quantities; limit ≥ request.
6. **Propose:** if a change touches a file in `EDITABLE_MANIFESTS`, and no rightsizing PR is open, it creates a branch, commits the edited YAML, opens a PR via the GitHub API.
7. **Human merges** → Argo CD sees the new commit → applies the Deployment → Kubernetes does a rolling update (new ReplicaSet, new pod with the new resources).

### Layer 6: shipping new analyzer code
1. Push a change under `ai-analyzer/` → GitHub Actions builds the Docker image → pushes `sha-<commit>` and `latest` to GHCR.
2. Wait for green, then commit the new tag in `gitops/charts/ai-analyzer/cronjob.yaml` → Argo CD updates the CronJob → the next run uses it.

### The design rules that hold it together
- Git is the source of truth; the cluster is made to match it.
- The AI only **proposes**; a human approves; Argo CD applies.
- AI output is untrusted input: anything code can check, code checks.
- Least privilege everywhere: read-only ClusterRole, non-root container, single-repo token.
- Cheap by default: NodePort over LoadBalancer, one NAT, tear AWS down when not in use.

---

## Where I'm weak: honest gaps in this project

This list comes from what the project **doesn't** do yet, and from the parts I got working with help but would struggle to explain cold. For each: why it matters, what I'd say today, and how to close the gap.

### 1. Terraform state and team workflow (high priority)
- **Gap:** state is a local `terraform.tfstate` file on my laptop (git-ignored). No remote backend, no locking. The "all 14 resources will be created" scare happened because I didn't fully understand what state is.
- **Why it matters:** in any team, state lives in a shared backend (an S3 bucket with locking) so two people can't apply at once and losing a laptop doesn't lose the infrastructure's record.
- **What to learn:** what state stores and why Terraform needs it; S3 backend + locking; `terraform import`, `state mv`, `state rm`; drift (`plan -refresh-only`); why state can contain secrets.
- **Close it:** move `environments/aws` to an S3 backend, and build the plan-only CI that's been on the list since the start (GitHub Actions running `fmt`, `validate`, `plan` on PRs, with AWS access through **OIDC**, not stored keys).

### 2. AWS IAM beyond "attach a managed policy" (high priority)
- **Gap:** I used AWS-managed policies on two roles. I haven't written a custom policy, used IAM for pods (**IRSA** or **EKS Pod Identity**), or managed who can access the cluster (EKS **access entries**). Cluster access worked only because I created it.
- **Why it matters:** IAM is the core of AWS security. Expect questions like "how does a pod get AWS permissions without access keys?" and "difference between a role's trust policy and its permissions policy?"
- **What to learn:** trust policy vs permissions policy; `sts:AssumeRole`; least privilege with conditions; IRSA (OIDC provider → role → ServiceAccount annotation); access entries.
- **Close it:** when EKS is rebuilt, give one pod a role via IRSA (e.g. read one S3 bucket) and prove `aws sts get-caller-identity` from inside it shows the role.

### 3. Networking, below the diagram (high priority)
- **Gap:** I built the VPC, but security groups were all created by EKS, not by me. The `port-forward` failure on EKS was worked around with a LoadBalancer, not root-caused. No Ingress, no TLS, no DNS (Route 53).
- **Why it matters:** "a pod can't reach X" is the most common real-world ticket.
- **What to learn:** CIDR maths (how many IPs in a /24? why does AWS reserve 5?); security groups (stateful) vs NACLs (stateless); how a request reaches a pod (LB → node → Service → kube-proxy/iptables → pod); Service types; Ingress + an ALB controller; how cluster DNS resolves `svc.namespace.svc`.
- **Close it:** add an Ingress for hello-nginx on k3s (Traefik is already there); on EKS, the AWS Load Balancer Controller with an ALB. Draw the packet path from browser to pod from memory.

### 4. Secrets management
- **Gap:** the analyzer's Secret is created by hand and is only base64. It's the one thing not in GitOps, and the GitHub token expires every 90 days.
- **Close it:** External Secrets Operator reading from AWS Secrets Manager (pairs with IRSA in #2), or Sealed Secrets on k3s. Be ready to explain why base64 isn't encryption and what etcd encryption at rest is.

### 5. Scaling and reliability (Kubernetes)
- **Gap:** Aether does **rightsizing** (how big is each pod), not **autoscaling** (how many pods / nodes). No HPA, no Cluster Autoscaler/Karpenter, no liveness/readiness probes, no PodDisruptionBudgets, single replicas everywhere.
- **Why it matters:** "how would you handle a traffic spike?" is a standard question, and probes are expected on any production workload.
- **What to learn:** HPA (uses the same Metrics API I already run); readiness vs liveness vs startup probes; QoS classes (Guaranteed / Burstable / BestEffort, which follow from requests/limits); rolling update `maxSurge`/`maxUnavailable`.
- **Close it:** add probes and an HPA to hello-nginx, load it with a simple tool, and watch replicas go up and down.

### 6. Observability beyond "Prometheus stores numbers"
- **Gap:** no dashboards (Grafana), no alerts (Alertmanager is turned off), no central logs, nothing tells me when a CronJob run fails; I find out by checking.
- **What to learn:** the three pillars (metrics, logs, traces); writing an alert rule; CloudWatch Container Insights on EKS; the "four golden signals" (latency, traffic, errors, saturation).
- **Close it:** turn on Alertmanager with one rule: "the analyzer Job failed" or "no successful run in 13 hours".

### 7. Testing and code quality
- **Gap:** the analyzer has **no automated tests**. CI only builds the image. Correctness was checked by running it against the real cluster.
- **Why it matters:** the validation logic (quantity parsing, no-op detection, limit ≥ request) is exactly the kind of thing that should have unit tests, and a reviewer will look for them.
- **Close it:** `pytest` tests for `parse_quantity`, `mark_noops`, and the proposer's validation, run in the existing workflow before the image is built. Add image scanning (e.g. Trivy) to the same workflow.

### 8. AWS breadth
- **Gap:** this project touches VPC, EC2, EKS, IAM and ELB only. Not used: S3, RDS, CloudWatch, Route 53, ECR, Secrets Manager, Lambda, Auto Scaling groups directly, Cost Explorer/Budgets.
- **Close it:** the gap fixes above already pull in S3 (state), Secrets Manager (#4), CloudWatch (#6) and ECR (could mirror the image there). Set an AWS **Budget** alert before the next EKS rebuild.

### 9. Linux troubleshooting under pressure
- **Gap:** the k3s crash-loop took a while because the real error was buried. I know `systemctl`/`journalctl` now, but not deeply.
- **What to learn:** `journalctl -u <svc> -b --since`, filtering with `grep -E 'fatal|error'`; `ip addr`, `ip route`, `ss -tlnp`, `dig`, `curl -v`; `df -h` / `du` (Prometheus disk); file permissions and users (why UID 10001).

### 10. Explaining code I didn't type myself
- **Gap:** parts of the analyzer were written with an AI assistant. That's normal now, but in an interview "walk me through this function" has to get a confident answer.
- **Close it:** for each of `collect.py`, `history.py`, `analyze.py`, `propose.py`, be able to say from memory: what goes in, what comes out, what can fail and what happens then. Re-read one file a week and explain it out loud.

---

## Self-check: can I answer these without notes?

If I can't answer one in two or three sentences, that's a gap to study. Short answers are underneath each so I can check myself.

**Kubernetes**
1. What happens, step by step, after `kubectl apply` of a Deployment?
   *API server validates and stores it in etcd → Deployment controller creates a ReplicaSet → ReplicaSet controller creates pods → scheduler picks a node with enough unrequested CPU/memory → kubelet on that node pulls the image and starts the container.*
2. Request vs limit, and what happens when each is exceeded?
   *Request = reserved, used for scheduling; exceeding it is allowed. Limit = cap; CPU over limit is throttled, memory over limit is OOMKilled.*
3. Why does changing resources create a new pod?
   *Pod specs are mostly immutable; a template change makes a new ReplicaSet and a rolling update.*
4. Role vs ClusterRole?
   *Role is one namespace; ClusterRole is cluster-wide or for cluster-scoped resources. The analyzer reads every namespace, so ClusterRole.*
5. How does a pod find `prometheus-server.monitoring.svc`?
   *Cluster DNS (CoreDNS) resolves the Service name to its ClusterIP; kube-proxy routes that IP to a ready pod.*

**GitOps / CI/CD**
6. What does self-heal do, and what does prune do?
   *Self-heal reverts manual changes in the cluster to match Git; prune deletes resources that were removed from Git.*
7. Why pin `sha-` tags instead of `latest`?
   *You always know which code runs, a rollback is a Git revert, and `latest` can change without any commit.*
8. Why can't GitHub Actions deploy to my k3s cluster directly?
   *The laptop isn't reachable from the internet; pull-based GitOps works because Argo CD reaches out to GitHub, not the other way round.*

**Terraform / AWS**
9. What is Terraform state, and what goes wrong if two people apply at once without locking?
   *The record mapping code to real resource IDs; without locking, both write state and one overwrites the other, so Terraform loses track of resources.*
10. Why are worker nodes in private subnets, and how do they reach the internet?
    *No public IPs, so nothing can connect in; outbound goes through the NAT gateway in a public subnet.*
11. What's the risk of one NAT gateway?
    *It's in one AZ; if that AZ fails, private subnets in every AZ lose outbound access.*
12. Why did `t3.micro` nodes run out of room at ~4 pods?
    *The VPC CNI gives every pod a real IP from the node's ENIs, and a t3.micro supports few ENIs/IPs.*
13. Which AWS costs keep running if I forget to destroy?
    *EKS control plane (hourly), NAT gateway (hourly + data), load balancers, EC2 nodes, EBS volumes, Elastic IPs.*

**The AI part**
14. Why does Python do the maths instead of the model?
    *LLMs are unreliable at arithmetic; code is deterministic and testable.*
15. Why size requests from p95 and memory limits from max?
    *p95 is normal busy load to reserve for; max is the worst moment, and going over a memory limit kills the container.*
16. What stops the AI from breaking the cluster?
    *Read-only RBAC, validation in code, an allow-list of editable files, one PR at a time, and a human merge before Argo CD applies anything.*

---

## Suggested order to close the gaps

1. Unit tests + Trivy scan in the existing workflow (free, local, quick win).
2. Probes + HPA on hello-nginx, Ingress via Traefik (free, on k3s).
3. Alertmanager rule for failed analyzer runs (free).
4. Terraform plan-only CI with GitHub OIDC → AWS, S3 remote state (cents per month).
5. Rebuild EKS **for one session**: IRSA, External Secrets + Secrets Manager, AWS Load Balancer Controller. Set a Budget alert first, then `terraform destroy` at the end.
6. Then continue Phase 6 (Prophet forecasting).
