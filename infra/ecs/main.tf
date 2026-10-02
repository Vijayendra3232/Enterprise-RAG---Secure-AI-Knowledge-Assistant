# Amazon ECS Fargate Cluster and Services Deployment

resource "aws_ecs_cluster" "main" {
  name = "${var.project_name}-cluster-${var.environment}"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name = "${var.project_name}-cluster-${var.environment}"
  }
}

# ─── 1. API Task Definition & Service ────────────────────────────────────────

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project_name}-api-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_api_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "rag-api"
      image     = var.container_image
      essential = true

      portMappings = [
        {
          containerPort = 8000
          hostPort      = 8000
          protocol      = "tcp"
        }
      ]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "APP_ENV", value = var.environment },
        { name = "WORKER_EMBEDDED", value = "false" },
        { name = "DOCUMENT_STORAGE_TYPE", value = "s3" },
        { name = "S3_BUCKET", value = var.s3_documents_bucket_name },
        { name = "SEARCH_STORE_TYPE", value = "opensearch" },
        { name = "OPENSEARCH_URL", value = "https://${var.opensearch_endpoint}:443" },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "SECRET_PROVIDER_TYPE", value = "aws" },
        { name = "AWS_SECRET_PREFIX", value = "${var.project_name}/${var.environment}/" }
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = "${var.secrets_prefix}/database" },
        { name = "JWT_SECRET_KEY", valueFrom = "${var.secrets_prefix}/auth:JWT_SECRET_KEY::" },
        { name = "TELEMETRY_HMAC_KEY", valueFrom = "${var.secrets_prefix}/auth:TELEMETRY_HMAC_KEY::" },
        { name = "MONITORING_API_KEY", valueFrom = "${var.secrets_prefix}/auth:MONITORING_API_KEY::" },
        { name = "GROQ_API_KEY", valueFrom = "${var.secrets_prefix}/llm:GROQ_API_KEY::" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.project_name}-api-${var.environment}"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-api-task-def-${var.environment}"
  }
}

resource "aws_ecs_service" "api" {
  name            = "${var.project_name}-api-service-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_min_tasks
  launch_type     = "FARGATE"

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  network_configuration {
    subnets          = var.private_app_subnet_ids
    security_groups  = [var.api_security_group_id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.alb_target_group_arn
    container_name   = "rag-api"
    container_port   = 8000
  }

  depends_on = [var.alb_listener_arn]

  tags = {
    Name = "${var.project_name}-api-service-${var.environment}"
  }
}

# ─── 2. Worker Task Definition & Standalone Service ──────────────────────────

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.project_name}-worker-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "2048"
  memory                   = "4096"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_worker_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "rag-worker"
      image     = var.container_image
      essential = true
      command   = ["python", "-m", "app.tasks.worker"]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "APP_ENV", value = var.environment },
        { name = "WORKER_EMBEDDED", value = "false" },
        { name = "WORKER_CONCURRENCY", value = "4" },
        { name = "DOCUMENT_STORAGE_TYPE", value = "s3" },
        { name = "S3_BUCKET", value = var.s3_documents_bucket_name },
        { name = "SEARCH_STORE_TYPE", value = "opensearch" },
        { name = "OPENSEARCH_URL", value = "https://${var.opensearch_endpoint}:443" },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "SECRET_PROVIDER_TYPE", value = "aws" },
        { name = "AWS_SECRET_PREFIX", value = "${var.project_name}/${var.environment}/" }
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = "${var.secrets_prefix}/database" },
        { name = "JWT_SECRET_KEY", valueFrom = "${var.secrets_prefix}/auth:JWT_SECRET_KEY::" },
        { name = "TELEMETRY_HMAC_KEY", valueFrom = "${var.secrets_prefix}/auth:TELEMETRY_HMAC_KEY::" },
        { name = "GROQ_API_KEY", valueFrom = "${var.secrets_prefix}/llm:GROQ_API_KEY::" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.project_name}-worker-${var.environment}"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-worker-task-def-${var.environment}"
  }
}

resource "aws_ecs_service" "worker" {
  name            = "${var.project_name}-worker-service-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_min_tasks
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_app_subnet_ids
    security_groups  = [var.worker_security_group_id]
    assign_public_ip = false
  }

  tags = {
    Name = "${var.project_name}-worker-service-${var.environment}"
  }
}

# ─── 3. Independent Metrics Publisher Task Definition & Service ──────────────

resource "aws_ecs_task_definition" "metrics_publisher" {
  family                   = "${var.project_name}-metrics-publisher-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_metrics_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "rag-metrics-publisher"
      image     = var.container_image
      essential = true
      command   = ["python", "-m", "app.tasks.metrics_publisher"]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "APP_ENV", value = var.environment },
        { name = "AWS_REGION", value = var.aws_region },
        { name = "METRICS_POLL_INTERVAL_SECONDS", value = "15" }
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = "${var.secrets_prefix}/database" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.project_name}-metrics-publisher-${var.environment}"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "metrics"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-metrics-publisher-task-def-${var.environment}"
  }
}

resource "aws_ecs_service" "metrics_publisher" {
  name            = "${var.project_name}-metrics-publisher-service-${var.environment}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.metrics_publisher.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_app_subnet_ids
    security_groups  = [var.metrics_security_group_id]
    assign_public_ip = false
  }

  tags = {
    Name = "${var.project_name}-metrics-publisher-service-${var.environment}"
  }
}

# ─── 4. Database Migration Task Definition (One-off execution) ───────────────

resource "aws_ecs_task_definition" "migration" {
  family                   = "${var.project_name}-migration-${var.environment}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.migration_task_role_arn

  container_definitions = jsonencode([
    {
      name      = "migration"
      image     = var.container_image
      essential = true
      command   = ["alembic", "upgrade", "head"]

      environment = [
        { name = "ENVIRONMENT", value = var.environment },
        { name = "APP_ENV", value = var.environment },
        { name = "AWS_REGION", value = var.aws_region }
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = "${var.secrets_prefix}/database" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.project_name}-migration-${var.environment}"
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "migration"
        }
      }
    }
  ])

  tags = {
    Name = "${var.project_name}-migration-task-def-${var.environment}"
  }
}

# ─── 5. Worker Autoscaling Policies (Driven by CloudWatch Queue Depth) ──────

resource "aws_appautoscaling_target" "worker_scale_target" {
  max_capacity       = var.worker_max_tasks
  min_capacity       = var.worker_min_tasks
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

# Scale workers based on task queue depth (Correction 7)
resource "aws_appautoscaling_policy" "worker_queue_depth_scaling" {
  name               = "${var.project_name}-worker-queue-depth-scaling-${var.environment}"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.worker_scale_target.resource_id
  scalable_dimension = aws_appautoscaling_target.worker_scale_target.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker_scale_target.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 5.0 # Target 5 pending tasks per worker
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    customized_metric_specification {
      metric_name = "rag_task_queue_depth"
      namespace   = "EnterpriseRAG/Tasks"
      statistic   = "Average"

      dimensions {
        name  = "Environment"
        value = var.environment
      }
      dimensions {
        name  = "TaskType"
        value = "ALL"
      }
    }
  }
}
