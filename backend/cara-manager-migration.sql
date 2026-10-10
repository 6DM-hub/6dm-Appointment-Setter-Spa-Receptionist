BEGIN;

-- Running upgrade d1e2f3a4b5c6 -> a101_cara_manager

CREATE TABLE cara_preferences (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    tenant_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    approved_by UUID NOT NULL, 
    value JSONB NOT NULL, 
    PRIMARY KEY (id), 
    UNIQUE (tenant_id), 
    FOREIGN KEY(tenant_id) REFERENCES spa_accounts (id), 
    FOREIGN KEY(approved_by) REFERENCES users (id)
);

CREATE TABLE cara_campaigns (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    tenant_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    requested_by UUID NOT NULL, 
    request TEXT NOT NULL, 
    status VARCHAR(32) NOT NULL, 
    proposal JSONB NOT NULL, 
    proposal_hash VARCHAR(64) NOT NULL, 
    approved_hash VARCHAR(64), 
    approved_by UUID, 
    approved_at TIMESTAMP WITH TIME ZONE, 
    PRIMARY KEY (id), 
    FOREIGN KEY(tenant_id) REFERENCES spa_accounts (id), 
    FOREIGN KEY(requested_by) REFERENCES users (id), 
    FOREIGN KEY(approved_by) REFERENCES users (id)
);

CREATE TABLE cara_deliveries (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    tenant_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    campaign_id UUID NOT NULL, 
    contact_id UUID NOT NULL, 
    status VARCHAR(32) NOT NULL, 
    message TEXT NOT NULL, 
    response TEXT, 
    booking JSONB, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_cara_campaign_contact UNIQUE (campaign_id, contact_id), 
    FOREIGN KEY(tenant_id) REFERENCES spa_accounts (id), 
    FOREIGN KEY(campaign_id) REFERENCES cara_campaigns (id), 
    FOREIGN KEY(contact_id) REFERENCES contacts (id)
);

CREATE TABLE cara_packages (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    tenant_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    campaign_id UUID NOT NULL, 
    contact_id UUID NOT NULL, 
    offer JSONB NOT NULL, 
    PRIMARY KEY (id), 
    CONSTRAINT uq_cara_package_contact UNIQUE (campaign_id, contact_id), 
    FOREIGN KEY(tenant_id) REFERENCES spa_accounts (id), 
    FOREIGN KEY(campaign_id) REFERENCES cara_campaigns (id), 
    FOREIGN KEY(contact_id) REFERENCES contacts (id)
);

CREATE TABLE cara_audit (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    tenant_id UUID NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
    actor_id UUID, 
    campaign_id UUID, 
    action VARCHAR(64) NOT NULL, 
    details JSONB NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(tenant_id) REFERENCES spa_accounts (id), 
    FOREIGN KEY(actor_id) REFERENCES users (id), 
    FOREIGN KEY(campaign_id) REFERENCES cara_campaigns (id)
);

CREATE INDEX ix_cara_campaigns_tenant_id ON cara_campaigns (tenant_id);

CREATE INDEX ix_cara_deliveries_tenant_id ON cara_deliveries (tenant_id);

CREATE INDEX ix_cara_packages_tenant_id ON cara_packages (tenant_id);

CREATE INDEX ix_cara_audit_tenant_id ON cara_audit (tenant_id);

CREATE INDEX ix_cara_deliveries_campaign_id ON cara_deliveries (campaign_id);

CREATE INDEX ix_cara_deliveries_contact_id ON cara_deliveries (contact_id);

UPDATE alembic_version SET version_num='a101_cara_manager' WHERE alembic_version.version_num = 'd1e2f3a4b5c6';

COMMIT;

