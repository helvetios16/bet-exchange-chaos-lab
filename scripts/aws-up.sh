#!/usr/bin/env bash
# Crea la infraestructura (EKS + ECR) con Terraform y configura kubectl.
# Requiere credenciales del Learner Lab activas (aws sts get-caller-identity).
set -euo pipefail
cd "$(dirname "$0")/../infra/terraform"

aws sts get-caller-identity >/dev/null || { echo "Credenciales AWS inválidas o caducadas"; exit 1; }

terraform init -input=false
terraform apply -input=false -auto-approve

eval "$(terraform output -raw kubeconfig_command)"
echo
echo "Infraestructura lista."
echo "  ECR_REGISTRY = $(terraform output -raw ecr_registry)"
echo "Siguiente paso: lanzar el workflow 'CD' en GitHub (publica imágenes y despliega)."
