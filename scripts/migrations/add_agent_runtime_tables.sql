CREATE TABLE IF NOT EXISTS agent_runtime_runs (
    root_id VARCHAR(191) NOT NULL,
    status VARCHAR(64) NOT NULL,
    metadata JSON NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    started_at DOUBLE NULL,
    finished_at DOUBLE NULL,
    error TEXT NULL,
    version INT NOT NULL,
    PRIMARY KEY (root_id),
    CONSTRAINT ck_agent_runtime_runs_version CHECK (version >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_runtime_agents (
    root_id VARCHAR(191) NOT NULL,
    agent_id VARCHAR(191) NOT NULL,
    parent_id VARCHAR(191) NULL,
    depth INT NOT NULL,
    status VARCHAR(64) NOT NULL,
    metadata JSON NOT NULL,
    created_at DOUBLE NOT NULL,
    updated_at DOUBLE NOT NULL,
    started_at DOUBLE NULL,
    finished_at DOUBLE NULL,
    error TEXT NULL,
    version INT NOT NULL,
    PRIMARY KEY (root_id, agent_id),
    KEY ix_agent_runtime_agents_tree (root_id, depth, created_at, agent_id),
    CONSTRAINT ck_agent_runtime_agents_depth CHECK (depth >= 0),
    CONSTRAINT ck_agent_runtime_agents_version CHECK (version >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_runtime_events (
    root_id VARCHAR(191) NOT NULL,
    sequence INT NOT NULL,
    event_id VARCHAR(191) NOT NULL,
    agent_id VARCHAR(191) NULL,
    event_type VARCHAR(191) NOT NULL,
    payload JSON NOT NULL,
    created_at DOUBLE NOT NULL,
    PRIMARY KEY (root_id, sequence),
    UNIQUE KEY uq_agent_runtime_events_root_event (root_id, event_id),
    KEY ix_agent_runtime_events_agent (root_id, agent_id, sequence),
    CONSTRAINT ck_agent_runtime_events_sequence CHECK (sequence >= 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
