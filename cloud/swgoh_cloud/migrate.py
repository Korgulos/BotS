from .config import Settings
from .db import engine_for, migrate

if __name__ == "__main__":
    engine = engine_for(Settings.from_env().database_url)
    try:
        migrate(engine)
        print("Schema version 1 ready")
    finally:
        engine.dispose()
