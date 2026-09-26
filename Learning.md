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

## Phase 2: EKS (cloud) — not yet started

Terraform will provision the actual Kubernetes cluster itself here (via AWS EKS), rather than just deploying software onto a pre-existing one. To be filled in as this phase progresses.