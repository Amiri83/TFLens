resource "aws_vpc" "main" {
  cidr_block = var.vpc_cidr

  tags = {
    Name = local.name
  }
}

resource "aws_subnet" "private" {
  count             = length(var.private_cidrs)
  vpc_id            = aws_vpc.main.id
  cidr_block        = var.private_cidrs[count.index]
  availability_zone = element(var.azs, count.index)

  tags = {
    Name = "${local.name}-private-${count.index}"
  }
}

resource "aws_security_group" "app" {
  name   = join("-", [local.name, "app"])
  vpc_id = aws_vpc.main.id
}

module "events" {
  source   = "../../modules/queue"
  name     = "${local.name}-events"
  with_dlq = var.env == "prod"
  tags     = local.common_tags
}
