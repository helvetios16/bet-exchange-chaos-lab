# =============================================================================
# Infraestructura en AWS Academy Learner Lab:
#   - EKS (plano de control + grupo de nodos gestionado)
#   - 3 repositorios ECR (backend, frontend, loadgen)
#
# Decisiones de diseño (ver docs/INFORME.md):
#   * Se usa la VPC por defecto y sus subredes públicas: evita un NAT Gateway
#     (~0,045 USD/h) y reduce piezas que pueden fallar en el laboratorio.
#   * No se pueden crear roles IAM en el Learner Lab: se reutiliza LabRole,
#     que ya existe, tanto para el clúster como para los nodos.
# =============================================================================

data "aws_caller_identity" "current" {}

data "aws_iam_role" "lab" {
  name = "LabRole"
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "selected" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "availability-zone"
    values = var.azs
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

locals {
  cluster_name = "${var.project}-eks"
}

# Etiquetas que Kubernetes usa para descubrir subredes al crear el balanceador
# del Ingress (Service tipo LoadBalancer).
resource "aws_ec2_tag" "subnet_cluster" {
  for_each    = toset(data.aws_subnets.selected.ids)
  resource_id = each.value
  key         = "kubernetes.io/cluster/${local.cluster_name}"
  value       = "shared"
}

resource "aws_ec2_tag" "subnet_elb" {
  for_each    = toset(data.aws_subnets.selected.ids)
  resource_id = each.value
  key         = "kubernetes.io/role/elb"
  value       = "1"
}

# -----------------------------------------------------------------------------
# EKS
# -----------------------------------------------------------------------------
resource "aws_eks_cluster" "this" {
  name     = local.cluster_name
  version  = var.cluster_version
  role_arn = data.aws_iam_role.lab.arn

  vpc_config {
    subnet_ids              = data.aws_subnets.selected.ids
    endpoint_public_access  = true
    endpoint_private_access = false
  }

  # El principal que crea el clúster (rol voclabs) queda como administrador:
  # sirve para kubectl local y para el pipeline con las mismas credenciales.
  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = true
  }
}

resource "aws_eks_node_group" "app" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.project}-nodes"
  node_role_arn   = data.aws_iam_role.lab.arn
  subnet_ids      = data.aws_subnets.selected.ids
  instance_types  = [var.node_instance_type]
  capacity_type   = "ON_DEMAND"
  disk_size       = 20

  scaling_config {
    desired_size = var.node_count
    min_size     = var.node_count
    max_size     = var.node_count
  }

  # Los Deployments del repo usan nodeSelector workload=app.
  labels = {
    workload = "app"
  }

  # Evita bloquear el destroy si un nodo tarda en drenar.
  force_update_version = true

  depends_on = [aws_ec2_tag.subnet_cluster]
}

# El driver usa el rol de los nodos (LabRole). Si el laboratorio lo rechazara,
# poner enable_ebs_csi = false y ver docs/RUNBOOK-AWS.md (plan B de Postgres).
resource "aws_eks_addon" "ebs_csi" {
  count                       = var.enable_ebs_csi ? 1 : 0
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = "aws-ebs-csi-driver"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  depends_on = [aws_eks_node_group.app]
}

# -----------------------------------------------------------------------------
# ECR
# -----------------------------------------------------------------------------
resource "aws_ecr_repository" "images" {
  for_each             = toset(["backend", "frontend", "loadgen"])
  name                 = "${var.project}/${each.key}"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # permite destroy aunque haya imágenes

  image_scanning_configuration {
    scan_on_push = true
  }
}

# Conserva solo las 10 imágenes más recientes (ahorra almacenamiento).
resource "aws_ecr_lifecycle_policy" "images" {
  for_each   = aws_ecr_repository.images
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Conservar las ultimas 10 imagenes"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}
