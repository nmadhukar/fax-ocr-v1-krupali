-- Healthcare Fax Processing System - Mismatch Monitoring
-- Version: 1.0.0
-- Description: Table for tracking field extraction mismatch metrics

CREATE TABLE IF NOT EXISTS fax_mismatch_metric (
    metric_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payer_name payer_name_enum,
    template_id UUID REFERENCES fax_template(template_id),
    field_key VARCHAR(100) NOT NULL,
    mismatch_count INTEGER DEFAULT 0,
    total_count INTEGER DEFAULT 0,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    alert_triggered BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now()
);

COMMENT ON TABLE fax_mismatch_metric IS 'Aggregated mismatch metrics for template field extraction monitoring';

CREATE INDEX idx_mismatch_metric_payer ON fax_mismatch_metric(payer_name);
CREATE INDEX idx_mismatch_metric_period ON fax_mismatch_metric(period_start, period_end);
CREATE INDEX idx_mismatch_metric_field ON fax_mismatch_metric(field_key);
