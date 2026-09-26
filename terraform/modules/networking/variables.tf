variable "cluster_name" {
  description = "Used to tag subnets for EKS auto-discovery"
  type        = string
  default     = "aether"
}

variable "vpc_cidr" {
  type    = string
  default = "10.10.0.0/16"
}

variable "azs" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}