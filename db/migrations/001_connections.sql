-- Separate namespace: legacy demo requests/messages are not real user consent.
CREATE SCHEMA IF NOT EXISTS matching_api;
CREATE TABLE IF NOT EXISTS matching_api.requests (
    id UUID PRIMARY KEY,
    sender_id UUID NOT NULL,
    recipient_id UUID NOT NULL,
    pair_low UUID GENERATED ALWAYS AS (LEAST(sender_id, recipient_id)) STORED,
    pair_high UUID GENERATED ALWAYS AS (GREATEST(sender_id, recipient_id)) STORED,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMPTZ,
    CHECK (sender_id <> recipient_id),
    CHECK ((status = 'pending') = (decided_at IS NULL)),
    UNIQUE(pair_low, pair_high)
);
CREATE INDEX IF NOT EXISTS requests_sender ON matching_api.requests(sender_id, created_at DESC);
CREATE INDEX IF NOT EXISTS requests_recipient ON matching_api.requests(recipient_id, created_at DESC);
CREATE TABLE IF NOT EXISTS matching_api.messages (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    match_id UUID NOT NULL REFERENCES matching_api.requests(id),
    sender_id UUID NOT NULL,
    client_message_id UUID NOT NULL,
    text TEXT NOT NULL CHECK (char_length(btrim(text)) BETWEEN 1 AND 2000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(match_id, sender_id, client_message_id)
);
CREATE INDEX IF NOT EXISTS messages_conversation ON matching_api.messages(match_id, id DESC);
