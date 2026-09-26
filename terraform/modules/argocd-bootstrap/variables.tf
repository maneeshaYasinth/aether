variable "argocd_namespace" {
  type    = string
  default = "argocd"
}

variable "argocd_chart_version" {
  description = "Pin this — floating on 'latest' silently breaks reproducibility"
  type        = string
  default     = "10.9.2"
}