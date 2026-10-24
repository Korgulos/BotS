import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pgserver
import pytest
from swgoh_cloud import db

@pytest.fixture(scope="session")
def postgres(tmp_path_factory):
    from pgserver.postgres_server import PostgresServer
    import fasteners
    runtime=tmp_path_factory.mktemp("pg-runtime")
    PostgresServer.runtime_path=runtime
    PostgresServer._lock=fasteners.InterProcessLock(runtime/".lockfile")
    server=pgserver.get_server(tmp_path_factory.mktemp("cloud-postgres"),cleanup_mode="stop")
    yield server
    server.cleanup()

@pytest.fixture
def engine(postgres):
    engine=db.engine_for(postgres.get_uri())
    db.meta.drop_all(engine)
    db.migrate(engine)
    yield engine
    engine.dispose()
