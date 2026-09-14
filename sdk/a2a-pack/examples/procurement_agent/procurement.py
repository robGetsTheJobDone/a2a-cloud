from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterable


ADMIN_ROLES = {"procurement_admin"}
APPROVER_ROLES = {"approver", "finance", "procurement_admin", "owner"}
REVIEWER_ROLES = APPROVER_ROLES | {"buyer"}
POSTGRES_CONNECT_ATTEMPTS = 20
POSTGRES_CONNECT_RETRY_SECONDS = 0.75
TRANSIENT_POSTGRES_CONNECT_FRAGMENTS = (
    "database system is starting up",
    "database system is shutting down",
    "the database system is in recovery mode",
    "connection refused",
    "connection reset by peer",
    "server closed the connection unexpectedly",
    "could not connect to server",
    "timeout",
    "timed out",
)


@dataclass(frozen=True)
class Actor:
    org_slug: str
    user_id: int | None
    email: str


@dataclass(frozen=True)
class ExtractedQuote:
    supplier_name: str
    part_name: str
    sku: str | None
    quantity: Decimal
    unit: str
    unit_price_cents: int
    currency: str
    delivery_days: int | None
    payment_terms: str | None
    valid_until: str | None
    confidence: float
    text: str


def normalize_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", cleaned)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def money_to_cents(raw: str) -> tuple[int, str]:
    text = raw.strip()
    currency = "BRL"
    upper = text.upper()
    if "USD" in upper or "US$" in upper:
        currency = "USD"
    elif "EUR" in upper:
        currency = "EUR"
    elif "BRL" in upper or "R$" in upper:
        currency = "BRL"

    number = re.sub(r"[^0-9,.\-]", "", text)
    if "," in number and "." in number and number.rfind(",") > number.rfind("."):
        number = number.replace(".", "").replace(",", ".")
    elif "," in number and "." not in number:
        number = number.replace(",", ".")
    try:
        amount = Decimal(number)
    except InvalidOperation as exc:
        raise ValueError(f"could not parse money value: {raw!r}") from exc
    cents = int((amount * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents, currency


def decimal_from_text(raw: str) -> Decimal:
    cleaned = re.sub(r"[^0-9,.\-]", "", raw.strip())
    if "," in cleaned and "." in cleaned and cleaned.rfind(",") > cleaned.rfind("."):
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    return Decimal(cleaned)


def cents_to_amount(cents: int, currency: str = "BRL") -> str:
    return f"{currency} {Decimal(cents) / Decimal(100):.2f}"


def extract_text_from_document(data: bytes, filename: str = "") -> str:
    if filename.lower().endswith(".pdf"):
        try:
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(data))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if text.strip():
                return text
        except Exception:
            pass
    return data.decode("utf-8", errors="ignore")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def _is_transient_postgres_connect_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(fragment in message for fragment in TRANSIENT_POSTGRES_CONNECT_FRAGMENTS)


def _label(text: str, *names: str) -> str | None:
    joined = "|".join(re.escape(name) for name in names)
    match = re.search(rf"(?im)^\s*(?:{joined})\s*[:\-]\s*(.+?)\s*$", text)
    if match:
        return match.group(1).strip()
    return None


def _int_label(text: str, *names: str) -> int | None:
    raw = _label(text, *names)
    if not raw:
        return None
    match = re.search(r"-?\d+", raw)
    return int(match.group(0)) if match else None


def parse_quote_text(
    text: str,
    *,
    filename: str,
    requested_part: str = "",
    requested_quantity: int | None = None,
) -> ExtractedQuote:
    supplier = _label(text, "Supplier", "Vendor", "Fornecedor")
    part = _label(text, "Part", "Item", "Product", "Produto", "Description")
    sku = _label(text, "SKU", "Part Number", "Codigo", "Code")
    quantity_raw = _label(text, "Quantity", "Qty", "Quantidade")
    unit = _label(text, "Unit", "UOM", "Unidade") or "unit"
    price_raw = _label(text, "Unit Price", "Price", "Preco Unitario", "Preco", "Valor Unitario")
    delivery_days = _int_label(text, "Delivery", "Delivery Days", "Prazo", "Lead Time")
    payment_terms = _label(text, "Payment Terms", "Terms", "Condicoes")
    valid_until = _label(text, "Valid Until", "Validity", "Validade")

    supplier_name = supplier or Path(filename).stem.replace("_", " ").replace("-", " ").title()
    part_name = part or requested_part
    if not part_name:
        raise ValueError("quote is missing Part/Item/Product and no requested_part was provided")
    if price_raw is None:
        raise ValueError("quote is missing Unit Price/Price")

    quantity = decimal_from_text(quantity_raw) if quantity_raw else Decimal(requested_quantity or 1)
    unit_price_cents, currency = money_to_cents(price_raw)
    present = [
        supplier is not None,
        part is not None,
        quantity_raw is not None,
        price_raw is not None,
        delivery_days is not None,
        payment_terms is not None,
        valid_until is not None,
    ]
    confidence = round(sum(1 for item in present if item) / len(present), 3)
    return ExtractedQuote(
        supplier_name=supplier_name,
        part_name=part_name,
        sku=sku,
        quantity=quantity,
        unit=unit,
        unit_price_cents=unit_price_cents,
        currency=currency,
        delivery_days=delivery_days,
        payment_terms=payment_terms,
        valid_until=valid_until,
        confidence=confidence,
        text=text,
    )


class ProcurementStore:
    def __init__(self, database_url: str | None = None) -> None:
        self.database_url = database_url or os.environ.get("PROCUREMENT_DATABASE_URL") or os.environ.get("DATABASE_URL") or "sqlite:///procurement.sqlite3"
        self.kind = "postgres" if self.database_url.startswith(("postgres://", "postgresql://")) else "sqlite"

    def connect(self) -> Any:
        if self.kind == "postgres":
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("postgres DATABASE_URL requires psycopg; install procurement_agent/requirements.txt") from exc
            attempts = _env_int("PROCUREMENT_DATABASE_CONNECT_ATTEMPTS", POSTGRES_CONNECT_ATTEMPTS)
            retry_seconds = _env_float(
                "PROCUREMENT_DATABASE_CONNECT_RETRY_SECONDS",
                POSTGRES_CONNECT_RETRY_SECONDS,
            )
            last_error: Exception | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return psycopg.connect(self.database_url)
                except Exception as exc:
                    last_error = exc
                    if attempt >= attempts or not _is_transient_postgres_connect_error(exc):
                        raise
                    time.sleep(retry_seconds)
            if last_error is not None:
                raise last_error
            raise RuntimeError("postgres connection failed")
        path = self.database_url.removeprefix("sqlite:///")
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def placeholder(self, sql: str) -> str:
        if self.kind == "postgres":
            return sql.replace("?", "%s")
        return sql

    def execute(self, conn: Any, sql: str, params: Iterable[Any] = ()) -> Any:
        return conn.execute(self.placeholder(sql), tuple(params))

    def ensure_schema(self) -> None:
        with self.connect() as conn:
            if self.kind == "postgres":
                migration = Path(__file__).parent / "db" / "migrations" / "001_procurement.sql"
                for statement in migration.read_text().split(";"):
                    if statement.strip():
                        conn.execute(statement)
                return
            self._ensure_sqlite(conn)

    def _ensure_sqlite(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS procurement_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                user_id INTEGER,
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                approval_limit_cents INTEGER,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (org_slug, email)
            );
            CREATE TABLE IF NOT EXISTS suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (org_slug, normalized_name)
            );
            CREATE TABLE IF NOT EXISTS parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                sku TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (org_slug, normalized_name)
            );
            CREATE TABLE IF NOT EXISTS quote_documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                supplier_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                workspace_path TEXT NOT NULL,
                media_type TEXT,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                extracted_text TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                created_by_user_id INTEGER,
                created_by_email TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quote_line_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                quote_document_id INTEGER NOT NULL,
                supplier_id INTEGER NOT NULL,
                part_id INTEGER NOT NULL,
                part_name TEXT NOT NULL,
                sku TEXT,
                quantity TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'unit',
                unit_price_cents INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'BRL',
                delivery_days INTEGER,
                payment_terms TEXT,
                valid_until TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS purchase_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                part_id INTEGER,
                part_name TEXT NOT NULL,
                quantity TEXT NOT NULL,
                unit TEXT NOT NULL DEFAULT 'unit',
                recommended_supplier_id INTEGER,
                recommended_supplier_name TEXT,
                amount_cents INTEGER NOT NULL,
                currency TEXT NOT NULL DEFAULT 'BRL',
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending_approval',
                created_by_user_id INTEGER,
                created_by_email TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS purchase_request_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                quote_line_item_id INTEGER NOT NULL,
                UNIQUE (request_id, quote_line_item_id)
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                request_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                actor_user_id INTEGER,
                actor_email TEXT,
                note TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supplier_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_slug TEXT NOT NULL,
                supplier_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                purchase_request_id INTEGER,
                quote_line_item_id INTEGER,
                quantity TEXT,
                amount_cents INTEGER,
                unit_price_cents INTEGER,
                delivery_days INTEGER,
                quality_score INTEGER,
                note TEXT,
                occurred_at TEXT NOT NULL,
                created_by_user_id INTEGER,
                created_by_email TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_procurement_members_org ON procurement_members(org_slug);
            CREATE INDEX IF NOT EXISTS idx_suppliers_org ON suppliers(org_slug);
            CREATE INDEX IF NOT EXISTS idx_parts_org ON parts(org_slug);
            CREATE INDEX IF NOT EXISTS idx_quote_line_items_org_part ON quote_line_items(org_slug, part_id);
            CREATE INDEX IF NOT EXISTS idx_purchase_requests_org_status ON purchase_requests(org_slug, status);
            CREATE INDEX IF NOT EXISTS idx_supplier_events_org_supplier ON supplier_events(org_slug, supplier_id);
            """
        )

    def _row(self, cursor: Any) -> dict[str, Any] | None:
        row = cursor.fetchone()
        if row is None:
            return None
        if isinstance(row, sqlite3.Row):
            return dict(row)
        names = [col[0] for col in cursor.description]
        return dict(zip(names, row))

    def _rows(self, cursor: Any) -> list[dict[str, Any]]:
        rows = cursor.fetchall()
        if not rows:
            return []
        if isinstance(rows[0], sqlite3.Row):
            return [dict(row) for row in rows]
        names = [col[0] for col in cursor.description]
        return [dict(zip(names, row)) for row in rows]

    def ensure_actor_member(self, conn: Any, actor: Actor) -> dict[str, Any]:
        existing = self._row(
            self.execute(
                conn,
                "SELECT * FROM procurement_members WHERE org_slug = ? AND email = ? AND active = TRUE",
                (actor.org_slug, actor.email),
            )
        )
        if existing:
            return existing
        count = self._row(
            self.execute(
                conn,
                "SELECT COUNT(*) AS count FROM procurement_members WHERE org_slug = ? AND active = TRUE",
                (actor.org_slug,),
            )
        )["count"]
        role = "procurement_admin" if int(count) == 0 else "requester"
        approval_limit_cents = None if role == "procurement_admin" else 0
        now = utcnow()
        row = self._row(
            self.execute(
                conn,
                """
                INSERT INTO procurement_members
                    (org_slug, user_id, email, role, approval_limit_cents, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, TRUE, ?, ?)
                RETURNING *
                """,
                (actor.org_slug, actor.user_id, actor.email, role, approval_limit_cents, now, now),
            )
        )
        return row or {}

    def set_member_role(
        self,
        conn: Any,
        *,
        actor: Actor,
        email: str,
        role: str,
        approval_limit_cents: int | None,
        user_id: int | None = None,
    ) -> dict[str, Any]:
        actor_member = self.ensure_actor_member(conn, actor)
        if actor_member["role"] not in ADMIN_ROLES:
            raise PermissionError("procurement_admin role required to manage procurement members")
        if role not in {"procurement_admin", "buyer", "requester", "approver", "finance", "viewer", "owner"}:
            raise ValueError(f"unsupported procurement role: {role}")
        now = utcnow()
        row = self._row(
            self.execute(
                conn,
                """
                INSERT INTO procurement_members
                    (org_slug, user_id, email, role, approval_limit_cents, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, TRUE, ?, ?)
                ON CONFLICT (org_slug, email) DO UPDATE SET
                    user_id = COALESCE(EXCLUDED.user_id, procurement_members.user_id),
                    role = EXCLUDED.role,
                    approval_limit_cents = EXCLUDED.approval_limit_cents,
                    active = TRUE,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (actor.org_slug, user_id, email.lower(), role, approval_limit_cents, now, now),
            )
        )
        return row or {}

    def upsert_supplier(self, conn: Any, org_slug: str, name: str) -> int:
        normalized = normalize_name(name)
        row = self._row(
            self.execute(
                conn,
                """
                INSERT INTO suppliers (org_slug, name, normalized_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (org_slug, normalized_name) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                (org_slug, name, normalized, utcnow()),
            )
        )
        return int(row["id"])

    def upsert_part(self, conn: Any, org_slug: str, name: str, sku: str | None) -> int:
        normalized = normalize_name(sku or name)
        row = self._row(
            self.execute(
                conn,
                """
                INSERT INTO parts (org_slug, name, normalized_name, sku, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (org_slug, normalized_name) DO UPDATE SET
                    name = EXCLUDED.name,
                    sku = COALESCE(EXCLUDED.sku, parts.sku)
                RETURNING id
                """,
                (org_slug, name, normalized, sku, utcnow()),
            )
        )
        return int(row["id"])

    def insert_quote(
        self,
        conn: Any,
        *,
        actor: Actor,
        filename: str,
        workspace_path: str,
        media_type: str | None,
        size_bytes: int,
        quote: ExtractedQuote,
    ) -> dict[str, Any]:
        supplier_id = self.upsert_supplier(conn, actor.org_slug, quote.supplier_name)
        part_id = self.upsert_part(conn, actor.org_slug, quote.part_name, quote.sku)
        now = utcnow()
        document = self._row(
            self.execute(
                conn,
                """
                INSERT INTO quote_documents
                    (org_slug, supplier_id, filename, workspace_path, media_type, size_bytes,
                     extracted_text, confidence, created_by_user_id, created_by_email, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    actor.org_slug,
                    supplier_id,
                    filename,
                    workspace_path,
                    media_type,
                    size_bytes,
                    quote.text,
                    quote.confidence,
                    actor.user_id,
                    actor.email,
                    now,
                ),
            )
        )
        line = self._row(
            self.execute(
                conn,
                """
                INSERT INTO quote_line_items
                    (org_slug, quote_document_id, supplier_id, part_id, part_name, sku, quantity,
                     unit, unit_price_cents, currency, delivery_days, payment_terms, valid_until,
                     confidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    actor.org_slug,
                    document["id"],
                    supplier_id,
                    part_id,
                    quote.part_name,
                    quote.sku,
                    str(quote.quantity),
                    quote.unit,
                    quote.unit_price_cents,
                    quote.currency,
                    quote.delivery_days,
                    quote.payment_terms,
                    quote.valid_until,
                    quote.confidence,
                    now,
                ),
            )
        )
        return {
            "document": document,
            "line_item": self.enrich_quote_line(conn, int(line["id"])),
        }

    def enrich_quote_line(self, conn: Any, line_id: int) -> dict[str, Any]:
        row = self._row(
            self.execute(
                conn,
                """
                SELECT qli.*, suppliers.name AS supplier_name
                FROM quote_line_items qli
                JOIN suppliers ON suppliers.id = qli.supplier_id
                WHERE qli.id = ?
                """,
                (line_id,),
            )
        )
        return self.format_quote_line(row or {})

    def format_quote_line(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        if "unit_price_cents" in out:
            out["unit_price"] = cents_to_amount(int(out["unit_price_cents"]), out.get("currency") or "BRL")
        return out

    def find_quote_lines(self, conn: Any, *, org_slug: str, part_query: str, limit: int = 25) -> list[dict[str, Any]]:
        needle = f"%{part_query.lower()}%"
        rows = self._rows(
            self.execute(
                conn,
                """
                SELECT qli.*, suppliers.name AS supplier_name
                FROM quote_line_items qli
                JOIN suppliers ON suppliers.id = qli.supplier_id
                WHERE qli.org_slug = ?
                  AND (lower(qli.part_name) LIKE ? OR lower(COALESCE(qli.sku, '')) LIKE ?)
                ORDER BY qli.created_at DESC, qli.id DESC
                LIMIT ?
                """,
                (org_slug, needle, needle, limit),
            )
        )
        return [self.format_quote_line(row) for row in rows]

    def compare_quotes(self, conn: Any, *, org_slug: str, part_query: str, quantity: Decimal | None = None) -> dict[str, Any]:
        lines = self.find_quote_lines(conn, org_slug=org_slug, part_query=part_query)
        if not lines:
            return {
                "part_query": part_query,
                "quotes": [],
                "recommendation": None,
                "message": "No historical quotes found for this part.",
            }
        scored: list[dict[str, Any]] = []
        for line in lines:
            delivery_days = int(line["delivery_days"] or 14)
            price_cents = int(line["unit_price_cents"])
            delivery_penalty = max(0, delivery_days - 7) * 10
            score = price_cents + delivery_penalty
            total_cents = int((Decimal(price_cents) * (quantity or Decimal(str(line["quantity"])))).quantize(Decimal("1")))
            scored.append({**line, "score": score, "quoted_total": cents_to_amount(total_cents, line["currency"])})
        scored.sort(key=lambda item: (item["score"], item["unit_price_cents"], item.get("delivery_days") or 999))
        prices = [int(item["unit_price_cents"]) for item in scored]
        recommendation = scored[0]
        return {
            "part_query": part_query,
            "quote_count": len(scored),
            "historical_range": {
                "min": cents_to_amount(min(prices), recommendation["currency"]),
                "max": cents_to_amount(max(prices), recommendation["currency"]),
            },
            "quotes": scored,
            "recommendation": {
                "quote_line_item_id": recommendation["id"],
                "supplier": recommendation["supplier_name"],
                "unit_price": recommendation["unit_price"],
                "delivery_days": recommendation.get("delivery_days"),
                "reason": "Lowest effective cost after delivery penalty.",
            },
        }

    def create_purchase_request(
        self,
        conn: Any,
        *,
        actor: Actor,
        quote_line_item_id: int,
        quantity: Decimal,
        reason: str,
    ) -> dict[str, Any]:
        self.ensure_actor_member(conn, actor)
        quote = self.enrich_quote_line(conn, quote_line_item_id)
        if not quote or quote.get("org_slug") != actor.org_slug:
            raise ValueError("quote line item not found in this organization")
        amount_cents = int((Decimal(int(quote["unit_price_cents"])) * quantity).quantize(Decimal("1")))
        now = utcnow()
        request = self._row(
            self.execute(
                conn,
                """
                INSERT INTO purchase_requests
                    (org_slug, part_id, part_name, quantity, unit, recommended_supplier_id,
                     recommended_supplier_name, amount_cents, currency, reason, status,
                     created_by_user_id, created_by_email, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending_approval', ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    actor.org_slug,
                    quote["part_id"],
                    quote["part_name"],
                    str(quantity),
                    quote["unit"],
                    quote["supplier_id"],
                    quote["supplier_name"],
                    amount_cents,
                    quote["currency"],
                    reason,
                    actor.user_id,
                    actor.email,
                    now,
                    now,
                ),
            )
        )
        self.execute(
            conn,
            "INSERT INTO purchase_request_quotes (request_id, quote_line_item_id) VALUES (?, ?)",
            (request["id"], quote_line_item_id),
        )
        return self.format_purchase_request(request)

    def format_purchase_request(self, row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        if "amount_cents" in out:
            out["amount"] = cents_to_amount(int(out["amount_cents"]), out.get("currency") or "BRL")
        return out

    def decide_purchase_request(
        self,
        conn: Any,
        *,
        actor: Actor,
        request_id: int,
        decision: str,
        note: str,
    ) -> dict[str, Any]:
        member = self.ensure_actor_member(conn, actor)
        request = self._row(
            self.execute(
                conn,
                "SELECT * FROM purchase_requests WHERE id = ? AND org_slug = ?",
                (request_id, actor.org_slug),
            )
        )
        if request is None:
            raise ValueError("purchase request not found in this organization")
        decision = decision.lower().strip()
        if decision not in {"approve", "reject", "request_more_quotes"}:
            raise ValueError("decision must be approve, reject, or request_more_quotes")
        if decision == "approve":
            if member["role"] not in APPROVER_ROLES:
                raise PermissionError("approver, finance, owner, or procurement_admin role required")
            if request.get("created_by_user_id") is not None and actor.user_id == request.get("created_by_user_id"):
                raise PermissionError("request creator cannot approve their own purchase request")
            limit = member.get("approval_limit_cents")
            if limit is not None and int(request["amount_cents"]) > int(limit):
                raise PermissionError("approval limit is below purchase request amount")
            status = "approved"
        elif decision == "reject":
            if member["role"] not in REVIEWER_ROLES:
                raise PermissionError("buyer or approver role required to reject")
            status = "rejected"
        else:
            if member["role"] not in REVIEWER_ROLES:
                raise PermissionError("buyer or approver role required to request more quotes")
            status = "more_quotes_requested"

        now = utcnow()
        approval = self._row(
            self.execute(
                conn,
                """
                INSERT INTO approvals
                    (org_slug, request_id, decision, actor_user_id, actor_email, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (actor.org_slug, request_id, decision, actor.user_id, actor.email, note, now),
            )
        )
        updated = self._row(
            self.execute(
                conn,
                "UPDATE purchase_requests SET status = ?, updated_at = ? WHERE id = ? RETURNING *",
                (status, now, request_id),
            )
        )
        return {
            "request": self.format_purchase_request(updated or {}),
            "approval": approval,
        }

    def record_supplier_event(
        self,
        conn: Any,
        *,
        actor: Actor,
        supplier_name: str,
        event_type: str,
        purchase_request_id: int | None,
        quote_line_item_id: int | None,
        quantity: Decimal | None,
        amount_cents: int | None,
        delivery_days: int | None,
        quality_score: int | None,
        note: str,
    ) -> dict[str, Any]:
        self.ensure_actor_member(conn, actor)
        supplier_id = self.upsert_supplier(conn, actor.org_slug, supplier_name)
        unit_price_cents = None
        if amount_cents is not None and quantity is not None and quantity != 0:
            unit_price_cents = int((Decimal(amount_cents) / quantity).quantize(Decimal("1")))
        row = self._row(
            self.execute(
                conn,
                """
                INSERT INTO supplier_events
                    (org_slug, supplier_id, event_type, purchase_request_id, quote_line_item_id,
                     quantity, amount_cents, unit_price_cents, delivery_days, quality_score,
                     note, occurred_at, created_by_user_id, created_by_email, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    actor.org_slug,
                    supplier_id,
                    event_type,
                    purchase_request_id,
                    quote_line_item_id,
                    str(quantity) if quantity is not None else None,
                    amount_cents,
                    unit_price_cents,
                    delivery_days,
                    quality_score,
                    note,
                    utcnow(),
                    actor.user_id,
                    actor.email,
                    utcnow(),
                ),
            )
        )
        return row or {}

    def supplier_scorecard(self, conn: Any, *, org_slug: str, supplier_name: str) -> dict[str, Any]:
        supplier = self._row(
            self.execute(
                conn,
                "SELECT * FROM suppliers WHERE org_slug = ? AND normalized_name = ?",
                (org_slug, normalize_name(supplier_name)),
            )
        )
        if supplier is None:
            return {"supplier": supplier_name, "found": False}
        quote_rows = self._rows(
            self.execute(
                conn,
                "SELECT unit_price_cents FROM quote_line_items WHERE org_slug = ? AND supplier_id = ?",
                (org_slug, supplier["id"]),
            )
        )
        market = self._row(
            self.execute(
                conn,
                "SELECT AVG(unit_price_cents) AS avg_price FROM quote_line_items WHERE org_slug = ?",
                (org_slug,),
            )
        )
        supplier_avg = sum(int(row["unit_price_cents"]) for row in quote_rows) / max(1, len(quote_rows))
        market_avg = float(market["avg_price"] or supplier_avg or 1)
        price_score = max(0, min(100, round(100 - ((supplier_avg - market_avg) / market_avg * 100), 1)))
        events = self._rows(
            self.execute(
                conn,
                "SELECT * FROM supplier_events WHERE org_slug = ? AND supplier_id = ?",
                (org_slug, supplier["id"]),
            )
        )
        delivery_values = [int(row["delivery_days"]) for row in events if row.get("delivery_days") is not None]
        quality_values = [int(row["quality_score"]) for row in events if row.get("quality_score") is not None]
        avg_delivery = sum(delivery_values) / len(delivery_values) if delivery_values else None
        avg_quality = sum(quality_values) / len(quality_values) if quality_values else None
        delivery_score = 80 if avg_delivery is None else max(0, min(100, round(100 - max(0, avg_delivery - 7) * 4, 1)))
        quality_score = 80 if avg_quality is None else max(0, min(100, round(avg_quality, 1)))
        overall = round(price_score * 0.45 + delivery_score * 0.30 + quality_score * 0.25, 1)
        return {
            "supplier": supplier["name"],
            "found": True,
            "quotes_submitted": len(quote_rows),
            "average_delivery_days": avg_delivery,
            "price_score": price_score,
            "delivery_score": delivery_score,
            "quality_score": quality_score,
            "overall_score": overall,
        }

    def executive_dashboard(self, conn: Any, *, org_slug: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        purchases = self._row(
            self.execute(
                conn,
                """
                SELECT COALESCE(SUM(amount_cents), 0) AS total
                FROM supplier_events
                WHERE org_slug = ? AND event_type = 'purchase' AND occurred_at >= ?
                """,
                (org_slug, month_start),
            )
        )
        suppliers_reviewed = self._row(
            self.execute(
                conn,
                """
                SELECT COUNT(DISTINCT supplier_id) AS count
                FROM quote_line_items
                WHERE org_slug = ? AND created_at >= ?
                """,
                (org_slug, month_start),
            )
        )
        late = self._row(
            self.execute(
                conn,
                """
                SELECT COUNT(*) AS count
                FROM supplier_events
                WHERE org_slug = ? AND event_type = 'late_delivery' AND occurred_at >= ?
                """,
                (org_slug, month_start),
            )
        )
        pending = self._row(
            self.execute(
                conn,
                "SELECT COUNT(*) AS count FROM purchase_requests WHERE org_slug = ? AND status = 'pending_approval'",
                (org_slug,),
            )
        )
        return {
            "purchases_this_month": cents_to_amount(int(purchases["total"] or 0)),
            "suppliers_reviewed": int(suppliers_reviewed["count"] or 0),
            "late_deliveries": int(late["count"] or 0),
            "pending_approvals": int(pending["count"] or 0),
        }
