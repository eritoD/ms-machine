from psycopg_pool import ConnectionPool


def create_pool(url):
    pool = ConnectionPool(url, min_size=1, max_size=10, open=False)
    pool.open(wait=True)
    return pool
