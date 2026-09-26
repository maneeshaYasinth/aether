variable "cluster_name" {
  type    = string
  default = "aether"
}

variable "cluster_version" {
  description = "Kubernetes version — pin explicitly, don't float on EKS's default"
  type        = string
  default     = "1.31"
}

variable "vpc_id" {
  type = string
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "node_instance_type" {
  description = "Free Tier restriction on this account limits us to t3.micro/t2.micro"
  type        = string
  default     = "t3.micro"
}

variable "node_desired_size" {
  type    = number
  default = 4
}

variable "node_min_size" {
  type    = number
  default = 2
}

variable "node_max_size" {
  type    = number
  default = 5
}