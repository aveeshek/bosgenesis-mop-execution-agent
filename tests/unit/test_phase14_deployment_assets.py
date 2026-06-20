from pathlib import Path

CHART_ROOT = Path("helm/bosgenesis-mop-execution-agent")


def test_helm_chart_defines_runtime_units_and_config() -> None:
    values = (CHART_ROOT / "values.yaml").read_text(encoding="utf-8")
    chart = (CHART_ROOT / "Chart.yaml").read_text(encoding="utf-8")

    assert "name: bosgenesis-mop-execution-agent" in chart
    assert "workerConcurrency" in values
    assert "namespaceLockLeaseSeconds" in values
    assert 'memoryEnabled: "true"' in values
    assert 'memoryPostgresEnabled: "true"' in values
    assert 'postgresEnabled: "true"' in values
    assert 'postgresSchema: "mop_execution"' in values
    assert "postgresDsnSecret" in values
    assert "databaseUrlSecret" in values
    assert "redisUrlSecret" in values
    assert "maxParallelJobsPerNamespace" in values


def test_helm_templates_cover_required_phase14_resources() -> None:
    templates = {path.name for path in (CHART_ROOT / "templates").glob("*")}

    assert {
        "api-deployment.yaml",
        "api-service.yaml",
        "worker-deployment.yaml",
        "reconciler-deployment.yaml",
        "migration-job.yaml",
        "configmap.yaml",
        "serviceaccount.yaml",
        "role.yaml",
        "rolebinding.yaml",
        "networkpolicy.yaml",
        "pvc.yaml",
        "ingress.yaml",
    }.issubset(templates)


def test_playbook_scripts_match_repo_deployment_pattern() -> None:
    deploy = Path("playbook/deploy.sh").read_text(encoding="utf-8")
    undeploy = Path("playbook/undeploy.sh").read_text(encoding="utf-8")

    assert "docker build" in deploy
    assert "docker save" in deploy
    assert "upgrade" in deploy
    assert "--install" in deploy
    assert "kubectl rollout status" in deploy
    assert "helm uninstall" in undeploy
    assert "DELETE_NAMESPACE" in undeploy


def test_deployment_docs_include_runbook_and_sample_requests() -> None:
    deployment = Path("docs/DEPLOYMENT.md").read_text(encoding="utf-8")
    runbook = Path("docs/RUNBOOK.md").read_text(encoding="utf-8")
    samples = Path("docs/SAMPLE_REQUESTS.md").read_text(encoding="utf-8")

    assert "migrations/postgres/0001_phase2_core.sql" in deployment
    assert "migrations/postgres/0002_phase11_memory.sql" in deployment
    assert "POSTGRES_DSN" in deployment
    assert "memoryEnabled" in deployment
    assert "./playbook/deploy.sh" in runbook
    assert "/v1/execution-jobs" in samples
    assert "dry_run_only" in samples


def test_helm_templates_enable_memory_and_creation_agent_postgres_convention() -> None:
    configmap = (CHART_ROOT / "templates" / "configmap.yaml").read_text(encoding="utf-8")
    secret_env = (CHART_ROOT / "templates" / "_secret-env.tpl").read_text(encoding="utf-8")
    credentials = (CHART_ROOT / "values.credentials.example.yaml").read_text(encoding="utf-8")

    assert "MEMORY_ENABLED" in configmap
    assert "MEMORY_POSTGRES_ENABLED" in configmap
    assert "POSTGRES_ENABLED" in configmap
    assert "POSTGRES_SCHEMA" in configmap
    assert "POSTGRES_DSN" in secret_env
    assert "postgresql.bosgenesis.svc.cluster.local:5432/bosgenesis" in credentials
