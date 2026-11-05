terraform {
  backend "s3" {}
}

variable "env" {
  type = string
}

variable "settings" {
  type = object({
    tier   = optional(string, "standard")
    queues = optional(list(string), ["orders", "billing"])
    extra  = optional(string)
  })
  default = {}
}

variable "ports" {
  type    = list(number)
  default = [443, 8443]
}

variable "image_tag" {
  type = string
}

variable "alert_email" {
  type = string
}

variable "enable_queue" {
  type    = bool
  default = true
}

data "terraform_remote_state" "network" {
  backend = "s3"
  config = {
    bucket = "example-tfstate-${var.env}"
    key    = "network/terraform.tfstate"
    region = "us-east-1"
  }
}

data "terraform_remote_state" "shared" {
  backend = "s3"
  config = {
    bucket = "example-tfstate-shared"
    key    = "shared/terraform.tfstate"
    region = "us-east-1"
  }
}

locals {
  org    = data.terraform_remote_state.shared.outputs.org_prefix
  prefix = "${local.org}-${var.env}"
  # chain: local -> local -> remote state output of the same environment
  vpc_label = upper(data.terraform_remote_state.network.outputs.vpc_name)
}

module "queue" {
  source = "../../modules/queue"
  count  = var.enable_queue ? 1 : 0
  name   = "${local.prefix}-orders"
}

module "worker" {
  source   = "../../modules/queue"
  for_each = toset(var.settings.queues)
  name     = "${local.prefix}-${each.key}"
}

module "registry" {
  source  = "example-org/registry-module/aws"
  version = "1.0.0"
}

resource "aws_sqs_queue" "main" {
  # module output of a module with count: module.queue[0].<output>
  name = "${module.queue[0].queue_name}-main"
  tags = {
    Tier    = var.settings.tier
    Vpc     = local.vpc_label
    Workdir = path.cwd
    Space   = terraform.workspace
  }
}

resource "aws_sqs_queue" "derived" {
  name = "${local.prefix}-derived"
  tags = {
    # all of these only exist after apply and the code shows where they come from
    Region  = split(":", aws_sqs_queue.main.arn)[3]
    Created = timestamp()
    Peer    = coalesce(var.settings.extra, aws_sqs_queue.main.arn)
    Remote  = module.registry.some_arn
    All     = join(",", concat(aws_subnet.app[*].id, [aws_sqs_queue.main.arn]))
  }
}

resource "aws_subnet" "app" {
  count      = length(values(module.worker))
  vpc_id     = data.terraform_remote_state.network.outputs.vpc_id
  cidr_block = data.terraform_remote_state.network.outputs["subnet_cidr"]
}

resource "aws_security_group" "app" {
  name   = "${local.prefix}-app"
  vpc_id = data.terraform_remote_state.network.outputs.vpc_id

  dynamic "ingress" {
    for_each = var.ports
    content {
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = ["10.0.0.0/8"]
    }
  }
}

resource "aws_instance" "app" {
  count         = length(aws_subnet.app)
  ami           = "ami-00000000"
  instance_type = var.env == "prod" ? "m5.large" : "t3.small"
  subnet_id     = aws_subnet.app[count.index].id
  user_data = templatefile("${path.module}/user_data.tpl", {
    env   = var.env
    ports = var.ports
  })
}

resource "aws_sns_topic" "alerts" {
  name = "%{if var.env == "prod"}${local.prefix}-pager%{else}${local.prefix}-chat%{endif}"
}

# what stays unknown: the value is not in the repository
resource "aws_lambda_function" "api" {
  function_name = "${local.prefix}-api"
  role          = "arn:aws:iam::123456789012:role/example"
  handler       = "app.handler"
  runtime       = "python3.12"
  image_uri     = "example.com/app:${var.image_tag}"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}
