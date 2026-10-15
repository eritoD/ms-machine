-- HU-50: share a location, images and files in the chat.
-- A message is one of TEXT, IMAGE, FILE or LOCATION. Images and files are stored as an
-- https URL (this service stores no files); a location is a pair of coordinates.
-- Only text messages must have text: an image or a location may go without a caption.
-- Runs once: the guard looks for the new column, so restarts do not rescan the table.
DO $$
DECLARE
    old_check RECORD;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('matching_api.messages') AND attname = 'message_type' AND NOT attisdropped
    ) THEN
        ALTER TABLE matching_api.messages
            ADD COLUMN message_type TEXT NOT NULL DEFAULT 'TEXT'
                CHECK (message_type IN ('TEXT', 'IMAGE', 'LOCATION', 'FILE')),
            ADD COLUMN attachment_url TEXT CHECK (char_length(attachment_url) <= 2048),
            ADD COLUMN attachment_name VARCHAR(120),
            ADD COLUMN latitude DOUBLE PRECISION CHECK (latitude BETWEEN -90 AND 90),
            ADD COLUMN longitude DOUBLE PRECISION CHECK (longitude BETWEEN -180 AND 180),
            ADD COLUMN location_name VARCHAR(120);

        -- The original rule (text of 1 to 2000 characters for every message) is replaced.
        FOR old_check IN
            SELECT conname FROM pg_constraint
            WHERE conrelid = to_regclass('matching_api.messages') AND contype = 'c'
              AND pg_get_constraintdef(oid) LIKE '%btrim%'
        LOOP
            EXECUTE format('ALTER TABLE matching_api.messages DROP CONSTRAINT %I', old_check.conname);
        END LOOP;

        ALTER TABLE matching_api.messages
            ADD CONSTRAINT messages_text_length CHECK (
                char_length(text) <= 2000 AND (message_type <> 'TEXT' OR char_length(btrim(text)) >= 1)),
            -- Each type carries exactly its own data.
            ADD CONSTRAINT messages_type_shape CHECK (
                (message_type = 'TEXT' AND attachment_url IS NULL AND latitude IS NULL AND longitude IS NULL)
                OR (message_type IN ('IMAGE', 'FILE') AND attachment_url IS NOT NULL
                    AND latitude IS NULL AND longitude IS NULL)
                OR (message_type = 'LOCATION' AND attachment_url IS NULL
                    AND latitude IS NOT NULL AND longitude IS NOT NULL));
    END IF;
END $$;
