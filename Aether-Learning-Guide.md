# Aether Learning Guide

This guide explains what you built in Aether, why each part exists, how the parts connect, and how to practice the important ideas again.

## 1. What Aether is

Aether is a Kubernetes platform with four main goals:

1. Provision infrastructure with Terraform.
2. Run Kubernetes locally with k3s and in AWS with EKS.
3. Deploy applications through GitOps with Argo CD.
4. Collect cluster metrics so an AI analyzer can later recommend rightsizing and scaling changes.

The central idea is:

```text
Git repository
     |
     v
Argo CD watches Git and reconciles the cluster
     |
     v
Kubernetes runs the declared workloads
     |
     v
Metrics are collected for analysis
```

Git is the source of truth. Manual Kubernetes commands are useful for investigation and experiments, but permanent changes should be represented in Git.

## 2. The tools and their responsibilities

### Linux

Your Ubuntu machine is the host. It runs the local Kubernetes cluster, Terraform, Helm, kubectl, and the containers used by k3s.

### k3s

k3s is a lightweight Kubernetes distribution. In the local environment, k3s creates and runs the Kubernetes cluster on your laptop.

Important distinction:

- k3s creates the local cluster.
- Terraform manages software deployed onto that cluster.

### Kubernetes

Kubernetes continuously tries to make the real cluster match the desired state declared in resources such as Deployments and Services.

Important resources in this project:

- `Deployment`: keeps the requested number of application pods running.
- `Pod`: the actual running unit containing one or more containers.
- `Service`: gives pods a stable network identity.
- `Namespace`: groups resources.
- `Application`: an Argo CD custom resource describing what Argo CD should deploy.
- `APIService`: registers an extension API such as `metrics.k8s.io`.

### kubectl

`kubectl` is the command-line client for the Kubernetes API server.

Examples:

```bash
kubectl config current-context
kubectl get nodes
kubectl get pods -A
kubectl describe pod POD_NAME -n NAMESPACE
kubectl logs deployment/metrics-server -n kube-system
```

The command always operates against the current kubeconfig context. Check it before working on local or AWS resources:

```bash
kubectl config get-contexts
kubectl config use-context default
```

### Terraform

Terraform declares and manages infrastructure as code. In this project it is used for two different jobs:

- Local environment: install Argo CD onto the existing k3s cluster.
- AWS environment: create the VPC, subnets, NAT gateway, EKS cluster, node group, and Argo CD.

The usual workflow is:

```bash
terraform init
terraform fmt -check
terraform validate
terraform plan
terraform apply
```

`plan` previews changes. `apply` makes them real. `destroy` removes managed infrastructure, so use it carefully.

### Helm

Helm packages Kubernetes applications. Argo CD itself is installed by Terraform's Helm provider. Metrics-server is installed by Argo CD from its Helm chart.

A chart version is not always the same as the application version. Always check which value a tool expects.

### Argo CD

Argo CD watches a Git repository and continuously reconciles Kubernetes with the manifests or charts declared there.

Useful states:

- `Synced`: live resources match Git.
- `OutOfSync`: live resources differ from Git.
- `Healthy`: the application is running correctly.
- `Progressing`: resources are still starting or becoming ready.
- `Degraded`: a resource is unhealthy.

Argo CD gives you GitOps: changes are reviewed, committed, pushed, and automatically deployed.

## 3. Repository structure

```text
aether/
├── terraform/
│   ├── environments/
│   │   ├── local/          # Terraform for the existing k3s cluster
│   │   └── aws/            # Terraform for AWS infrastructure and EKS
│   └── modules/
│       ├── networking/     # VPC, subnets, routes, NAT
│       ├── eks-cluster/    # EKS control plane and worker nodes
│       └── argocd-bootstrap/# Shared Argo CD installation
├── gitops/
│   ├── root-app.yaml       # Parent Argo CD Application
│   ├── apps/               # Child Argo CD Applications
│   └── charts/              # Workload manifests
├── ai-analyzer/            # Collector, Gemini analyzer, PR proposer (Phase 5)
├── .github/workflows/      # Builds the analyzer container image
├── Learning.md             # Chronological learning notes
├── Readme.md               # Project overview and roadmap
└── Aether-Learning-Guide.md
```

## 4. How the local platform was created

### Step 1: Create the local cluster

The k3s installer created Kubernetes locally. It also created the kubeconfig used by kubectl and Terraform.

```bash
curl -sfL https://get.k3s.io | sh -
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $(id -u):$(id -g) ~/.kube/config
kubectl get nodes
```

The kubeconfig is important because Kubernetes providers need an API server address and credentials.

### Step 2: Install Argo CD through Terraform

The shared module contains a `helm_release` for the `argo-cd` chart. Terraform creates the Argo CD namespace and installs the chart.

```bash
cd terraform/environments/local
terraform init
terraform plan
terraform apply
```

The local Terraform configuration does not create k3s. It connects to k3s and installs Argo CD into it.

### Step 3: Bootstrap the root Application

This is the one manual bootstrap step:

```bash
kubectl apply -f gitops/root-app.yaml
```

The root Application watches `gitops/apps/`. It discovers child Application manifests in that directory.

## 5. The app-of-apps pattern

The parent-child relationship is:

```text
root-app
  |
  +-- hello-nginx Application
  |     |
  |     +-- Deployment
  |     +-- Service
  |
  +-- metrics-server Application
  |     |
  |     +-- Deployment
  |     +-- Service
  |     +-- APIService
  |
  +-- ai-analyzer Application
        |
        +-- CronJob
        +-- ServiceAccount
        +-- ClusterRole + ClusterRoleBinding
```

The root Application is declared in [gitops/root-app.yaml](gitops/root-app.yaml). Child Applications are stored in `gitops/apps/`.

To add a new Argo-managed application:

1. Create an Application manifest in `gitops/apps/`.
2. Point it at a Helm chart or a directory of manifests.
3. Commit and push it.
4. Argo CD discovers and deploys it.

This avoids registering applications manually in the Argo CD UI.

## 6. The hello-nginx workload

The workload is declared in `gitops/charts/hello-nginx/deployment.yaml` and its Service manifest.

A Deployment says approximately:

```text
Keep one hello-nginx pod running.
Use this container image.
Expose this container port.
```

A Service says approximately:

```text
Find pods with these labels and give them a stable network endpoint.
```

Labels are the connection between a Service and a Deployment's pods. If the selector does not match the pod labels, the Service has no endpoints.

## 7. GitOps self-healing

Argo CD was tested by manually changing the live cluster:

```bash
kubectl scale deployment hello-nginx -n hello-nginx --replicas=5
```

Git still declared one replica. Because Argo CD had automated sync and self-healing enabled, it detected the drift and returned the Deployment to the Git-declared state.

This demonstrates an important distinction:

- `kubectl scale` changes the live cluster only.
- Editing the Deployment manifest changes desired state.
- Argo CD continuously compares desired state with live state.

## 8. Argo CD access

The local Argo CD Service currently uses a NodePort:

```bash
kubectl -n argocd get svc argocd-server
```

Open:

```text
http://localhost:30080
```

The alternative is a temporary port-forward:

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:80
```

Then open `http://localhost:8080`. Press `Ctrl+C` to stop the port-forward. Stopping the port-forward does not stop Argo CD.

Get the initial password without storing it in Git:

```bash
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d
```

The username is `admin`.

## 9. EKS and the AWS environment

The AWS environment uses Terraform to create more infrastructure than the local environment:

```text
VPC
  +-- public subnets
  +-- private subnets
  +-- Internet Gateway
  +-- NAT Gateway

EKS cluster
  +-- control plane
  +-- managed node group
  +-- IAM roles and policies

Argo CD
  +-- installed through the shared bootstrap module
```

Why private subnets matter:

- Worker nodes do not need public IP addresses.
- The NAT gateway provides outbound Internet access.
- Public-facing components can be placed in public subnets.

Why the single NAT gateway was chosen:

- It costs less for a portfolio project.
- It is not as resilient as one NAT gateway per availability zone.

The local and AWS environments use the same general GitOps idea, but the cluster provisioning responsibility differs:

```text
Local: k3s creates the cluster; Terraform installs Argo CD.
AWS:   Terraform creates EKS and installs Argo CD.
```

## 10. Metrics-server

Metrics-server collects current CPU and memory usage from kubelets and exposes it through the Kubernetes Metrics API.

It enables commands such as:

```bash
kubectl top nodes
kubectl top pods -A
kubectl top pods -n hello-nginx
```

Metrics-server is not a historical database. It answers current usage questions. Prometheus is needed later for historical queries and trends.

The Argo CD Application is declared in [gitops/apps/metrics-server.yaml](gitops/apps/metrics-server.yaml).

Important configuration:

```yaml
args:
  - --kubelet-insecure-tls
```

This is appropriate for the local development cluster where kubelet certificate verification was preventing scraping. It should be reviewed before using a production-grade cluster.

## 11. The metrics-server problem and what it taught you

The first metrics-server deployment appeared to run, but the API was unhealthy:

```text
v1beta1.metrics.k8s.io   False (MissingEndpoints)
```

The investigation followed the Kubernetes dependency chain:

```text
kubectl top
  -> metrics.k8s.io API
  -> APIService
  -> metrics-server Service
  -> Service endpoints
  -> metrics-server Pod
  -> kubelet /metrics/resource endpoint
```

The actual problem was that k3s already supplied a metrics-server Deployment and Service. Argo CD then attempted to manage a Helm chart with different immutable Deployment selectors.

Kubernetes does not allow a Deployment selector to change after creation. Argo reported:

```text
field is immutable
```

The resolution was:

1. Add `Replace=true` to the Argo sync options.
2. Push the manifest to Git.
3. Remove the old k3s-owned metrics-server Deployment.
4. Reset the failed child Application so the root Application recreated it.
5. Remove the stale k3s-owned Service so Argo recreated the Helm-managed Service.
6. Wait for the image pull and pod startup.
7. Verify the API and endpoints.

The final successful checks were:

```bash
kubectl -n kube-system get pods -l app.kubernetes.io/name=metrics-server
kubectl get apiservice v1beta1.metrics.k8s.io
kubectl top nodes
kubectl top pods -A
kubectl -n argocd get applications
```

The key lesson is to check whether the cluster distribution already installs a component before adding another installation through GitOps.

## 12. A reliable Kubernetes troubleshooting method

When a command fails, work from the user-facing symptom toward the dependency that controls it.

### Check status

```bash
kubectl get applications -n argocd
kubectl get pods -A
kubectl get deployments -A
kubectl get services -A
```

### Describe the resource

```bash
kubectl describe pod POD_NAME -n NAMESPACE
kubectl describe deployment DEPLOYMENT_NAME -n NAMESPACE
```

Events often explain image pulls, scheduling, permissions, probes, and volume problems.

### Check logs

```bash
kubectl logs POD_NAME -n NAMESPACE
kubectl logs deployment/DEPLOYMENT_NAME -n NAMESPACE
```

### Check selectors and endpoints

```bash
kubectl get service SERVICE_NAME -n NAMESPACE -o yaml
kubectl get endpoints SERVICE_NAME -n NAMESPACE -o wide
```

A Service with no endpoints usually means one of these:

- no matching pods exist;
- pod labels do not match the Service selector;
- pods are not Ready;
- the wrong Service is being managed;
- the application failed before becoming Ready.

### Check the owning system

For Argo CD:

```bash
kubectl -n argocd get application APP_NAME -o yaml
kubectl -n argocd describe application APP_NAME
```

For Terraform:

```bash
terraform plan
terraform state list
```

## 13. Git workflow used in this project

The normal change flow is:

```bash
git status
git diff
# edit a manifest or Terraform file
git diff
git add PATH
git commit -m "type: short description"
git push origin main
```

Examples of useful commit types:

- `feat`: add functionality.
- `fix`: correct broken behavior.
- `docs`: update documentation.
- `refactor`: change structure without changing behavior.

The metrics-server work produced a feature commit and then a fix commit after the ownership conflict was discovered. That is normal engineering work: the important part is that the final behavior is verified.

Before committing, check for unrelated changes:

```bash
git status --short
git diff --stat
git diff -- PATH/TO/FILE
```

## 14. What is complete and what is next

Completed:

- Local k3s cluster.
- Terraform-managed Argo CD installation.
- GitOps app-of-apps pattern.
- hello-nginx deployment.
- Argo CD self-healing test.
- AWS EKS infrastructure.
- Metrics-server and current resource metrics.
- The AI analyzer (collector, Gemini analyzer, PR proposer).
- The analyzer image built by GitHub Actions and published to GHCR.
- The analyzer running in-cluster as a read-only CronJob deployed by Argo CD.
- A full AI → pull request → human merge → Argo CD loop (PR #1).

The order changed from the original plan: the analyzer was built on metrics-server alone, because rightsizing only needs a current snapshot. Prometheus moves to Phase 6, where forecasting needs history.

Next milestone (Phase 6):

1. Install Prometheus through Argo CD.
2. Confirm Prometheus is scraping Kubernetes metrics.
3. Query historical CPU and memory data.
4. Feed history to the analyzer so recommendations can see peaks (higher confidence).
5. Add Prophet-based forecasting in `ai-analyzer/forecaster/`.

Metrics-server answers:

```text
What is being used right now?
```

Prometheus answers:

```text
What was used over time, and what trend is developing?
```

## 15. Practice exercises

### Exercise 1: Inspect the whole cluster

```bash
kubectl get nodes -o wide
kubectl get pods -A
kubectl get applications -n argocd
```

Explain which resources belong to Argo CD, kube-system, and hello-nginx.

### Exercise 2: Trace a Service to a Pod

```bash
kubectl get svc -n hello-nginx
kubectl get endpoints -n hello-nginx
kubectl get pods -n hello-nginx --show-labels
```

Explain why the Service selects its pods.

### Exercise 3: Observe GitOps drift

```bash
kubectl scale deployment hello-nginx -n hello-nginx --replicas=3
kubectl get deployment hello-nginx -n hello-nginx
kubectl get application hello-nginx -n argocd
```

Wait for Argo CD to self-heal, then explain why the replica count changed back.

### Exercise 4: Break and diagnose a workload

Temporarily change the image in the hello-nginx Deployment to an invalid image. Commit and push it, then inspect:

```bash
kubectl get pods -n hello-nginx
kubectl describe pod POD_NAME -n hello-nginx
kubectl get application hello-nginx -n argocd
```

Restore the valid image afterward.

### Exercise 5: Verify metrics

```bash
kubectl get apiservice v1beta1.metrics.k8s.io
kubectl top nodes
kubectl top pods -A
```

Explain the path from `kubectl top` to the metrics-server pod.

### Exercise 6: Prove the analyzer can't change the cluster

```bash
kubectl auth can-i list pods -A --as=system:serviceaccount:aether-analyzer:aether-analyzer
kubectl auth can-i delete pods -n hello-nginx --as=system:serviceaccount:aether-analyzer:aether-analyzer
kubectl auth can-i patch deployments -n hello-nginx --as=system:serviceaccount:aether-analyzer:aether-analyzer
```

Explain why the first is `yes` and the others are `no`, and which file in `gitops/charts/ai-analyzer/` decides that.

### Exercise 7: Make the AI open a PR again

Raise hello-nginx's requests in `gitops/charts/hello-nginx/deployment.yaml` (e.g. back to `250m` / `128Mi`), commit and push, wait for Argo CD to sync, then:

```bash
kubectl -n aether-analyzer create job --from=cronjob/aether-analyzer manual-test-pr
kubectl -n aether-analyzer logs -f job/manual-test-pr
```

Find the new PR on GitHub. Before merging, check the diff: which lines changed, and were your comments preserved?

### Exercise 8: Ship new analyzer code

Make a small change in `ai-analyzer/` (for example, edit a log message). Follow it all the way through: commit → green GitHub Actions run → new `sha-` tag in GHCR → bump the tag in `cronjob.yaml` → Argo CD sync → manual Job. Explain why the CronJob doesn't use `latest`.

### Exercise 9: Read a Gemini failure

Read the logs of a run where Gemini was busy. For each line, say whether it was a 503 or a 429, what that code means, and why the script waited or moved on.

## 16. Interview explanation

A concise explanation of the project is:

> I built Aether as a Terraform and GitOps-based Kubernetes platform. Locally, k3s creates the cluster and Terraform installs Argo CD through the Helm provider. A root Argo CD Application watches a directory of child Applications, so workloads are deployed by committing manifests to Git. I proved self-healing by manually changing a Deployment and watching Argo restore the declared state. I also provisioned an AWS EKS environment with a VPC, private worker subnets, IAM roles, and the same Argo CD bootstrap module. I installed metrics-server through Argo CD and debugged a conflict with k3s's built-in metrics-server by tracing the Metrics API, Service endpoints, Deployment selectors, and pod readiness. On top of that I built an AI rightsizing analyzer: a Python CronJob running in the cluster under a read-only ServiceAccount collects live usage and resource requests, sends them to Gemini with explicit rules and a JSON schema, validates the answer in code, and opens a GitHub pull request. A human reviews and merges it, and Argo CD rolls it out. I proved the loop with a deliberately over-provisioned workload whose requests the AI cut from 250m/128Mi to 10m/32Mi. The design principle is that the AI proposes and a human approves — and that model output is treated as untrusted input. The next step is Prometheus history and Prophet forecasting.
```

## 17. Commands to remember

```bash
# Cluster context
kubectl config current-context
kubectl config get-contexts

# Cluster overview
kubectl get nodes -o wide
kubectl get pods -A
kubectl get applications -n argocd

# Debugging
kubectl describe pod POD -n NAMESPACE
kubectl logs deployment/DEPLOYMENT -n NAMESPACE
kubectl get endpoints SERVICE -n NAMESPACE -o wide

# Argo CD access
kubectl -n argocd port-forward svc/argocd-server 8080:80

# Current resource metrics
kubectl top nodes
kubectl top pods -A

# AI analyzer (in-cluster)
kubectl -n aether-analyzer get cronjob,jobs,pods
kubectl -n aether-analyzer create job --from=cronjob/aether-analyzer manual-test-N
kubectl -n aether-analyzer logs -f job/manual-test-N

# AI analyzer (laptop, from ai-analyzer/)
.venv/bin/python run.py --dry-run

# Which commit is Argo CD running?
kubectl get application hello-nginx -n argocd -o jsonpath='{.status.sync.revision}'; echo

# Terraform safety loop
terraform fmt -check
terraform validate
terraform plan

# Git safety loop
git status
git diff
git add PATH
git commit -m "type: description"
git push origin main
```

## 18. Phase 5 in one page

Detailed explanations of every concept below are in `Learning.md` under *Phase 5*.

```text
CronJob (every 6h)
  -> pod runs as ServiceAccount aether-analyzer (read-only RBAC)
  -> collector: metrics.k8s.io usage + pod requests/limits, units normalised
  -> analyzer:  Gemini with rules + JSON schema, backoff + model fallback
  -> report printed to the pod logs
  -> proposer:  validate -> edit allowed YAML -> GitHub PR (or no PR if nothing changes)
  -> human merges -> Argo CD syncs -> rolling update
```

Where each piece lives:

| Piece | Location |
|---|---|
| Python code | `ai-analyzer/collector`, `analyzer`, `proposer`, `run.py` |
| Container image | `ai-analyzer/Dockerfile` → `ghcr.io/maneeshayasinth/aether-analyzer:sha-<commit>` |
| Image build | `.github/workflows/analyzer-image.yml` |
| Kubernetes objects | `gitops/charts/ai-analyzer/` (CronJob, ServiceAccount, ClusterRole, ClusterRoleBinding) |
| Argo CD Application | `gitops/apps/ai-analyzer.yaml` |
| Secret (not in Git) | `aether-analyzer-secrets` in namespace `aether-analyzer`: `GEMINI_API_KEY`, `GITHUB_TOKEN` |

Three things to be able to explain:

1. **Why a PR and not a direct change?** The analyzer can only read the cluster. A human reviews every change, Git keeps the history, and Argo CD stays the only thing that applies changes.
2. **Why validate the AI's answer?** Model output is untrusted input. A weaker fallback model once recommended a no-op and contradicted itself; validation meant nothing happened.
3. **Why low confidence?** One metrics-server snapshot can't see peaks. Prometheus history (Phase 6) fixes that.
