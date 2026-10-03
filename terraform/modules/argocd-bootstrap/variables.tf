variable "argocd_namespace" {
  type    = string
  default = "argocd"
}

variable "argocd_chart_version" {
  description = "Pin this — floating on 'latest' silently breaks reproducibility"
  type        = string
  default     = "10.9.2"
}

variable "server_service_type" {
  description = "How argocd-server is exposed: NodePort on k3s (Traefik already owns 80/443), LoadBalancer on EKS"
  type        = string
  default     = "NodePort"

  validation {
    condition     = contains(["NodePort", "LoadBalancer", "ClusterIP"], var.server_service_type)
    error_message = "server_service_type must be NodePort, LoadBalancer or ClusterIP."
  }
}
