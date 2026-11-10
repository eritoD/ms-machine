-- HU-64: reply to a specific message of the same chat.
-- reply_to_id points at another message of the SAME conversation: the foreign key is (match_id, reply_to_id), so
-- the database itself refuses a reply to a message of another chat. is_reply is written when the message is saved. If
-- the quoted message is ever deleted, the reply stays (reply_to_id becomes NULL) and is_reply stays true, so the app
-- can say "message not available" without ever showing what it said. No endpoint deletes a single message today;
-- this keeps the rule true if one appears. There is deliberately no trigger: it would break bulk deletes.
-- Idempotent: every step checks before it changes anything.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('matching_api.messages') AND attname = 'reply_to_id' AND NOT attisdropped
    ) THEN
        ALTER TABLE matching_api.messages
            ADD COLUMN reply_to_id BIGINT,
            ADD COLUMN is_reply BOOLEAN NOT NULL DEFAULT FALSE;
    END IF;
END $$;

-- A foreign key needs a unique target: (match_id, id) is unique because id alone already is.
CREATE UNIQUE INDEX IF NOT EXISTS messages_match_id_id ON matching_api.messages(match_id, id);
CREATE INDEX IF NOT EXISTS messages_reply_to ON matching_api.messages(reply_to_id) WHERE reply_to_id IS NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'messages_reply_same_chat'
                   AND conrelid = to_regclass('matching_api.messages')) THEN
        ALTER TABLE matching_api.messages
            ADD CONSTRAINT messages_reply_same_chat FOREIGN KEY (match_id, reply_to_id)
                REFERENCES matching_api.messages(match_id, id) ON DELETE SET NULL (reply_to_id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'messages_reply_not_itself'
                   AND conrelid = to_regclass('matching_api.messages')) THEN
        ALTER TABLE matching_api.messages
            ADD CONSTRAINT messages_reply_not_itself CHECK (reply_to_id IS NULL OR reply_to_id <> id);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'messages_reply_flag'
                   AND conrelid = to_regclass('matching_api.messages')) THEN
        ALTER TABLE matching_api.messages
            ADD CONSTRAINT messages_reply_flag CHECK (reply_to_id IS NULL OR is_reply);
    END IF;
END $$;
