from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Settings:
    app_env: str = "development"
    demo_now: str = "2026-08-18T09:00:00+08:00"
    seed: int = 42
    # Database access requires the deployment's explicit configuration.
    database_url: str = ""
    ontop_url: str = "http://localhost:8080/sparql"
    fuseki_url: str = "http://localhost:3030/supply"
    semantica_url: str = "http://127.0.0.1:8001"
    redis_url: str = "redis://localhost:6379/0"
    llm_mode: str = "mock"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.1
    realtime_project_dir: str = ""
    ontop_deployment_binding: str = ""
    realtime_runtime_registry_path: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            app_env=os.getenv("APP_ENV", cls.app_env),
            demo_now=os.getenv("DEMO_NOW", cls.demo_now),
            seed=int(os.getenv("SEED", str(cls.seed))),
            database_url=os.getenv("DATABASE_URL", cls.database_url),
            ontop_url=os.getenv("ONTOP_URL", cls.ontop_url),
            fuseki_url=os.getenv("FUSEKI_URL", cls.fuseki_url),
            semantica_url=os.getenv("SEMANTICA_API_URL", cls.semantica_url),
            redis_url=os.getenv("REDIS_URL", cls.redis_url),
            llm_mode=os.getenv("LLM_MODE", cls.llm_mode),
            llm_base_url=os.getenv("LLM_BASE_URL", cls.llm_base_url),
            llm_api_key=os.getenv("LLM_API_KEY", cls.llm_api_key),
            llm_model=os.getenv("LLM_MODEL", cls.llm_model),
            llm_temperature=float(os.getenv("LLM_TEMPERATURE", str(cls.llm_temperature))),
            realtime_project_dir=os.getenv(
                "ORION_REALTIME_PROJECT_DIR",
                cls.realtime_project_dir,
            ),
            ontop_deployment_binding=os.getenv(
                "ORION_ONTOP_DEPLOYMENT_BINDING",
                cls.ontop_deployment_binding,
            ),
            realtime_runtime_registry_path=os.getenv(
                "ORION_REALTIME_RUNTIME_REGISTRY",
                cls.realtime_runtime_registry_path,
            ),
        )

    def parsed_demo_now(self) -> datetime:
        return datetime.fromisoformat(self.demo_now)


settings = Settings.from_env()
