CREATE TABLE IF NOT EXISTS procurement_members (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    user_id BIGINT,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    approval_limit_cents BIGINT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_slug, email)
);

CREATE TABLE IF NOT EXISTS suppliers (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_slug, normalized_name)
);

CREATE TABLE IF NOT EXISTS parts (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    sku TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (org_slug, normalized_name)
);

CREATE TABLE IF NOT EXISTS quote_documents (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
    filename TEXT NOT NULL,
    workspace_path TEXT NOT NULL,
    media_type TEXT,
    size_bytes BIGINT NOT NULL DEFAULT 0,
    extracted_text TEXT NOT NULL DEFAULT '',
    confidence NUMERIC NOT NULL DEFAULT 0,
    created_by_user_id BIGINT,
    created_by_email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quote_line_items (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    quote_document_id BIGINT NOT NULL REFERENCES quote_documents(id),
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
    part_id BIGINT NOT NULL REFERENCES parts(id),
    part_name TEXT NOT NULL,
    sku TEXT,
    quantity NUMERIC NOT NULL,
    unit TEXT NOT NULL DEFAULT 'unit',
    unit_price_cents BIGINT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'BRL',
    delivery_days INTEGER,
    payment_terms TEXT,
    valid_until TEXT,
    confidence NUMERIC NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purchase_requests (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    part_id BIGINT REFERENCES parts(id),
    part_name TEXT NOT NULL,
    quantity NUMERIC NOT NULL,
    unit TEXT NOT NULL DEFAULT 'unit',
    recommended_supplier_id BIGINT REFERENCES suppliers(id),
    recommended_supplier_name TEXT,
    amount_cents BIGINT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'BRL',
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending_approval',
    created_by_user_id BIGINT,
    created_by_email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS purchase_request_quotes (
    id BIGSERIAL PRIMARY KEY,
    request_id BIGINT NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
    quote_line_item_id BIGINT NOT NULL REFERENCES quote_line_items(id),
    UNIQUE (request_id, quote_line_item_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    request_id BIGINT NOT NULL REFERENCES purchase_requests(id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    actor_user_id BIGINT,
    actor_email TEXT,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS supplier_events (
    id BIGSERIAL PRIMARY KEY,
    org_slug TEXT NOT NULL,
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
    event_type TEXT NOT NULL,
    purchase_request_id BIGINT REFERENCES purchase_requests(id),
    quote_line_item_id BIGINT REFERENCES quote_line_items(id),
    quantity NUMERIC,
    amount_cents BIGINT,
    unit_price_cents BIGINT,
    delivery_days INTEGER,
    quality_score INTEGER,
    note TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_user_id BIGINT,
    created_by_email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_procurement_members_org ON procurement_members(org_slug);
CREATE INDEX IF NOT EXISTS idx_suppliers_org ON suppliers(org_slug);
CREATE INDEX IF NOT EXISTS idx_parts_org ON parts(org_slug);
CREATE INDEX IF NOT EXISTS idx_quote_documents_org_created ON quote_documents(org_slug, created_at);
CREATE INDEX IF NOT EXISTS idx_quote_line_items_org_part ON quote_line_items(org_slug, part_id);
CREATE INDEX IF NOT EXISTS idx_purchase_requests_org_status ON purchase_requests(org_slug, status);
CREATE INDEX IF NOT EXISTS idx_supplier_events_org_supplier ON supplier_events(org_slug, supplier_id);

