ALTER TABLE tool_calls
    ADD COLUMN assistant_message_id BIGINT UNSIGNED NULL AFTER tool_name,
    ADD COLUMN provider_tool_call_id VARCHAR(128) NULL AFTER assistant_message_id,
    ADD COLUMN step_index INTEGER UNSIGNED NOT NULL DEFAULT 0 AFTER provider_tool_call_id;

ALTER TABLE tool_calls
    ADD UNIQUE KEY uk_tool_calls_turn_provider_call (turn_id, provider_tool_call_id),
    ADD KEY idx_tool_calls_assistant_message (assistant_message_id);

ALTER TABLE tool_calls
    ADD CONSTRAINT fk_tool_calls_assistant_message
        FOREIGN KEY (assistant_message_id) REFERENCES messages(id) ON DELETE SET NULL;
