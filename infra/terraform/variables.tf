variable "region" {
  description = "Región de AWS. El Learner Lab solo permite us-east-1 y us-west-2."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Prefijo/etiqueta de todos los recursos."
  type        = string
  default     = "chaos-lab"
}

variable "cluster_version" {
  description = "Versión de Kubernetes del plano de control de EKS."
  type        = string
  default     = "1.31"
}

variable "azs" {
  description = "Zonas de disponibilidad. Se evita us-east-1e: EKS no admite el plano de control allí."
  type        = list(string)
  default     = ["us-east-1a", "us-east-1b", "us-east-1c"]
}

variable "node_instance_type" {
  description = "Tipo de instancia de los nodos (el Learner Lab limita a tamaños pequeños)."
  type        = string
  default     = "t3.medium"
}

variable "node_count" {
  description = "Número de nodos. 3 permite repartir backend/postgres/monitoring con margen."
  type        = number
  default     = 3
}

variable "enable_ebs_csi" {
  description = "Instala el driver EBS CSI (necesario para el volumen persistente de Postgres)."
  type        = bool
  # En Learner Lab el driver falla por permisos (LabRole no puede crear
  # el addon aws-ebs-csi-driver). Por defecto va desactivado y Postgres
  # corre en un EC2 aparte (ver main.tf), que sí persiste en EBS root.
  default = false
}

variable "db_instance_type" {
  description = "Tipo de instancia para el EC2 de Postgres."
  type        = string
  default     = "t3.micro"
}

variable "db_root_volume_size" {
  description = "GB del disco raíz del EC2 de Postgres (persiste si se apaga la instancia)."
  type        = number
  default     = 20
}
