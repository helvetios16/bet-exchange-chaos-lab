output "cluster_name" {
  value = aws_eks_cluster.this.name
}

output "region" {
  value = var.region
}

output "account_id" {
  value = data.aws_caller_identity.current.account_id
}

output "ecr_registry" {
  description = "Registro ECR (prefijo de las imágenes)."
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.images : k => r.repository_url }
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${aws_eks_cluster.this.name}"
}

output "postgres_private_ip" {
  description = "IP privada del EC2 de Postgres (DB_HOST para backend)."
  value       = aws_instance.postgres.private_ip
}

output "postgres_public_ip" {
  description = "IP pública del EC2 de Postgres (solo para depurar con ssh si hay key)."
  value       = aws_instance.postgres.public_ip
}

output "postgres_instance_id" {
  value = aws_instance.postgres.id
}

output "postgres_password" {
  description = "Contraseña de Postgres (sensible, solo para crear el secret de K8s)."
  value       = random_password.pg_password.result
  sensitive   = true
}
