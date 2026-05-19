variable "name" {
  type = string
}

variable "region" {
  type = string
}

variable "image" {
  type = string
}

variable "service_account_email" {
  type = string
}

variable "labels" {
  type    = map(string)
  default = {}
}
