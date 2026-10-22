-- Events commit atomically with the business operation. Network retries do not
-- fire INSERT triggers again when ON CONFLICT returns an existing row.
CREATE TABLE IF NOT EXISTS matching_api.notification_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id uuid NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    body text NOT NULL,
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at timestamptz
);
CREATE INDEX IF NOT EXISTS matching_outbox_pending ON matching_api.notification_outbox(id) WHERE published_at IS NULL;

CREATE OR REPLACE FUNCTION matching_api.notify_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO matching_api.notification_outbox(user_id, kind, title, body, data)
        VALUES (NEW.recipient_id, 'solicitud', 'Nueva solicitud de conexión',
            'Un deportista quiere entrenar contigo. Revisa tus solicitudes.',
            jsonb_build_object('screen', 'matches'));
    ELSIF OLD.status IS DISTINCT FROM NEW.status AND NEW.status = 'accepted' THEN
        INSERT INTO matching_api.notification_outbox(user_id, kind, title, body, data)
        VALUES (NEW.sender_id, 'match', '¡Tienes un nuevo match!',
            'Aceptaron tu solicitud. Ya pueden coordinar un entrenamiento.',
            jsonb_build_object('screen', 'chat', 'matchId', NEW.id));
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS notification_request ON matching_api.requests;
CREATE TRIGGER notification_request AFTER INSERT OR UPDATE ON matching_api.requests
    FOR EACH ROW EXECUTE FUNCTION matching_api.notify_request();

CREATE OR REPLACE FUNCTION matching_api.notify_message() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO matching_api.notification_outbox(user_id, kind, title, body, data)
    SELECT CASE WHEN r.sender_id = NEW.sender_id THEN r.recipient_id ELSE r.sender_id END,
        'mensaje', 'Nuevo mensaje', 'Tienes un mensaje de uno de tus matches.',
        jsonb_build_object('screen', 'chat', 'matchId', NEW.match_id)
    FROM matching_api.requests r WHERE r.id = NEW.match_id;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS notification_message ON matching_api.messages;
CREATE TRIGGER notification_message AFTER INSERT ON matching_api.messages
    FOR EACH ROW EXECUTE FUNCTION matching_api.notify_message();
