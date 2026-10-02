# Aether

**An AI-assisted, multi-cloud Kubernetes provisioning platform.**

Aether provisions Kubernetes clusters (locally and on AWS EKS), deploys workloads via GitOps, and uses an AI layer to analyze real cluster metrics and recommend infrastructure changes — rightsizing over-provisioned workloads, suggesting scaling adjustments — rather than just summarizing scan output.

This is a follow-up to [GuardRail](#), a secure CI/CD pipeline for AWS infrastructure (Terraform + tfsec + AI-generated security summaries). Aether takes the same "AI reads real signal and explains it in plain English" idea further: from *explaining findings* to *recommending changes*.

> **Status:** Phases 1–5 complete. The AI analyzer runs inside the cluster every 6 hours, reads live metrics, and opens rightsizing pull requests that a human reviews before Argo CD applies them. The EKS environment is torn down between sessions to avoid AWS charges and can be rebuilt with `terraform apply`. Next: Phase 6 (Prometheus history + Prophet forecasting).

---

## Architecture

```
                     ┌─────────────────┐
                     │   Git Repo       │◄──── source of truth
                     │  (this repo)     │
                     └────────┬────────┘
                              │ watched by
                              ▼
                     ┌─────────────────┐
                     │     ArgoCD       │
                     │  (GitOps engine) │
                     └────────┬────────┘
                              │ deploys to
                              ▼
        ┌──────────────────────────────────────┐
        │      Kubernetes cluster               │
        │  (k3s locally  /  EKS on AWS)          │
        └──────────────────┬────────────────────┘
                            │ metrics
                            ▼
                  ┌───────────────────┐
                  │  metrics-server    │
                  │ (live CPU/memory)  │
                  └─────────┬─────────┘
                            │ read-only
                            ▼
                  ┌───────────────────┐
                  │   AI analyzer      │  CronJob, every 6h
                  │ collect → Gemini → │
                  │ validate → propose │
                  └─────────┬─────────┘
                            │ opens
                            ▼
                  ┌───────────────────┐
                  │  GitHub pull       │──► human reviews & merges
                  │  request           │      ──► back to the Git repo
                  └───────────────────┘
```

The loop is closed through Git, not through the cluster: the analyzer can only *read* Kubernetes. Every change it proposes is a pull request a human must approve, and Argo CD applies it only after the merge.

Infrastructure is provisioned with **Terraform** end to end — including the GitOps engine itself (ArgoCD is installed via Terraform's `helm` provider, not a manual `helm install`), so the entire platform is reproducible from code rather than a remembered sequence of manual steps.

## Tech stack

| Layer | Tool |
|---|---|
| Infrastructure as code | Terraform |
| Local Kubernetes | k3s |
| Cloud Kubernetes | AWS EKS |
| Package management (K8s) | Helm |
| GitOps / continuous deployment | ArgoCD |
| Metrics | metrics-server (Prometheus planned for Phase 6) |
| AI analysis | Google Gemini API (REST, schema-enforced JSON output) |
| Analyzer runtime | Python 3.14, Kubernetes Python client, ruamel.yaml |
| Container image | Docker, GitHub Container Registry (GHCR) |
| Predictive scaling (planned) | Facebook Prophet |
| CI | GitHub Actions: analyzer image build; Terraform plan-only CI planned (never auto-apply) |

## Repo structure

```
aether/
├── terraform/
│   ├── environments/
│   │   ├── local/          # k3s-facing config (helm/kubernetes providers)
│   │   └── aws/             # EKS-facing config
│   └── modules/
│       ├── networking/      # VPC, subnets, NAT — EKS-tagged
│       ├── eks-cluster/     # EKS control plane + managed node group
│       └── argocd-bootstrap/# ArgoCD install, shared across environments
├── gitops/
│   ├── root-app.yaml         # app-of-apps entry point (k3s)
│   ├── root-app-aws.yaml     # app-of-apps entry point (EKS)
│   ├── apps/                 # ArgoCD Applications: hello-nginx, metrics-server, ai-analyzer
│   └── charts/
│       ├── hello-nginx/      # test workload (Deployment + Service)
│       └── ai-analyzer/      # CronJob + read-only RBAC for the analyzer
├── ai-analyzer/
│   ├── collector/            # metrics-server usage + pod requests/limits → JSON
│   ├── analyzer/             # prompt rules, Gemini call, Markdown report
│   ├── proposer/             # validates AI output, edits YAML, opens a GitHub PR
│   ├── run.py                # full pipeline (what the CronJob runs)
│   ├── Dockerfile
│   └── forecaster/           # Prophet-based load forecasting (planned)
├── .github/workflows/
│   └── analyzer-image.yml    # builds the analyzer image and pushes to GHCR
├── Learning.md               # detailed concept notes per phase
├── Aether-Learning-Guide.md  # how the platform fits together + exercises
├── AGENT_CONTEXT.md          # handoff context for AI coding sessions
└── README.md
```

## Design decisions worth knowing about

- **Hand-rolled Terraform modules, not registry modules** (e.g. no `terraform-aws-modules/vpc/aws`). This is a deliberate choice for a portfolio project — the goal is to be able to explain every subnet tag and route table, not import someone else's answer.
- **One NAT gateway, not one per AZ.** A cost-conscious call for a non-production project; multi-AZ NAT is called out in this README as the production-grade upgrade, rather than paid for here.
- **Local cluster before cloud cluster.** The GitOps and AI logic is proven on a free local k3s cluster first, then pointed at EKS — this also makes the "multi-cloud" claim demonstrable rather than aspirational: the same ArgoCD bootstrap module runs against both.
- **Plan-only CI**, never auto-apply — consistent with GuardRail's security posture. A human reviews every `terraform plan` before anything is applied.
- **AI proposes, humans approve.** The analyzer has a read-only ServiceAccount; its only route to changing the cluster is a pull request. The same "human reviews before apply" rule as Terraform.
- **AI output is treated as untrusted input.** Recommendations are validated in code (valid Kubernetes quantities, limits ≥ requests, an allow-list of editable files, no no-op PRs, one open PR at a time) before anything is written.
- **Code does the maths, the model does the reasoning.** Unit conversion and percentages are computed in Python; Gemini gets clean numbers, explicit rules, and a JSON schema it must answer in.
- **Pinned image tags.** The CronJob runs `sha-<commit>`, never `latest`, so the running code is always traceable to a commit and every upgrade goes through Git.
- **metrics-server before Prometheus.** Rightsizing only needs a current snapshot; Prometheus is added when Phase 6's forecasting actually needs history.

## Phase 1: Local GitOps loop — done

Getting a Kubernetes cluster running locally, with ArgoCD managing deployments via an app-of-apps pattern, entirely reproducible from code.

**Setup:**

```bash
# 1. Install k3s (creates a local Kubernetes cluster)
curl -sfL https://get.k3s.io | sh -

# 2. Point kubectl at it
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config

# 3. Install ArgoCD via Terraform
cd terraform/environments/local
terraform init
terraform plan
terraform apply

# 4. Bootstrap GitOps (one-time manual step — everything after this is git-driven)
kubectl apply -f gitops/root-app.yaml
```

**Verify:**
```bash
kubectl get applications -n argocd
# root-app and hello-nginx should both show Healthy / Synced
```

**Proven behaviors:**
- ArgoCD installed and managed entirely through Terraform (`helm_release` resource) — no manual `helm install`.
- App-of-apps pattern: `root-app` watches `gitops/apps/`, automatically picking up and deploying any Application manifest added there — adding a new app to the platform means committing one YAML file, nothing more.
- **Self-healing**, demonstrated by deliberately drifting the cluster (`kubectl scale --replicas=5` against a git-declared `replicas: 1`) and watching ArgoCD detect and revert the drift automatically, with no manual intervention.

## Phase 2: EKS (cloud) — done

The same platform is now running on AWS EKS, proving the Terraform and GitOps
setup is not k3s-specific. The AWS environment provisions a VPC, public and
private subnets across two availability zones, a cost-conscious single NAT
gateway, an EKS control plane, a managed node group, and Argo CD through the
shared Terraform bootstrap module.

**Implemented:**
- EKS Kubernetes version pinned to `1.31`.
- Worker nodes run in private subnets using `t3.micro` instances.
- IAM roles and managed policies are defined for the EKS control plane and nodes.
- Argo CD is exposed through a NodePort and configured for local HTTP access.
- Argo CD was accessed through a local port-forward at `http://localhost:8080`.

**Access the Argo CD UI:**
```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80
# Open http://localhost:8080
```

The first port-forward attempt reset its connection after the Kubernetes API
upgrade succeeded. The pod remained healthy with zero restarts, and reconnecting
established the tunnel successfully.

After Phase 2 was demonstrated, all AWS resources were removed with `terraform destroy` to stop hourly charges (EKS control plane, NAT gateway, load balancer, EC2 nodes). `terraform apply` in `terraform/environments/aws` rebuilds it in about 15–20 minutes.

## Phase 4: Metrics pipeline — done

metrics-server is deployed by Argo CD straight from its public Helm repository ([gitops/apps/metrics-server.yaml](gitops/apps/metrics-server.yaml)), exposing live CPU and memory usage through the Kubernetes Metrics API.

```bash
kubectl top nodes
kubectl top pods -A
```

- `--kubelet-insecure-tls` is required on k3s, whose kubelet uses a self-signed certificate; without it metrics-server installs but reports nothing.
- k3s ships its own metrics-server, which conflicted with the Helm-managed one (`field is immutable` on the Deployment selector). Resolved with Argo CD's `Replace=true` sync option and removing the k3s-owned copy.

## Phase 5: AI rightsizing analyzer — done

A Python pipeline in [ai-analyzer/](ai-analyzer/) that turns live cluster metrics into reviewed pull requests.

| Stage | File | What it does |
|---|---|---|
| Collect | [collector/collect.py](ai-analyzer/collector/collect.py) | Joins metrics-server usage with each container's requests/limits; normalises units (millicores, MiB); follows pod → ReplicaSet → Deployment |
| Analyze | [analyzer/analyze.py](ai-analyzer/analyzer/analyze.py) | Sends the data to Gemini with explicit rightsizing rules and a `responseSchema`; retries with exponential backoff and falls back to other models; renders a Markdown report |
| Propose | [proposer/propose.py](ai-analyzer/proposer/propose.py) | Validates every recommendation, edits the YAML with comments preserved, and opens a GitHub PR via the REST API |
| Run | [run.py](ai-analyzer/run.py) | The whole pipeline; always logs the full report |

**How it runs:** GitHub Actions builds the image and publishes it to `ghcr.io/maneeshayasinth/aether-analyzer`. Argo CD deploys a CronJob ([gitops/charts/ai-analyzer/](gitops/charts/ai-analyzer/)) that runs it every 6 hours in the `aether-analyzer` namespace, under a ServiceAccount that can only read pods, ReplicaSets and pod metrics. The container runs as non-root with a read-only filesystem and all Linux capabilities dropped.

**Proven end to end:** hello-nginx was deliberately over-provisioned (250m CPU / 128Mi requested, ~0m / ~10Mi used). The analyzer opened [PR #1](https://github.com/maneeshaYasinth/aether/pull/1) recommending 10m / 32Mi; after it was merged, Argo CD deployed the merge commit and the pod was replaced with the new values. Later runs found nothing auto-editable to change and correctly opened no PR, even when a smaller fallback model returned a no-op recommendation.

**Run it locally** (from `ai-analyzer/`, with `GEMINI_API_KEY` exported):

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py --dry-run        # show the report, diff and PR body; change nothing
```

**Run it in the cluster** (one-time Secret, never committed):

```bash
kubectl create namespace aether-analyzer
kubectl -n aether-analyzer create secret generic aether-analyzer-secrets \
  --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY" \
  --from-literal=GITHUB_TOKEN="$GITHUB_TOKEN"   # fine-grained PAT: Contents + Pull requests on this repo only
kubectl -n aether-analyzer create job --from=cronjob/aether-analyzer manual-run
kubectl -n aether-analyzer logs -f job/manual-run
```

**Known limitations:** a single metrics-server snapshot can't see traffic peaks, so all recommendations are low confidence; only `gitops/charts/hello-nginx` is auto-editable (Helm- and Terraform-managed values are report-only); the Secret is created by hand rather than managed through GitOps.

## Roadmap

- [x] Phase 1 — Local GitOps loop (k3s + Terraform + ArgoCD)
- [x] Phase 2 — EKS provisioning via Terraform
- [x] Phase 3 — Same GitOps setup on EKS (multi-cloud proof)
- [x] Phase 4 — Metrics pipeline (metrics-server)
- [x] Phase 5 — AI analysis layer (Gemini rightsizing → validated pull requests, running in-cluster)
- [ ] Phase 6 — Prometheus history + Prophet-based predictive scaling, tying in undergraduate dissertation research

**Future improvements**

- Manage the analyzer Secret through GitOps (Sealed Secrets or External Secrets Operator).
- Automate image tag bumps with Argo CD Image Updater.
- Make the Argo CD Service type a Terraform variable (`NodePort` on k3s, `LoadBalancer` on EKS).
- Terraform plan-only CI, mirroring GuardRail.
- Extend auto-editing to Helm values (metrics-server, Argo CD) so more findings become PRs.

## Related project

[GuardRail](#) — a secure CI/CD pipeline for AWS infrastructure: Terraform, GitHub Actions (plan-only), tfsec security scanning with PR comments, least-privilege CI-specific IAM, VPC Flow Logs, and an AI layer that turns tfsec's JSON output into a plain-English security summary posted on each PR.