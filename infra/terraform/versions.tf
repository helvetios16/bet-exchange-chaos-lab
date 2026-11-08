terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
  }
}

provider "aws" {
  region = var.region

  # Todo recurso creado lleva estas etiquetas: sirven para localizar huérfanos
  # al limpiar (ver scripts/aws-down.sh) y para controlar el costo.
  default_tags {
    tags = {
      Project = var.project
      Managed = "terraform"
    }
  }
}
