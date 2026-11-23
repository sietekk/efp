-- 0014_identity_indexes: the foreign-key columns of 0013's two operator tables are indexed (a test
-- requires every foreign key to lead some index). A new file, because 0013 was already applied.

CREATE INDEX idx_identity_label_queue_row ON identity_label_queue(row_id);
CREATE INDEX idx_identity_audit_sample_decision ON identity_audit_sample(decision_id);
