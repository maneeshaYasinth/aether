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
│   │   └── metrics-server.yaml
│   └── charts/
│       └── hello-nginx/      # raw Deployment + Service YAML for the test app
├── ai-analyzer/               # not yet built — Phase 5
├── .github/workflows/         # not yet built — CI, will mirror GuardRail's plan-only pattern
├── README.md
├── learning.md                # beginner-level log of concepts learned, written for the person's own reference
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

**Phase 4 — Metrics pipeline: IN PROGRESS** (Phase 3 was effectively folded into Phase 2, since the GitOps-on-EKS proof happened there)
- Decision made: start with `metrics-server` only (lightweight), defer Prometheus until Phase 6 (predictive scaling) actually needs historical time-series data
- `gitops/apps/metrics-server.yaml` created — an ArgoCD Application pointing directly at the public `metrics-server` Helm chart repo (first time using that ArgoCD capability rather than a chart in this repo)
- Uses `--kubelet-insecure-tls` because k3s's kubelet has a self-signed cert metrics-server doesn't trust by default — without this flag it installs but silently reports no real metrics
- Being deployed to the **local k3s cluster** (not EKS, since EKS is currently torn down)
- Status as of last session: manifest written, instructed to commit/push and verify via `kubectl get applications -n argocd` and `kubectl top nodes` / `kubectl top pods` — **verification not yet confirmed back to the assistant**

**Phase 5 — AI analysis layer: NOT STARTED**
- Planned: Google Gemini API (same as GuardRail), reading real metrics-server/Prometheus data and producing structured (JSON) rightsizing/scaling recommendations, posted as PR comments

**Phase 6 — Prophet-based predictive scaling: NOT STARTED**
- Ties into the person's undergraduate dissertation research (Prophet-based predictive auto-scaling for AWS Lambda) — a lightweight version of that logic would inform Aether's scaling recommendations
- This is the actual reason Prometheus (not just metrics-server) will eventually be needed — Prophet needs historical data points, a live snapshot isn't enough

---

## Real problems hit and solved (good interview/README material)

1. **AWS Free Tier instance-type restriction** — `t3.medium` node group got stuck "Creating" with zero EC2 instances ever launching. Traced via `aws autoscaling describe-scaling-activities` to: account-level Free Tier restriction blocks anything above `t3.micro`/`t2.micro`. Fixed by changing instance type.
2. **Per-node pod IP limit on `t3.micro`** — even after nodes launched, ArgoCD pods stayed `Pending` with "Too many pods" scheduling errors. AWS VPC CNI gives every pod a real VPC IP; `t3.micro`'s tiny ENI capacity caps it at ~4 pods/node including mandatory system pods. Fixed by scaling node count 2→4 (more small nodes, since bigger ones were blocked) AND trimming ArgoCD's Helm install (`dex.enabled=false`, `notifications.enabled=false`, `applicationSet.enabled=false` — though applicationset-controller still ran anyway, a minor chart-version quirk, not chased further since it wasn't causing resource pressure).
3. **`kubectl port-forward` broken specifically on this EKS cluster** — "connection reset by peer" even pod-direct, with verbose logging (`-v=6`) showing it negotiates a WebSocket tunnel successfully but resets ~10s in. This is a known EKS websocket-tunneling compatibility quirk, not a config error. Fixed by switching ArgoCD's Service from `NodePort` to `LoadBalancer`, giving a real public AWS NLB hostname that bypasses `kubectl`'s tunneling entirely. Also had to use `http://` not `https://` when reaching it, since `configs.params.server.insecure=true` means ArgoCD speaks plain HTTP only, and the NLB is a pure L4 passthrough (no TLS termination) — using `https://` caused `PR_END_OF_FILE_ERROR` in the browser.
4. **A Terraform state scare that turned out to be a non-issue** — a `terraform plan` once showed all 14 networking resources as "will create" instead of "no changes," which looked like state corruption (there was a near-empty `terraform.tfstate` next to a large `terraform.tfstate.backup`). Turned out the person had legitimately run `terraform destroy` before adding the EKS module — the empty state was correct, not corrupted. Worth remembering: always ask "did you destroy recently?" before assuming state corruption.

---

## Conventions / how this person likes to work

- Wants things explained "why," not just handed code to copy-paste — beginner in DevOps/Kubernetes specifically (strong CS fundamentals, but this is new terrain).
- Prefers being walked through one command at a time with expected output described in advance, especially during install/setup sequences.
- Cost-consciousness matters — flag real AWS costs (NAT gateway, EKS control plane, LoadBalancer) proactively, and the person does tear things down between sessions to avoid billing.
- Wants a `learning.md` log kept updated with concepts learned, phrased for a beginner, and a `README.md` kept updated with the project's real architecture/status — both exist already in the repo and should be extended, not replaced, as phases complete.
- Is currently also balancing this project with: a full-time DevOps Engineer role, final-year university coursework, and AWS SAA-C03 exam prep — mentioned here only because it explains why progress happens in bursts across sessions, not as something to raise unprompted.
