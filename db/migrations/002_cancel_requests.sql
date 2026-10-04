-- Keep cancelled requests for safe retries, while allowing a fresh request ID.
ALTER TABLE matching_api.requests DROP CONSTRAINT IF EXISTS requests_status_check;
ALTER TABLE matching_api.requests ADD CONSTRAINT requests_status_check
    CHECK (status IN ('pending', 'accepted', 'rejected', 'cancelled'));
ALTER TABLE matching_api.requests DROP CONSTRAINT IF EXISTS requests_pair_low_pair_high_key;
CREATE UNIQUE INDEX IF NOT EXISTS requests_active_pair
    ON matching_api.requests(pair_low, pair_high) WHERE status <> 'cancelled';
