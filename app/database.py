from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings


settings = get_settings()


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema_compatibility() -> None:
    inspector = inspect(engine)
    if engine.dialect.name != "sqlite":
        return

    existing_tables = set(inspector.get_table_names())
    alter_map: dict[str, list[tuple[str, str]]] = {
        "deploy_releases": [
            ("source_type", "ALTER TABLE deploy_releases ADD COLUMN source_type VARCHAR(40) DEFAULT 'manual' NOT NULL"),
            ("storage_mode", "ALTER TABLE deploy_releases ADD COLUMN storage_mode VARCHAR(40) DEFAULT 'manual' NOT NULL"),
        ],
        "deploy_projects": [
            ("workspace_path", "ALTER TABLE deploy_projects ADD COLUMN workspace_path VARCHAR(255)"),
            ("image_registry_prefix", "ALTER TABLE deploy_projects ADD COLUMN image_registry_prefix VARCHAR(255)"),
            ("default_artifact_repository_id", "ALTER TABLE deploy_projects ADD COLUMN default_artifact_repository_id INTEGER"),
        ],
        "deploy_project_components": [
            ("build_enabled", "ALTER TABLE deploy_project_components ADD COLUMN build_enabled BOOLEAN DEFAULT 1 NOT NULL"),
        ],
        "deploy_deployments": [
            ("submitted_at", "ALTER TABLE deploy_deployments ADD COLUMN submitted_at DATETIME"),
            ("last_polled_at", "ALTER TABLE deploy_deployments ADD COLUMN last_polled_at DATETIME"),
            ("external_request_id", "ALTER TABLE deploy_deployments ADD COLUMN external_request_id VARCHAR(255)"),
            ("status_reason", "ALTER TABLE deploy_deployments ADD COLUMN status_reason TEXT"),
            ("progress_percent", "ALTER TABLE deploy_deployments ADD COLUMN progress_percent INTEGER DEFAULT 0 NOT NULL"),
        ],
        "deploy_operator_users": [
            ("preferred_lang", "ALTER TABLE deploy_operator_users ADD COLUMN preferred_lang VARCHAR(10)"),
            ("display_name", "ALTER TABLE deploy_operator_users ADD COLUMN display_name VARCHAR(120)"),
            ("last_login_at", "ALTER TABLE deploy_operator_users ADD COLUMN last_login_at DATETIME"),
        ],
        "deploy_operator_user_roles": [
            ("project_id", "ALTER TABLE deploy_operator_user_roles ADD COLUMN project_id INTEGER"),
        ],
    }

    with engine.begin() as conn:
        if "deploy_project_components" not in existing_tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_project_components (
                        id INTEGER NOT NULL PRIMARY KEY,
                        project_id INTEGER NOT NULL,
                        name VARCHAR(120) NOT NULL,
                        service_name VARCHAR(120) NOT NULL,
                        image VARCHAR(255) NOT NULL,
                        dockerfile VARCHAR(255) NOT NULL DEFAULT './Dockerfile',
                        context_path VARCHAR(255) NOT NULL DEFAULT '.',
                        tar_name_pattern VARCHAR(255) NOT NULL,
                        build_enabled BOOLEAN NOT NULL DEFAULT 1,
                        enabled BOOLEAN NOT NULL DEFAULT 1,
                        default_selected BOOLEAN NOT NULL DEFAULT 1,
                        source_type VARCHAR(40) NOT NULL DEFAULT 'manual',
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES deploy_projects (id),
                        CONSTRAINT uq_deploy_project_component_project_name UNIQUE (project_id, name),
                        CONSTRAINT uq_deploy_project_component_project_service UNIQUE (project_id, service_name)
                    )
                    """
                )
            )
        if "deploy_operator_user_roles" not in existing_tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_operator_user_roles (
                        id INTEGER NOT NULL PRIMARY KEY,
                        user_id INTEGER NOT NULL,
                        role_code VARCHAR(80) NOT NULL,
                        project_id INTEGER,
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES deploy_operator_users (id),
                        FOREIGN KEY(project_id) REFERENCES deploy_projects (id),
                        CONSTRAINT uq_deploy_operator_user_role UNIQUE (user_id, role_code, project_id)
                    )
                    """
                )
            )
        if "deploy_artifact_repositories" not in existing_tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_artifact_repositories (
                        id INTEGER NOT NULL PRIMARY KEY,
                        name VARCHAR(120) NOT NULL,
                        slug VARCHAR(120) NOT NULL,
                        provider VARCHAR(40) NOT NULL,
                        bucket_name VARCHAR(255) NOT NULL,
                        region VARCHAR(120),
                        endpoint VARCHAR(500),
                        custom_domain VARCHAR(500),
                        path_prefix VARCHAR(255),
                        access_key_id_encrypted TEXT NOT NULL,
                        secret_access_key_encrypted TEXT NOT NULL,
                        is_active BOOLEAN NOT NULL DEFAULT 1,
                        is_default BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        CONSTRAINT uq_deploy_artifact_repository_slug UNIQUE (slug),
                        CONSTRAINT uq_deploy_artifact_repository_name UNIQUE (name)
                    )
                    """
                )
            )
        if "deploy_system_settings" not in existing_tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_system_settings (
                        key VARCHAR(120) NOT NULL PRIMARY KEY,
                        value TEXT,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
        if "deploy_audit_logs" not in existing_tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_audit_logs (
                        id INTEGER NOT NULL PRIMARY KEY,
                        actor_user_id INTEGER,
                        actor_username VARCHAR(120),
                        action VARCHAR(120) NOT NULL,
                        target_type VARCHAR(80) NOT NULL,
                        target_id VARCHAR(120),
                        summary TEXT NOT NULL,
                        detail_json TEXT,
                        request_path VARCHAR(500),
                        request_method VARCHAR(20),
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(actor_user_id) REFERENCES deploy_operator_users (id)
                    )
                    """
                )
            )
        if "deploy_project_governance" not in existing_tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_project_governance (
                        id INTEGER NOT NULL PRIMARY KEY,
                        project_id INTEGER NOT NULL,
                        owner_user_id INTEGER,
                        release_owner_user_id INTEGER,
                        risk_level VARCHAR(40) NOT NULL DEFAULT 'medium',
                        release_checklist_json TEXT,
                        notes TEXT,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES deploy_projects (id),
                        FOREIGN KEY(owner_user_id) REFERENCES deploy_operator_users (id),
                        FOREIGN KEY(release_owner_user_id) REFERENCES deploy_operator_users (id),
                        CONSTRAINT uq_deploy_project_governance_project UNIQUE (project_id)
                    )
                    """
                )
            )
        if "deploy_quality_check_runs" not in existing_tables:
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS deploy_quality_check_runs (
                        id INTEGER NOT NULL PRIMARY KEY,
                        project_id INTEGER NOT NULL,
                        status VARCHAR(40) NOT NULL DEFAULT 'pending',
                        score INTEGER NOT NULL DEFAULT 0,
                        summary TEXT,
                        result_json TEXT,
                        triggered_by VARCHAR(120),
                        created_at DATETIME NOT NULL,
                        FOREIGN KEY(project_id) REFERENCES deploy_projects (id)
                    )
                    """
                )
            )
        for table_name, changes in alter_map.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, sql in changes:
                if column_name not in existing_columns:
                    conn.execute(text(sql))
