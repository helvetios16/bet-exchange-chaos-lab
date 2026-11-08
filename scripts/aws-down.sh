#!/usr/bin/env bash
# Destruye TODO lo creado en AWS y comprueba que no queden recursos que
# sigan cobrando. Ejecutar siempre antes de cerrar el laboratorio.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

echo "==> 1/4 Retirando recursos de Kubernetes que crean objetos fuera de Terraform"
if aws eks update-kubeconfig --region "$REGION" --name chaos-lab-eks >/dev/null 2>&1; then
  helm uninstall monitoring -n monitoring 2>/dev/null || true
  helm uninstall traefik -n traefik 2>/dev/null || true   # elimina el balanceador
  kubectl delete namespace kuber-app kuber-data monitoring traefik --ignore-not-found --timeout=180s || true
  kubectl delete pvc --all -A --ignore-not-found --timeout=120s 2>/dev/null || true  # libera volúmenes EBS
  echo "    esperando a que AWS borre balanceadores y volúmenes..."
  sleep 60
else
  echo "    (no hay clúster accesible; se sigue con terraform destroy)"
fi

echo "==> 2/4 terraform destroy"
( cd "$ROOT/infra/terraform" && terraform destroy -input=false -auto-approve )

echo "==> 3/4 Comprobando restos (deben salir vacíos)"
echo "- Clústeres EKS:";          aws eks list-clusters --region "$REGION" --query clusters --output text
echo "- Balanceadores clásicos:"; aws elb describe-load-balancers --region "$REGION" --query 'LoadBalancerDescriptions[].LoadBalancerName' --output text
echo "- Balanceadores v2:";       aws elbv2 describe-load-balancers --region "$REGION" --query 'LoadBalancers[].LoadBalancerName' --output text
echo "- Volúmenes EBS libres:";   aws ec2 describe-volumes --region "$REGION" --filters Name=status,Values=available --query 'Volumes[].VolumeId' --output text
echo "- Repositorios ECR:";       aws ecr describe-repositories --region "$REGION" --query 'repositories[].repositoryName' --output text
echo "- NAT Gateways:";           aws ec2 describe-nat-gateways --region "$REGION" --filter Name=state,Values=available --query 'NatGateways[].NatGatewayId' --output text

echo "==> 4/4 Revisa 'Used \$X of \$50' en el Learner Lab."
