# Aether — Agent Context

This file exists so a Claude Code (or any agent) session starting fresh in this repo can get full context without the person having to re-explain everything. Read this first before making changes.

---

## What this project is

Aether is an AI-assisted, multi-cloud Kubernetes provisioning platform. It provisions Kubernetes clusters (locally via k3s, and on AWS via EKS), deploys workloads via GitOps (ArgoCD), and — in later phases — uses an AI layer to analyze real cluster metrics and recommend infrastructure changes (rightsizing, scaling adjustments), not just summarize scan output.

It's a follow-up/portfolio project to an earlier project called **GuardRail** (a secure CI/CD pipeline for AWS infra: Terraform + GitHub Actions plan-only CI + tfsec scanning + an AI layer that turns tfsec's JSON output into plain-English PR comments). Aether's differentiator is going from "AI explains findings" to "AI recommends changes."

**Why it's being built:** following advice from Lithira Aponsu (Senior Cloud Engineer at Sysco LABS, the person's target company) on what makes a cloud engineering portfolio stand out — understand AWS services deeply and combine them in real projects, get real hands-on Linux/Kubernetes experience, include proper Git/CI-CD, and incorporate security + AI meaningfully.

**Naming history:** originally called "Nimbus," renamed to "Aether" after realizing "Project Nimbus" is the name of Google/Amazon's controversial Israel cloud contract — wanted to avoid that association in a portfolio piece.

---

## Repo structure

```
aether/
├── terraform/
│   ├── environments/
│   │   ├── local/          # k3s-facing config (helm/kubernetes providers via static kubeconfig)
│   │   └── aws/             # EKS-facing config (helm/kubernetes providers via dynamic EKS auth)
│   └── modules/
│       ├── networking/      # VPC, subnets, NAT — EKS-tagged
│       ├── eks-cluster/     # EKS control plane + managed node group + IAM roles
│       └── argocd-bootstrap/# ArgoCD install via Helm, shared across both environments unchanged
├── gitops/
│   ├── root-app.yaml         # app-of-apps entry point (local/k3s)
│   ├── root-app-aws.yaml     # app-of-apps entry point (EKS) — identical spec, different cluster
│   ├── apps/                 # ArgoCD Application manifests (one per app ArgoCD should manage)
│   │   ├── hello-nginx.yaml
│   │   ├── metrics-server.yaml
│   │   ├── prometheus.yaml
│   │   └── ai-analyzer.yaml
│   └── charts/
│       ├── hello-nginx/      # raw Deployment + Service YAML for the test app (now has resource requests/limits)
│       └── ai-analyzer/      # cronjob.yaml + rbac.yaml (ServiceAccount, read-only ClusterRole, binding)
├── ai-analyzer/               # Phase 5 — Python pipeline (see Phase 5 below)
│   ├── collector/collect.py   # + history.py (Prometheus p95/max, Phase 6)
│   ├── analyzer/analyze.py
│   ├── proposer/propose.py
│   ├── run.py
│   ├── requirements.txt       # kubernetes, ruamel.yaml
│   └── Dockerfile
├── .github/workflows/
│   └── analyzer-image.yml     # builds + pushes analyzer image to GHCR (Terraform plan-only CI still not built)
├── Readme.md
├── Learning.md                # beginner-level log of concepts learned, written for the person's own reference
├── Aether-Learning-Guide.md   # how it all fits together + practice exercises + interview explanation
└── AGENT_CONTEXT.md           # this file
```

GitHub repo: `github.com/maneeshaYasinth/aether`, cloned locally at `~/Desktop/dev/aether` on an Ubuntu laptop.

---

## Architecture / key design decisions

- **Local cluster before cloud cluster.** The GitOps and (eventually) AI logic is proven on a free local k3s cluster first, then pointed at EKS. This also makes "multi-cloud" demonstrable rather than aspirational — the same `argocd-bootstrap` Terraform module and the same `hello-nginx` Application manifest run unmodified against both k3s and EKS; only the surrounding provider auth config differs per environment.
- **Hand-rolled Terraform modules, not registry modules** (no `terraform-aws-modules/vpc/aws` etc.) — deliberate, so every subnet tag and route table can be explained in an interview rather than imported from someone else's module.
- **ArgoCD managed via Terraform's `helm` provider**, not a manual `helm install` — keeps the GitOps engine itself in the same infra-as-code story as everything else.
- **App-of-apps pattern**: one root `Application` (`root-app` / `root-app-aws`) watches the `gitops/apps/` folder in git. ArgoCD auto-deploys whatever Application manifests appear there. Adding a new app to the platform = committing one YAML file, nothing manual.
- **One NAT gateway, not one per AZ** — cost-conscious choice for a portfolio project; called out as a "would harden for production" item.
- **Plan-only CI planned** (not yet built) — mirroring GuardRail's security posture, no auto-apply.

---

## Phase status

**Phase 1 — Local GitOps loop: COMPLETE**
- k3s installed locally, ArgoCD installed onto it via Terraform (`terraform/environments/local`)
- App-of-apps pattern proven: `root-app` → `gitops/apps/hello-nginx.yaml` → real nginx pod running
- Self-heal demonstrated: manually drifted replica count, watched ArgoCD auto-revert to git's declared state

**Phase 2 — EKS (cloud) provisioning: COMPLETE, then torn down to stop billing**
- Real EKS cluster ("aether") built in AWS us-east-1 via Terraform: VPC/subnets/NAT (`modules/networking`), EKS control plane + managed node group + IAM roles (`modules/eks-cluster`)
- ArgoCD installed on EKS reusing the exact same `argocd-bootstrap` module, just with EKS-specific provider auth (dynamic token via `aws_eks_cluster_auth`, not a static kubeconfig file like k3s)
- `hello-nginx` + `root-app-aws` deployed and proven Synced/Healthy on EKS — the actual "multi-cloud" proof
- **All AWS resources were destroyed via `terraform destroy`** after Phase 2 was demonstrated, to stop hourly billing (NAT gateway, EKS control plane, LoadBalancer, EC2 nodes). Confirmed no orphaned load balancers/NAT gateways/clusters remained afterward. To resume Phase 2 work, `terraform apply` from `terraform/environments/aws` rebuilds everything from scratch (~15-20 min).

**Phase 4 — Metrics pipeline: COMPLETE** (Phase 3 was effectively folded into Phase 2, since the GitOps-on-EKS proof happened there)
- Decision made: start with `metrics-server` only (lightweight), defer Prometheus until Phase 6 (predictive scaling) actually needs historical time-series data
- `gitops/apps/metrics-server.yaml` created — an ArgoCD Application pointing directly at the public `metrics-server` Helm chart repo (first time using that ArgoCD capability rather than a chart in this repo)
- Uses `--kubelet-insecure-tls` because k3s's kubelet has a self-signed cert metrics-server doesn't trust by default — without this flag it installs but silently reports no real metrics
- Being deployed to the **local k3s cluster** (not EKS, since EKS is currently torn down)
- Verified 2026-10-02: `metrics-server` Synced/Healthy, `kubectl top nodes` / `kubectl top pods -A` return real numbers.

**Phase 5 — AI analysis layer: COMPLETE (2026-10-03)** — built in a Claude Code session
- **Collector** (`ai-analyzer/collector/collect.py`): joins metrics.k8s.io usage with each container's requests/limits, normalises to millicores/MiB, computes % of request/limit, resolves pod → ReplicaSet → Deployment (`workload: {kind, name}`). Uses kubeconfig locally, in-cluster ServiceAccount otherwise.
- **Analyzer** (`ai-analyzer/analyzer/analyze.py`): Gemini via raw REST (`urllib`, same style as GuardRail — no SDK). Explicit rules in the prompt (<20% of request = over-provisioned, ~2× usage, floors 10m CPU / 32Mi memory, memory limit ≥ 1.5× request), `responseSchema` for structured JSON, `temperature: 0.2`. `CONFIG_SOURCES` maps workloads to the file that declares them. Default model `gemini-3.8-flash` (`GEMINI_MODEL` env overrides); `gemini-2.5-flash` is retired for new keys. Exponential backoff (2/4/8/16s) on 429/503, then falls back to other Flash models from `GET /models`. `--dry-run` prints the prompt.
- **Proposer** (`ai-analyzer/proposer/propose.py`): validates AI output (quantity regex, limit ≥ request), only edits files in `EDITABLE_MANIFESTS` (currently just `gitops/charts/hello-nginx/deployment.yaml`), uses ruamel.yaml to preserve comments, skips no-op edits, opens at most one open PR at a time (branch prefix `aether/rightsizing-`). GitHub REST API with `GITHUB_TOKEN`.
- **run.py**: collect → Gemini → always print the full report → propose. `--dry-run`, `-i recs.json` (skip Gemini), `--exclude NS`.
- **Proven loop**: hello-nginx deliberately set to 250m/128Mi (using ~0m/~10Mi) → PR #1 recommended 10m/32Mi (limits 100m/64Mi) → merged → Argo CD deployed merge commit `a41e55b` → pod replaced.
- **Image**: `ai-analyzer/Dockerfile` (python:3.14-slim, non-root uid 10001) built by `.github/workflows/analyzer-image.yml` on changes to `ai-analyzer/**`; pushed to `ghcr.io/maneeshayasinth/aether-analyzer` with tags `sha-<short>` and `latest`. Package is public (inherited from the public repo), so no imagePullSecret.
- **In-cluster**: `gitops/apps/ai-analyzer.yaml` → namespace `aether-analyzer`. CronJob `0 */6 * * *`, `concurrencyPolicy: Forbid`, `activeDeadlineSeconds: 600`, pinned image tag (currently `sha-82c549e`), hardened securityContext, read-only root FS with `/tmp` emptyDir. ClusterRole allows only get/list pods, get replicasets, get/list pods.metrics.k8s.io.
- **Secret** `aether-analyzer-secrets` (`GEMINI_API_KEY`, `GITHUB_TOKEN`) was created by hand with kubectl and is NOT in Git. GitHub token is a fine-grained PAT named `aether-rightsizing-bot`: this repo only, Contents + Pull requests read/write, 90-day expiry — renew it and recreate the Secret when it expires.
- **Rolling out analyzer code is two commits**: push code → wait for green CI → bump the `sha-` tag in `gitops/charts/ai-analyzer/cronjob.yaml`. Bumping before the image exists → `ImagePullBackOff`.
- Last manual run (`manual-test-3`) completed: 15 containers, full report logged, correctly no PR (hello-nginx already rightsized; everything else is report-only).

**Phase 6 — Prometheus history + Prophet-based predictive scaling: IN PROGRESS** (see Open items)
- Ties into the person's undergraduate dissertation research (Prophet-based predictive auto-scaling for AWS Lambda) — a lightweight version of that logic would inform Aether's scaling recommendations
- This is the actual reason Prometheus (not just metrics-server) will eventually be needed — Prophet needs historical data points, a live snapshot isn't enough

---

## Real problems hit and solved (good interview/README material)

1. **AWS Free Tier instance-type restriction** — `t3.medium` node group got stuck "Creating" with zero EC2 instances ever launching. Traced via `aws autoscaling describe-scaling-activities` to: account-level Free Tier restriction blocks anything above `t3.micro`/`t2.micro`. Fixed by changing instance type.
2. **Per-node pod IP limit on `t3.micro`** — even after nodes launched, ArgoCD pods stayed `Pending` with "Too many pods" scheduling errors. AWS VPC CNI gives every pod a real VPC IP; `t3.micro`'s tiny ENI capacity caps it at ~4 pods/node including mandatory system pods. Fixed by scaling node count 2→4 (more small nodes, since bigger ones were blocked) AND trimming ArgoCD's Helm install (`dex.enabled=false`, `notifications.enabled=false`, `applicationSet.enabled=false` — though applicationset-controller still ran anyway, a minor chart-version quirk, not chased further since it wasn't causing resource pressure).
3. **`kubectl port-forward` broken specifically on this EKS cluster** — "connection reset by peer" even pod-direct, with verbose logging (`-v=6`) showing it negotiates a WebSocket tunnel successfully but resets ~10s in. This is a known EKS websocket-tunneling compatibility quirk, not a config error. Fixed by switching ArgoCD's Service from `NodePort` to `LoadBalancer`, giving a real public AWS NLB hostname that bypasses `kubectl`'s tunneling entirely. Also had to use `http://` not `https://` when reaching it, since `configs.params.server.insecure=true` means ArgoCD speaks plain HTTP only, and the NLB is a pure L4 passthrough (no TLS termination) — using `https://` caused `PR_END_OF_FILE_ERROR` in the browser.
4. **A Terraform state scare that turned out to be a non-issue** — a `terraform plan` once showed all 14 networking resources as "will create" instead of "no changes," which looked like state corruption (there was a near-empty `terraform.tfstate` next to a large `terraform.tfstate.backup`). Turned out the person had legitimately run `terraform destroy` before adding the EKS module — the empty state was correct, not corrupted. Worth remembering: always ask "did you destroy recently?" before assuming state corruption.

5. **Gemini model retired** — `gemini-2.5-flash` returned 404 "no longer available to new users"; switched default to `gemini-3.8-flash`. GuardRail still references 2.5-flash (it has its own ListModels fallback, but worth updating).
6. **Gemini overload/quota** — repeated 503 on 3.8-flash and 429 on fallback models; backoff + fallback eventually got an answer from `gemini-flash-lite-latest`, whose reasoning is vaguer (it once recommended a no-op "10m → 10m" and contradicted itself on coredns). Validation meant no bad PR was opened.
7. **Workflow file never committed** — `git add .github/...` was run from inside `ai-analyzer/`, so the path didn't match; the push contained only the Dockerfile and Actions never ran. Run git from the repo root.
8. **First in-cluster image pull took 56s**, making `kubectl logs -f` time out (`context deadline exceeded`) while the pod was still `ContainerCreating`. Not a failure; the next image version pulled in 5.6s (shared layers).
9. **`kubectl create job --from=cronjob/...` copies the CronJob at that moment** — a job created before Argo CD synced the new tag ran the old image.
10. **python3 -m venv failed** — Ubuntu needed `sudo apt install python3.14-venv`.
11. **k3s crash-loop after changing networks** — `node-ip` pinned in `/etc/rancher/k3s/config.yaml` to an old DHCP address → `failed to find interface with specified node ip`. Removed the pin (2026-10-06). The k3s service was also disabled, so it didn't start on boot; now enabled.
12. **CronJob catch-up run races metrics-server at boot** → 503 from metrics.k8s.io; collector retries 503 now.

---

## Open items / next steps

- Old test Jobs `manual-test-1/2/3` in `aether-analyzer` can be deleted: `kubectl -n aether-analyzer delete job manual-test-1 manual-test-2 manual-test-3`.
- Future: Sealed Secrets / External Secrets for the analyzer Secret; Argo CD Image Updater for tag bumps; Terraform plan-only CI; auto-editing Helm values.
- **Phase 6 (in progress)**: step 1 done 2026-10-03: Prometheus runs via Argo CD (`gitops/apps/prometheus.yaml`, server only, cAdvisor scrape, 15d/4GB retention on a 5Gi local-path PVC, namespace `monitoring`). Step 2 code written 2026-10-06, **not yet verified live**: `ai-analyzer/collector/history.py` queries Prometheus (`PROMETHEUS_URL`, CronJob sets `http://prometheus-server.monitoring.svc`) for p95/max CPU+memory over 7d plus hours of coverage, grouped by workload via pod-name prefix; <24h history or Prometheus down → metrics-server snapshot (`usage_basis` per container). Prompt sizes requests ≈1.2× p95, memory limit ≥1.5× max, and says no CPU limit is intentional; report table now has a single "Changes" column of only the fields that change (limits included). PromQL not yet run against real Prometheus (no Docker access to test). **Found 2026-10-06: k3s service was disabled, so it didn't start after a reboot ~2026-10-04 and history has gaps** — `sudo systemctl enable --now k3s`. Verified 2026-10-06 against real Prometheus: queries work, every container reported 4.6h of history (so all still `snapshot`). k3s had also been crash-looping because `/etc/rancher/k3s/config.yaml` pinned an old Wi-Fi `node-ip`; removed so k3s follows the current interface. Catch-up CronJob runs at cluster start failed with metrics API 503 (metrics-server not ready); collector now retries 503 with backoff. `run.py --dry-run` now always prints the report. **To finish step 2:** push → wait for CI → bump the image `sha-` tag in `gitops/charts/ai-analyzer/cronjob.yaml`; keep k3s up ≥24h so containers switch to `usage_basis: history`. Then Prophet forecasting in `ai-analyzer/forecaster/`. Motivation: back-to-back single-snapshot runs gave contradictory Argo CD advice (CPU 50m→10m, then 50m→100m), so Argo CD requests stay as set in `argocd-bootstrap` until history exists.

---

## Conventions / how this person likes to work

- Wants things explained "why," not just handed code to copy-paste — beginner in DevOps/Kubernetes specifically (strong CS fundamentals, but this is new terrain).
- Prefers being walked through one command at a time with expected output described in advance, especially during install/setup sequences.
- Cost-consciousness matters — flag real AWS costs (NAT gateway, EKS control plane, LoadBalancer) proactively, and the person does tear things down between sessions to avoid billing.
- Wants a `learning.md` log kept updated with concepts learned, phrased for a beginner, and a `README.md` kept updated with the project's real architecture/status — both exist already in the repo and should be extended, not replaced, as phases complete.
- Is currently also balancing this project with: a full-time DevOps Engineer role, final-year university coursework, and AWS SAA-C03 exam prep — mentioned here only because it explains why progress happens in bursts across sessions, not as something to raise unprompted.
