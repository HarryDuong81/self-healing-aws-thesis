# =============================================================================
# module/network — VPC, Subnets, Gateways, Security Groups, SSM Endpoints
# =============================================================================

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# ── VPC ───────────────────────────────────────────────────────────────────────
resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${local.name_prefix}-vpc" }
}

# ── Subnets ───────────────────────────────────────────────────────────────────
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = var.public_subnet_cidr
  availability_zone       = var.availability_zone
  map_public_ip_on_launch = false # monitoring server gets an EIP instead

  tags = { Name = "${local.name_prefix}-public", Tier = "public" }
}

resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_subnet_cidr
  availability_zone = var.availability_zone

  tags = { Name = "${local.name_prefix}-private", Tier = "private" }
}

# ── Internet Gateway ──────────────────────────────────────────────────────────
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name_prefix}-igw" }
}

# ── NAT Gateway (for private-subnet egress) ───────────────────────────────────
resource "aws_eip" "nat" {
  domain     = "vpc"
  depends_on = [aws_internet_gateway.igw]
  tags       = { Name = "${local.name_prefix}-nat-eip" }
}

resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  depends_on    = [aws_internet_gateway.igw]
  tags          = { Name = "${local.name_prefix}-nat-gw" }
}

# ── Route Tables ─────────────────────────────────────────────────────────────
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = { Name = "${local.name_prefix}-public-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat.id
  }

  tags = { Name = "${local.name_prefix}-private-rt" }
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

# ── Security Group: Monitoring Server ────────────────────────────────────────
resource "aws_security_group" "orchestrator" {
  name        = "${local.name_prefix}-sg-orchestrator"
  description = "Self-healing monitoring server (Prometheus/Grafana/Flask)"
  vpc_id      = aws_vpc.main.id

  # All ingress rules are declared as standalone aws_security_group_rule
  # resources below (orchestrator_ingress_*), not inline here. Confirmed
  # live: mixing inline ingress blocks on this resource with the standalone
  # Filebeat/OTel rules (needed to avoid a dependency cycle with sg-targets,
  # see those resources' own comments) caused the two management styles to
  # perpetually fight each other — each `terraform apply` would silently
  # revert whichever style wasn't just applied, including a live incident
  # where the Filebeat/OTel ingress rules were transiently deleted by an
  # inline-only apply. Egress stays inline since no standalone egress rule
  # exists for this SG to conflict with it.
  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-sg-orchestrator" }
}

resource "aws_security_group_rule" "orchestrator_ingress_ssh" {
  type              = "ingress"
  description       = "SSH from researcher"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  cidr_blocks       = [var.researcher_cidr]
}

resource "aws_security_group_rule" "orchestrator_ingress_grafana" {
  type              = "ingress"
  description       = "Grafana from researcher"
  from_port         = 3000
  to_port           = 3000
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  cidr_blocks       = [var.researcher_cidr]
}

resource "aws_security_group_rule" "orchestrator_ingress_kibana" {
  type              = "ingress"
  description       = "Kibana from researcher"
  from_port         = 5601
  to_port           = 5601
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  cidr_blocks       = [var.researcher_cidr]
}

resource "aws_security_group_rule" "orchestrator_ingress_prometheus" {
  type              = "ingress"
  description       = "Prometheus from researcher"
  from_port         = 9090
  to_port           = 9090
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  cidr_blocks       = [var.researcher_cidr]
}

resource "aws_security_group_rule" "orchestrator_ingress_jaeger" {
  type              = "ingress"
  description       = "Jaeger UI from researcher"
  from_port         = 16686
  to_port           = 16686
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  cidr_blocks       = [var.researcher_cidr]
}

resource "aws_security_group_rule" "orchestrator_ingress_alertmanager_self" {
  type              = "ingress"
  description       = "Alertmanager internal"
  from_port         = 9093
  to_port           = 9093
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  self              = true
}

resource "aws_security_group_rule" "orchestrator_ingress_webhook_self" {
  # Flask orchestrator webhook + metrics (Thesis Table 4.3, Listing 4.9)
  # Port 9094: Alertmanager → orchestrator:9094/alert (Docker bridge)
  #            Prometheus   → localhost:9094/metrics
  type              = "ingress"
  description       = "Flask orchestrator webhook and metrics (port 9094)"
  from_port         = 9094
  to_port           = 9094
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  self              = true
}

resource "aws_security_group_rule" "orchestrator_ingress_node_exporter" {
  type              = "ingress"
  description       = "Node Exporter scrape from private subnet"
  from_port         = 9100
  to_port           = 9100
  protocol          = "tcp"
  security_group_id = aws_security_group.orchestrator.id
  cidr_blocks       = [var.private_subnet_cidr]
}

# ── Security Group: Target Instances ─────────────────────────────────────────
resource "aws_security_group" "targets" {
  name        = "${local.name_prefix}-sg-targets"
  description = "Target EC2 instances - SSM + monitoring scrape only, no inbound SSH"
  vpc_id      = aws_vpc.main.id

  # All ingress rules are declared as standalone aws_security_group_rule
  # resources below (targets_ingress_*), not inline here — matching the same
  # standalone-only discipline sg-orchestrator already follows (see that
  # resource's own comment for the live incident that motivated it: mixing
  # inline and standalone rules on one security group makes Terraform's two
  # management styles fight each other, silently reverting whichever style
  # wasn't just applied). sg-targets never actually hit that incident (it had
  # no standalone rules to conflict with), but converting proactively removes
  # the same latent risk before a future standalone rule addition reproduces
  # it here too. Egress stays inline since no standalone egress rule exists
  # for this SG to conflict with it.

  # Filebeat → Logstash on monitoring server
  egress {
    description     = "Filebeat to Logstash"
    from_port       = 5044
    to_port         = 5044
    protocol        = "tcp"
    security_groups = [aws_security_group.orchestrator.id]
  }

  # OTel Collector → Jaeger on monitoring server
  egress {
    description     = "OTel to Jaeger"
    from_port       = 4317
    to_port         = 4317
    protocol        = "tcp"
    security_groups = [aws_security_group.orchestrator.id]
  }

  # HTTPS for SSM Agent, apt-get, etc.
  egress {
    description = "HTTPS outbound"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # HTTP for apt mirrors
  egress {
    description = "HTTP outbound"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-sg-targets" }
}

resource "aws_security_group_rule" "targets_ingress_ssh" {
  # SSH from the monitoring/orchestrator server only (deliberate reversal of
  # the thesis's no-SSH-on-targets invariant — authorised as a workaround for
  # a confirmed AWS Session Manager plugin defect where the aws_ssm
  # connection plugin's interactive session starts remote command execution
  # but never receives the completion message back over the session
  # WebSocket. Scoped to sg-orchestrator only, never 0.0.0.0/0. This is the
  # Ansible remediation-transport rule (see orchestrator/execute.py).
  type                     = "ingress"
  description              = "SSH from orchestrator SG (Ansible control node)"
  from_port                = 22
  to_port                  = 22
  protocol                 = "tcp"
  security_group_id        = aws_security_group.targets.id
  source_security_group_id = aws_security_group.orchestrator.id
}

resource "aws_security_group_rule" "targets_ingress_node_exporter" {
  # Scraped by Prometheus on the monitoring server.
  type                     = "ingress"
  description              = "Node Exporter from orchestrator SG"
  from_port                = 9100
  to_port                  = 9100
  protocol                 = "tcp"
  security_group_id        = aws_security_group.targets.id
  source_security_group_id = aws_security_group.orchestrator.id
}

resource "aws_security_group_rule" "targets_ingress_nginx_health" {
  # NGINX /health + /nginx_status — scraped by Prometheus.
  type                     = "ingress"
  description              = "NGINX health from orchestrator SG"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  security_group_id        = aws_security_group.targets.id
  source_security_group_id = aws_security_group.orchestrator.id
}

# ── Security Group: SSM VPC Interface Endpoints ───────────────────────────────
resource "aws_security_group" "ssm_endpoints" {
  name        = "${local.name_prefix}-sg-ssm-endpoints"
  description = "Allow HTTPS from VPC to SSM interface endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name_prefix}-sg-ssm-endpoints" }
}

# ── SSM VPC Interface Endpoints ───────────────────────────────────────────────
# Required for private-subnet instances to reach SSM without internet access.
locals {
  ssm_services = ["ssm", "ssmmessages", "ec2messages"]
}

resource "aws_vpc_endpoint" "ssm" {
  for_each = toset(local.ssm_services)

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${data.aws_region.current.name}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private.id]
  security_group_ids  = [aws_security_group.ssm_endpoints.id]
  private_dns_enabled = true

  tags = { Name = "${local.name_prefix}-endpoint-${each.key}" }
}

data "aws_region" "current" {}

# ── sg-orchestrator <-> sg-targets diagnostic-path ingress ───────────────────
# Declared as standalone rules (not inline in either aws_security_group
# block) to avoid the circular dependency that would result from both
# security groups referencing each other's id inline: sg-targets already
# references sg-orchestrator.id inline (Node Exporter / NGINX ingress,
# Filebeat / OTel egress); these two rules are the corresponding
# sg-orchestrator-side ingress that lets those connections actually
# complete (security groups are evaluated independently per ENI in both
# directions — an egress rule on one side is not sufficient by itself).

resource "aws_security_group_rule" "orchestrator_ingress_filebeat" {
  type                     = "ingress"
  description              = "Filebeat to Logstash from sg-targets"
  from_port                = 5044
  to_port                  = 5044
  protocol                 = "tcp"
  security_group_id        = aws_security_group.orchestrator.id
  source_security_group_id = aws_security_group.targets.id
}

resource "aws_security_group_rule" "orchestrator_ingress_otel" {
  type                     = "ingress"
  description              = "OTel agent to central Collector from sg-targets"
  from_port                = 4317
  to_port                  = 4317
  protocol                 = "tcp"
  security_group_id        = aws_security_group.orchestrator.id
  source_security_group_id = aws_security_group.targets.id
}
