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

# -----------------------------------------------------------------------------
# Postgres en EC2 aparte (persistencia real sin necesitar el addon EBS CSI)
# -----------------------------------------------------------------------------
# El Learner Lab no permite instalar aws-ebs-csi-driver (LabRole no tiene
# iam:PassRole para el addon). Sin CSI no hay PVC persistente en EKS.
# Solución: un EC2 t3.micro en la misma VPC cuyo disco raíz (/dev/xvda)
# es un volumen EBS. Si apagás la instancia (stop), el disco queda guardado.
# Los pods de EKS llegan por IP privada (ver no ingress 0.0.0.0/0).
data "aws_ami" "amazon_linux2" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["amzn2-ami-hvm-2.0.*.x86_64-gp2"]
  }
}

resource "random_password" "pg_password" {
  length  = 24
  special = false
}

resource "aws_security_group" "postgres" {
  name        = "${var.project}-postgres-sg"
  description = "Postgres en EC2: solo desde la VPC (pods EKS)"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Postgres desde VPC"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "postgres" {
  ami                    = data.aws_ami.amazon_linux2.id
  instance_type          = var.db_instance_type
  subnet_id              = data.aws_subnets.selected.ids[0]
  vpc_security_group_ids = [aws_security_group.postgres.id]
  monitoring             = false
  disable_api_termination = false

  root_block_device {
    volume_size           = var.db_root_volume_size
    volume_type           = "gp2"
    delete_on_termination = false
    encrypted             = false
    tags = { Name = "${var.project}-postgres" }
  }

  user_data = <<-EOT
    #!/bin/bash
    set -eux
    dnf install -y postgresql-server postgresql-contrib
    /usr/share/postgresql-setup --initdb --encoding=UTF8
    systemctl enable postgresql
    systemctl start postgresql
    sudo -u postgres psql -c "CREATE USER kuber WITH PASSWORD '${random_password.pg_password.result}' LOGIN SUPERUSER;" || true
    sudo -u postgres psql -c "CREATE DATABASE kuber OWNER kuber;" || true
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE kuber TO kuber;" || true
    # escucha en red privada de la VPC (pods EKS)
    sed -i "s/^#listen_addresses = 'localhost'/listen_addresses = '*'/" /var/lib/pgsql/data/postgresql.conf
    echo "host all all ${data.aws_vpc.default.cidr_block} md5" >> /var/lib/pgsql/data/pg_hba.conf
    systemctl restart postgresql
    systemctl is-enabled postgresql
    # --- Backup local del disco (aparte del EBS snapshot de Terraform) ---
    mkdir -p /var/backups/kuber
    chown postgres:postgres /var/backups/kuber
    cat > /usr/local/bin/kuber-pg-backup.sh <<'EOS'
    #!/bin/bash
    # pg_dump diario + rotación 7 días. Corre en el mismo disco: sirve si
    # se corrompe la BD, no si se pierde el volumen entero (para eso está
    # el snapshot EBS de Terraform).
    set -euo pipefail
    DUMP=/var/backups/kuber/kuber-$(date +%Y-%m-%d).sql.gz
    sudo -u postgres pg_dump -Fc kuber | gzip > $DUMP
    find /var/backups/kuber -name 'kuber-*.sql.gz' -mtime +7 -delete
    EOS
    chmod +x /usr/local/bin/kuber-pg-backup.sh
    echo "0 2 * * * root /usr/local/bin/kuber-pg-backup.sh" > /etc/cron.d/kuber-pg-backup
    /usr/local/bin/kuber-pg-backup.sh
  EOT

  tags = { Name = "${var.project}-postgres-ec2" }
}

# Snapshot inicial del disco raíz: si se termina la instancia por error,
# el volumen queda huérfano pero los datos están en este snapshot.
resource "aws_ebs_snapshot" "postgres_root" {
  volume_id   = aws_instance.postgres.root_block_device[0].volume_id
  description = "Snapshot inicial Postgres EC2 (backup)"
  tags        = { Name = "${var.project}-postgres-snapshot" }
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
