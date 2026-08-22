CREATE TABLE IF NOT EXISTS agent_trace_index_runs (
    run_id VARCHAR(191) NOT NULL,
    next_ordinal BIGINT NOT NULL,
    version BIGINT NOT NULL,
    created_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (run_id),
    CONSTRAINT ck_agent_trace_runs_next_ordinal CHECK (next_ordinal >= 0),
    CONSTRAINT ck_agent_trace_runs_version CHECK (version >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS agent_trace_index_records (
    run_id VARCHAR(191) NOT NULL,
    ordinal BIGINT NOT NULL,
    kind VARCHAR(64) NOT NULL,
    object_key VARCHAR(1024) NOT NULL,
    sha256 CHAR(64) NOT NULL,
    event_sequence BIGINT NULL,
    event_unit_id VARCHAR(191) NULL,
    event_id VARCHAR(191) NULL,
    created_at VARCHAR(64) NOT NULL,
    PRIMARY KEY (run_id, ordinal),
    UNIQUE KEY uq_agent_trace_records_event_id (event_id),
    KEY ix_agent_trace_records_run_sequence (run_id, event_sequence),
    KEY ix_agent_trace_records_run_unit (run_id, event_unit_id),
    CONSTRAINT ck_agent_trace_records_ordinal CHECK (ordinal >= 0),
    CONSTRAINT fk_agent_trace_records_run
        FOREIGN KEY (run_id) REFERENCES agent_trace_index_runs (run_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
