# Aether

**An AI-assisted, multi-cloud Kubernetes provisioning platform.**

Aether provisions Kubernetes clusters (locally and on AWS EKS), deploys workloads via GitOps, and uses an AI layer to analyze real cluster metrics and recommend infrastructure changes — rightsizing over-provisioned workloads, suggesting scaling adjustments — rather than just summarizing scan output.

This is a follow-up to [GuardRail](#), a secure CI/CD pipeline for AWS infrastructure (Terraform + tfsec + AI-generated security summaries). Aether takes the same "AI reads real signal and explains it in plain English" idea further: from *explaining findings* to *recommending changes*.

> **Status:** Phase 1 complete (local GitOps loop). Phase 2 complete (EKS cluster and Argo CD deployed in AWS).

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
                  │  Metrics pipeline  │
                  │ (metrics-server /  │
                  │   Prometheus)      │
                  └─────────┬─────────┘
                            │ feeds
                            ▼
                  ┌───────────────────┐
                  │   AI analyzer      │
                  │ (rightsizing /     │
                  │  scaling advice)   │
                  └───────────────────┘
```

Infrastructure is provisioned with **Terraform** end to end — including the GitOps engine itself (ArgoCD is installed via Terraform's `helm` provider, not a manual `helm install`), so the entire platform is reproducible from code rather than a remembered sequence of manual steps.

## Tech stack

| Layer | Tool |
|---|---|
| Infrastructure as code | Terraform |
| Local Kubernetes | k3s |
| Cloud Kubernetes | AWS EKS |
| Package management (K8s) | Helm |
| GitOps / continuous deployment | ArgoCD |
| Metrics | metrics-server, Prometheus |
| AI analysis | Google Gemini API |
| Predictive scaling (planned) | Facebook Prophet |
| CI | GitHub Actions (plan-only, never auto-apply) |

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
│   ├── root-app.yaml         # app-of-apps entry point
│   ├── apps/                 # ArgoCD Application manifests
│   └── charts/                # actual workload manifests
├── ai-analyzer/
│   ├── collector/            # pulls metrics from Prometheus/metrics-server
│   ├── analyzer/              # builds AI prompt, calls Gemini, parses response
│   └── forecaster/            # Prophet-based load forecasting (planned)
├── .github/workflows/
└── README.md
```

## Design decisions worth knowing about

- **Hand-rolled Terraform modules, not registry modules** (e.g. no `terraform-aws-modules/vpc/aws`). This is a deliberate choice for a portfolio project — the goal is to be able to explain every subnet tag and route table, not import someone else's answer.
- **One NAT gateway, not one per AZ.** A cost-conscious call for a non-production project; multi-AZ NAT is called out in this README as the production-grade upgrade, rather than paid for here.
- **Local cluster before cloud cluster.** The GitOps and AI logic is proven on a free local k3s cluster first, then pointed at EKS — this also makes the "multi-cloud" claim demonstrable rather than aspirational: the same ArgoCD bootstrap module runs against both.
- **Plan-only CI**, never auto-apply — consistent with GuardRail's security posture. A human reviews every `terraform plan` before anything is applied.

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

## Roadmap

- [x] Phase 1 — Local GitOps loop (k3s + Terraform + ArgoCD)
- [x] Phase 2 — EKS provisioning via Terraform
- [x] Phase 3 — Same GitOps setup on EKS (multi-cloud proof)
- [ ] Phase 4 — Metrics pipeline (metrics-server / Prometheus)
- [ ] Phase 5 — AI analysis layer (Gemini-based rightsizing/scaling recommendations)
- [ ] Phase 6 — Prophet-based predictive scaling, tying in undergraduate dissertation research

## Related project

[GuardRail](#) — a secure CI/CD pipeline for AWS infrastructure: Terraform, GitHub Actions (plan-only), tfsec security scanning with PR comments, least-privilege CI-specific IAM, VPC Flow Logs, and an AI layer that turns tfsec's JSON output into a plain-English security summary posted on each PR.