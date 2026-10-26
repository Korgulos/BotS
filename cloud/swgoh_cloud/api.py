from contextlib import asynccontextmanager
import hmac
import secrets
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import db
from .config import Settings
from .models import AccountCreate, SourceCreate, IngestBatch
from .store import ingest


class BodyLimit:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b""); size += len(chunk)
            if size > self.limit:
                return await JSONResponse({"error": "Request body too large"}, status_code=413)(scope, receive, send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        sent = False
        async def buffered():
            nonlocal sent
            if sent:
                return await receive()
            sent = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        await self.app(scope, buffered, send)


def create_app(settings: Settings, engine=None):
    engine = engine or db.engine_for(settings.database_url)
    @asynccontextmanager
    async def lifespan(app):
        migration_engine = db.engine_for(settings.migration_database_url) if settings.migration_database_url else engine
        try:
            db.migrate(migration_engine)
        finally:
            if migration_engine is not engine:
                migration_engine.dispose()
        yield
        engine.dispose()

    app = FastAPI(title="SWGoH Cloud", version="0.1.0", lifespan=lifespan,
        docs_url="/docs" if settings.docs else None, redoc_url=None,
        openapi_url="/openapi.json" if settings.docs else None)
    app.add_middleware(BodyLimit, limit=settings.max_body_bytes)
    panel = Path(__file__).parent / "panel"
    app.mount("/panel-assets", StaticFiles(directory=panel), name="panel-assets")

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(panel / "index.html", headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        })
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        # Pydantic's default errors echo supplied inputs, which might contain credentials.
        return JSONResponse({"error": "Invalid observation or request", "fields": [
            {"location": e["loc"], "type": e["type"]} for e in error.errors()]}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, error):
        return JSONResponse({"error": "Database operation unavailable"}, status_code=503)

    def require_admin(auth: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if auth is None or not hmac.compare_digest(auth.credentials.encode(), settings.admin_token.encode()):
            raise HTTPException(401, "Authentication required")

    def identity(auth: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if auth is None:
            raise HTTPException(401, "Authentication required")
        with engine.connect() as conn:
            row = conn.execute(select(db.keys).where(db.keys.c.digest == db.digest(auth.credentials),
                db.keys.c.enabled.is_(True))).mappings().first()
        if row is None:
            raise HTTPException(401, "Authentication required")
        return dict(row)

    def reader(who=Depends(identity)):
        if who["role"] not in {"read", "control"}:
            raise HTTPException(403, "Read credential required")
        return who

    def controller(who=Depends(identity)):
        if who["role"] != "control":
            raise HTTPException(403, "Za kupovinu je potreban poseban control ključ")
        return who

    def writer(who=Depends(identity)):
        if who["role"] != "ingest" or who["source_id"] is None:
            raise HTTPException(403, "Ingest credential required")
        return who

    def issue_key(conn, account_id, role, source_id=None):
        token = secrets.token_urlsafe(32)
        conn.execute(db.keys.insert().values(id=db.uid(), account_id=account_id, source_id=source_id,
            role=role, digest=db.digest(token), enabled=True, created_at=db.now()))
        return token

    @app.get("/healthz")
    def health():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.post("/v1/accounts", dependencies=[Depends(require_admin)], status_code=201)
    def account(data: AccountCreate):
        account_id = db.uid()
        try:
            with engine.begin() as conn:
                conn.execute(db.accounts.insert().values(id=account_id, alias=data.alias, created_at=db.now()))
                token = issue_key(conn, account_id, "read")
        except IntegrityError:
            raise HTTPException(409, "Account alias already exists")
        return {"account_id": account_id, "alias": data.alias, "read_token": token}

    @app.post("/v1/accounts/{account_id}/sources", dependencies=[Depends(require_admin)], status_code=201)
    def source(account_id: str, data: SourceCreate):
        source_id = db.uid()
        try:
            with engine.begin() as conn:
                if conn.execute(select(db.accounts.c.id).where(db.accounts.c.id == account_id)).first() is None:
                    raise HTTPException(404, "Account not found")
                conn.execute(db.sources.insert().values(id=source_id, account_id=account_id, name=data.name, created_at=db.now()))
                token = issue_key(conn, account_id, "ingest", source_id)
        except IntegrityError:
            raise HTTPException(409, "Source name already exists")
        return {"source_id": source_id, "name": data.name, "ingest_token": token}

    @app.delete("/v1/keys/{key_id}", dependencies=[Depends(require_admin)])
    def revoke(key_id: str):
        with engine.begin() as conn:
            result = conn.execute(db.keys.update().where(db.keys.c.id == key_id).values(enabled=False))
            if not result.rowcount:
                raise HTTPException(404, "Credential not found")
        return {"revoked": True}

    @app.post("/v1/accounts/{account_id}/control-key", dependencies=[Depends(require_admin)])
    def control_key(account_id: str):
        with engine.begin() as conn:
            if conn.execute(select(db.accounts.c.id).where(db.accounts.c.id == account_id)).first() is None:
                raise HTTPException(404, "Account not found")
            return {"control_token": issue_key(conn, account_id, "control")}

    @app.get("/v1/accounts", dependencies=[Depends(require_admin)])
    def list_accounts():
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(db.accounts)).mappings()]

    @app.get("/v1/keys", dependencies=[Depends(require_admin)])
    def list_keys():
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(db.keys.c.id, db.keys.c.account_id,
                db.keys.c.source_id, db.keys.c.role, db.keys.c.enabled, db.keys.c.created_at)).mappings()]

    @app.post("/v1/ingest")
    def receive(data: IngestBatch, who=Depends(writer)):
        try:
            with engine.begin() as conn:
                results = ingest(conn, who, data)
        except ValueError:
            raise HTTPException(409, "Conflicting event revision")
        return {"acknowledged": results}

    @app.get("/v1/events")
    def list_events(who=Depends(reader), limit: int = Query(50, ge=1, le=200),
                    offset: int = Query(0, ge=0, le=100000), service: str | None = None, operation: str | None = None):
        query = select(*(c for c in db.events.c if c.name != "payload")).where(db.events.c.account_id == who["account_id"])
        if service:
            query = query.where(db.events.c.service == service)
        if operation:
            query = query.where(db.events.c.operation == operation)
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(query.order_by(db.events.c.observed_at.desc(), db.events.c.id)
                .limit(limit).offset(offset)).mappings()]

    @app.get("/v1/events/{event_id}")
    def event_detail(event_id: str, who=Depends(reader)):
        with engine.connect() as conn:
            row = conn.execute(select(db.events).where(db.events.c.id == event_id,
                db.events.c.account_id == who["account_id"])).mappings().first()
        if row is None:
            raise HTTPException(404, "Observation not found")
        return dict(row)

    @app.get("/v1/snapshots")
    def latest(who=Depends(reader)):
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(select(db.snapshots).where(
                db.snapshots.c.account_id == who["account_id"]).order_by(db.snapshots.c.observed_at.desc()).limit(32)).mappings()]

    from .territory import install as install_territory
    install_territory(app, engine, reader)

    @app.get("/v1/battles")
    def list_battles(who=Depends(reader), limit: int = Query(50, ge=1, le=200)):
        query = select(db.battles, db.events.c.observed_at, db.events.c.session, db.events.c.source_id).join(
            db.events, db.events.c.id == db.battles.c.event_id).where(db.events.c.account_id == who["account_id"])
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(query.order_by(db.events.c.observed_at.desc()).limit(limit)).mappings()]

    def current_rows(table, topic, who, limit):
        query = select(table, db.snapshots.c.source_id, db.snapshots.c.observed_at).join(
            db.snapshots, db.snapshots.c.event_id == table.c.event_id).where(
            db.snapshots.c.account_id == who["account_id"], db.snapshots.c.topic == topic)
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(query.limit(limit)).mappings()]

    @app.get("/v1/shipments")
    def current_shipments(who=Depends(reader), limit: int = Query(200, ge=1, le=500)):
        return current_rows(db.offers, "shipments", who, limit)

    @app.get("/v1/inbox")
    def current_inbox(who=Depends(reader), limit: int = Query(100, ge=1, le=500)):
        return current_rows(db.inbox, "inbox", who, limit)

    @app.get("/v1/guild/members")
    def current_members(who=Depends(reader), limit: int = Query(100, ge=1, le=500)):
        return current_rows(db.members, "guild", who, limit)

    @app.get("/v1/capabilities")
    def capabilities(who=Depends(reader)):
        return {"mode": "inspector-observations", "standalone_game_rpc": False,
            "cloud_game_actions": True, "remote_shipments": True, "purchase_approval_required": True, "automatic_purchases": False, "crystal_spending": False,
            "requires_game_for_fresh_data": True, "direct_credit_purchases": True, "direct_nonpremium_currencies": True, "direct_session_renewal": False}

    from .purchases import install
    install(app, engine, reader, controller, writer)
    from .direct_probe import install as install_direct_probe
    install_direct_probe(app,controller)
    from .direct_account import install as install_direct_account
    install_direct_account(app,engine,controller)
    from .direct_purchases import install as install_direct_purchases
    install_direct_purchases(app,engine,controller)
    from .inventory import install as install_inventory
    install_inventory(app,engine,reader,controller)
    from .inbox_automation import install as install_inbox_automation
    install_inbox_automation(app,engine,reader,controller,writer)
    return app
