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
        for table_name, changes in alter_map.items():
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, sql in changes:
                if column_name not in existing_columns:
                    conn.execute(text(sql))
