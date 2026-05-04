CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    created_by_user_id BIGINT UNSIGNED NULL,
    name VARCHAR(255) NOT NULL,
    prompt TEXT NOT NULL,
    schedule_kind VARCHAR(32) NOT NULL,
    schedule_expr VARCHAR(255) NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    next_run_at DATETIME(6) NULL,
    last_run_at DATETIME(6) NULL,
    lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'active',
    run_count INT UNSIGNED NOT NULL DEFAULT 0,
    delivery_mode VARCHAR(32) NOT NULL DEFAULT 'origin',
    delivery_target_json JSON,
    metadata_json JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY idx_scheduled_jobs_due (lifecycle_status, next_run_at),
    KEY idx_scheduled_jobs_conversation (conversation_id, lifecycle_status, next_run_at),
    CONSTRAINT fk_scheduled_jobs_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_scheduled_jobs_created_by FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    job_id BIGINT UNSIGNED NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    scheduled_for DATETIME(6) NOT NULL,
    started_at DATETIME(6) NULL,
    finished_at DATETIME(6) NULL,
    output_summary TEXT,
    error_message TEXT,
    metadata_json JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_scheduled_job_runs_job_time (job_id, scheduled_for),
    KEY idx_scheduled_job_runs_status (status, scheduled_for),
    CONSTRAINT fk_scheduled_job_runs_job FOREIGN KEY (job_id) REFERENCES scheduled_jobs(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS message_deliveries (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    source_type VARCHAR(64) NOT NULL,
    source_id BIGINT UNSIGNED NOT NULL,
    platform VARCHAR(32) NOT NULL,
    external_chat_id VARCHAR(128) NOT NULL,
    delivery_key VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    external_message_id VARCHAR(128) NULL,
    error_message TEXT,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    sent_at DATETIME(6) NULL,
    UNIQUE KEY uk_message_deliveries_key (delivery_key),
    KEY idx_message_deliveries_source (source_type, source_id),
    KEY idx_message_deliveries_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

CREATE TABLE IF NOT EXISTS scheduler_locks (
    lock_name VARCHAR(64) PRIMARY KEY,
    owner VARCHAR(128) NOT NULL,
    expires_at DATETIME(6) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT IGNORE INTO scheduler_locks (lock_name, owner, expires_at)
VALUES ('scheduler', '', '1970-01-01 00:00:00');
