terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
  }
}

provider "aws" {
  region  = var.aws_region
  profile = var.aws_profile # null uses the default credential chain

  default_tags {
    tags = {
      project = var.project
      managed = "terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
