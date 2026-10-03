resource "kubernetes_namespace" "argocd" {
  metadata {
    name = var.argocd_namespace
  }
}

resource "helm_release" "argocd" {
  name       = "argocd"
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = var.argocd_chart_version
  namespace  = kubernetes_namespace.argocd.metadata[0].name
  timeout    = 600

  # The chart ships with resources: {} for everything; the Phase 5 analyzer
  # flagged it. Sized from `kubectl top` (controller ~120Mi, others <50Mi) with
  # headroom for sync spikes. Memory limits only: exceeding one OOM-kills the
  # pod, whereas a CPU limit would just throttle syncs.
  values = [yamlencode({
    controller = { resources = { requests = { cpu = "50m", memory = "256Mi" }, limits = { memory = "512Mi" } } }
    repoServer = { resources = { requests = { cpu = "25m", memory = "64Mi" }, limits = { memory = "256Mi" } } }
    server     = { resources = { requests = { cpu = "10m", memory = "64Mi" }, limits = { memory = "128Mi" } } }
    redis      = { resources = { requests = { cpu = "10m", memory = "32Mi" }, limits = { memory = "64Mi" } } }
  })]

  set {
    name  = "server.service.type"
    value = var.server_service_type
  }

  set {
    name  = "configs.params.server\\.insecure"
    value = "true"
  }

  set {
    name  = "dex.enabled"
    value = "false"
  }

  set {
    name  = "notifications.enabled"
    value = "false"
  }

  # The chart has no applicationSet.enabled key (Helm silently ignores unknown
  # values); scaling to zero is how this chart turns the controller off.
  set {
    name  = "applicationSet.replicas"
    value = "0"
  }
}