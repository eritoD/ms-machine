-- HU-49: read state of chat messages.
-- read_at is NULL while the recipient has not read the message. A message is
-- unread for a person when they did not send it and its read_at is NULL.
-- Numbered 004 on purpose: the notification outbox migration documented for
-- Matching is 003, so the two cannot collide.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('matching_api.messages') AND attname = 'read_at' AND NOT attisdropped
    ) THEN
        ALTER TABLE matching_api.messages ADD COLUMN read_at TIMESTAMPTZ;
        -- Everything written before this migration was already shown in the chat:
        -- start every counter at zero instead of flooding people with old unread messages.
        UPDATE matching_api.messages SET read_at = created_at WHERE read_at IS NULL;
    END IF;
END $$;
-- Counting the unread messages of each conversation for the chat list.
CREATE INDEX IF NOT EXISTS messages_unread ON matching_api.messages(match_id, sender_id) WHERE read_at IS NULL;
