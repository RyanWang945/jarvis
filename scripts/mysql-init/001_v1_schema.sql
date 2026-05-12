-- Jarvis V1 Conversation Runtime Schema
-- MySQL 8.0, InnoDB, utf8mb4

SET NAMES utf8mb4;

DROP DATABASE IF EXISTS jarvis;
CREATE DATABASE jarvis CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE jarvis;

-- ------------------------------------------------------------------
-- users
-- ------------------------------------------------------------------
CREATE TABLE users (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    external_user_id VARCHAR(128) NOT NULL,
    display_name VARCHAR(255),
    avatar_url VARCHAR(1024),
    metadata JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_users_platform_external (platform, external_user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------------
-- conversations
-- ------------------------------------------------------------------
CREATE TABLE conversations (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    platform VARCHAR(32) NOT NULL,
    external_chat_id VARCHAR(128) NOT NULL,
    chat_type VARCHAR(32) NOT NULL,
    title VARCHAR(255),
    owner_user_id BIGINT UNSIGNED NULL,
    created_by_user_id BIGINT UNSIGNED NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'active',
    clear_generation INT UNSIGNED NOT NULL DEFAULT 0,
    cleared_from_conversation_id BIGINT UNSIGNED NULL,
    metadata JSON,
    last_message_at DATETIME(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_conversations_generation (platform, external_chat_id, clear_generation),
    KEY idx_conversations_platform_chat (platform, external_chat_id),
    KEY idx_conversations_status_last_message (status, last_message_at),
    CONSTRAINT fk_conversations_owner_user FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_conversations_created_by FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------------
-- messages
-- turn_id FK added later to break circular dependency with turns.
-- ------------------------------------------------------------------
CREATE TABLE messages (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    turn_id BIGINT UNSIGNED NULL,
    sender_type VARCHAR(32) NOT NULL,
    user_id BIGINT UNSIGNED NULL,
    role VARCHAR(32) NOT NULL,
    content TEXT,
    content_type VARCHAR(32) NOT NULL DEFAULT 'text',
    external_message_id VARCHAR(128),
    reply_to_message_id BIGINT UNSIGNED NULL,
    raw_payload JSON,
    token_count INTEGER,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_messages_external (conversation_id, external_message_id),
    KEY idx_messages_conversation_created (conversation_id, created_at),
    KEY idx_messages_turn_created (turn_id, created_at),
    KEY idx_messages_user_created (user_id, created_at),
    CONSTRAINT fk_messages_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_messages_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL,
    CONSTRAINT fk_messages_reply_to FOREIGN KEY (reply_to_message_id) REFERENCES messages(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------------
-- turns
-- ------------------------------------------------------------------
CREATE TABLE turns (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    conversation_id BIGINT UNSIGNED NOT NULL,
    trigger_message_id BIGINT UNSIGNED NULL,
    trigger_type VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    turn_type VARCHAR(32) NOT NULL DEFAULT 'chat',
    started_by_user_id BIGINT UNSIGNED NULL,
    started_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    completed_at DATETIME(6),
    error_message TEXT,
    metadata JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    KEY idx_turns_conversation_created (conversation_id, created_at),
    KEY idx_turns_status_updated (status, updated_at),
    KEY idx_turns_trigger_message (trigger_message_id),
    CONSTRAINT fk_turns_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_turns_trigger_message FOREIGN KEY (trigger_message_id) REFERENCES messages(id) ON DELETE SET NULL,
    CONSTRAINT fk_turns_started_by FOREIGN KEY (started_by_user_id) REFERENCES users(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------------
-- tool_calls
-- ------------------------------------------------------------------
CREATE TABLE tool_calls (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    turn_id BIGINT UNSIGNED NOT NULL,
    tool_name VARCHAR(128) NOT NULL,
    assistant_message_id BIGINT UNSIGNED NULL,
    provider_tool_call_id VARCHAR(128) NULL,
    step_index INTEGER UNSIGNED NOT NULL DEFAULT 0,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    input JSON,
    output JSON,
    error_message TEXT,
    started_at DATETIME(6),
    finished_at DATETIME(6),
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_tool_calls_turn_provider_call (turn_id, provider_tool_call_id),
    KEY idx_tool_calls_turn_created (turn_id, created_at),
    KEY idx_tool_calls_assistant_message (assistant_message_id),
    KEY idx_tool_calls_status_created (status, created_at),
    KEY idx_tool_calls_tool_name_created (tool_name, created_at),
    CONSTRAINT fk_tool_calls_turn FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE CASCADE,
    CONSTRAINT fk_tool_calls_assistant_message FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------------
-- artifacts
-- ------------------------------------------------------------------
CREATE TABLE artifacts (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    artifact_id VARCHAR(255) NOT NULL,
    conversation_id BIGINT UNSIGNED NOT NULL,
    turn_id BIGINT UNSIGNED NULL,
    tool_call_id VARCHAR(128) NULL,
    source_tool VARCHAR(128) NOT NULL DEFAULT '',
    kind VARCHAR(32) NOT NULL,
    path TEXT,
    mime_type VARCHAR(255),
    filename VARCHAR(255),
    size_bytes BIGINT UNSIGNED NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'available',
    metadata JSON,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_artifacts_artifact_id (artifact_id),
    KEY idx_artifacts_conversation_created (conversation_id, created_at),
    KEY idx_artifacts_conversation_turn (conversation_id, turn_id),
    KEY idx_artifacts_source_created (source_tool, created_at),
    KEY idx_artifacts_status_updated (status, updated_at),
    CONSTRAINT fk_artifacts_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_artifacts_turn FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------------
-- delivery_records
-- ------------------------------------------------------------------
CREATE TABLE delivery_records (
    id BIGINT UNSIGNED PRIMARY KEY AUTO_INCREMENT,
    delivery_id VARCHAR(128) NOT NULL,
    artifact_id VARCHAR(255) NOT NULL,
    conversation_id BIGINT UNSIGNED NULL,
    turn_id BIGINT UNSIGNED NULL,
    channel VARCHAR(32) NOT NULL,
    external_chat_id VARCHAR(128) NOT NULL,
    purpose VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    upload_key VARCHAR(512),
    external_message_id VARCHAR(128),
    error_message TEXT,
    attempt_count INT UNSIGNED NOT NULL DEFAULT 1,
    created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6),
    UNIQUE KEY uk_delivery_records_delivery_id (delivery_id),
    KEY idx_delivery_records_artifact (artifact_id),
    KEY idx_delivery_records_dedupe (channel, external_chat_id, artifact_id, purpose, status),
    KEY idx_delivery_records_conversation_created (conversation_id, created_at),
    CONSTRAINT fk_delivery_records_conversation FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE SET NULL,
    CONSTRAINT fk_delivery_records_turn FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

-- ------------------------------------------------------------------
-- Resolve circular FK: messages -> turns
-- ------------------------------------------------------------------
ALTER TABLE messages
    ADD CONSTRAINT fk_messages_turn FOREIGN KEY (turn_id) REFERENCES turns(id) ON DELETE SET NULL;

-- ------------------------------------------------------------------
-- Resolve self FK: conversations -> conversations (clear lineage)
-- ------------------------------------------------------------------
ALTER TABLE conversations
    ADD CONSTRAINT fk_conversations_cleared_from FOREIGN KEY (cleared_from_conversation_id) REFERENCES conversations(id) ON DELETE SET NULL;
