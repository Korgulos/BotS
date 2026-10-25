from .config import Settings
from .db import engine_for, migrate

if __name__ == "__main__":
    settings = Settings.from_env()
    engine = engine_for(settings.migration_database_url or settings.database_url)
    try:
        migrate(engine)
        print("Schema version 2 ready")
    finally:
        engine.dispose()
