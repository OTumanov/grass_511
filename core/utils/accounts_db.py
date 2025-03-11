# accounts_db.py

import aiosqlite
import asyncio
import aiohttp
from aiohttp import ClientProxyConnectionError, ClientConnectorError, ClientTimeout
import logging

from data.config import CLEAR_BAD_PROXIES_INTERVAL

from core.utils import logger


class AccountsDB:
    is_first_run = True  # Статическая переменная для отслеживания первого запуска

    def __init__(self, db_path):
        self.db_path = db_path
        self.connection = None
        self.cursor = None
        self.db_lock = asyncio.Lock()
        self.logger = logging.getLogger(__name__)
        self.clear_bad_proxies_interval = CLEAR_BAD_PROXIES_INTERVAL
        self.clear_task = None

    async def connect(self):
        self.connection = await aiosqlite.connect(self.db_path)
        self.cursor = await self.connection.cursor()
        await self.create_tables()
        await self.clear_bad_proxies_on_first_run()

        # Запускаем периодическую очистку, если интервал задан
        if self.clear_bad_proxies_interval > 0:
            self.clear_task = asyncio.create_task(self.periodic_clear_bad_proxies())

    async def create_tables(self):
        await self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS Accounts (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            proxies TEXT NOT NULL
        )
        ''')
        await self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS ProxyList (
            id INTEGER PRIMARY KEY,
            proxy TEXT NOT NULL
        )
        ''')
        await self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS PointStats (
            id INTEGER PRIMARY KEY,
            email TEXT NOT NULL,
            points TEXT NOT NULL
        )
        ''')
        await self.cursor.execute('''
        CREATE TABLE IF NOT EXISTS BadProxies (
            proxy TEXT PRIMARY KEY
        )
        ''')
        await self.connection.commit()

    async def add_account(self, email, new_proxy):
        async with self.db_lock:
            await self.cursor.execute("SELECT proxies FROM Accounts WHERE email=?", (email,))
            existing_proxies = await self.cursor.fetchone()

        if existing_proxies:
            existing_proxies = existing_proxies[0].split(",")
            if new_proxy not in existing_proxies:
                updated_proxies = ",".join(existing_proxies + [new_proxy])
                async with self.db_lock:
                    await self.cursor.execute("UPDATE Accounts SET proxies=? WHERE email=?", (updated_proxies, email))
                    await self.connection.commit()
        else:
            async with self.db_lock:
                await self.cursor.execute("INSERT INTO Accounts(email, proxies) VALUES(?, ?)", (email, new_proxy))
                await self.connection.commit()

    async def proxies_exist(self, proxy):
        async with self.db_lock:
            await self.cursor.execute("SELECT email, proxies FROM Accounts")
            rows = await self.cursor.fetchall()

        for row in rows:
            if len(row) > 1:
                email = row[0]
                existing_proxies = row[1].split(",")
                if proxy in existing_proxies:
                    return email
        return False

    async def update_or_create_point_stat(self, user_id, email, points):
        async with self.db_lock:
            await self.cursor.execute("SELECT * FROM PointStats WHERE id = ?", (user_id,))
            existing_user = await self.cursor.fetchone()

            if existing_user:
                await self.cursor.execute("UPDATE PointStats SET email = ?, points = ? WHERE id = ?",
                                          (email, points, user_id))
            else:
                await self.cursor.execute("INSERT INTO PointStats(id, email, points) VALUES (?, ?, ?)",
                                          (user_id, email, points))

            await self.connection.commit()

    async def get_total_points(self):
        async with self.db_lock:
            await self.cursor.execute(
                'SELECT SUM(CAST(points AS INTEGER)) '
                'FROM (SELECT email, MAX(CAST(points AS INTEGER)) as points '
                'FROM PointStats WHERE points NOT LIKE "%[^0-9]%" '
                'GROUP BY email)'
            )
            result = await self.cursor.fetchone()
            return result[0] if result else 0

    async def get_proxies_by_email(self, email):
        async with self.db_lock:
            await self.cursor.execute("SELECT proxies FROM Accounts WHERE email=?", (email,))
            row = await self.cursor.fetchone()
        return row[0].split(",") if row else []

    async def push_extra_proxies(self, proxies):
        async with self.db_lock:
            await self.cursor.executemany("INSERT INTO ProxyList(proxy) VALUES(?)", [(proxy,) for proxy in proxies])
            await self.connection.commit()

    async def delete_all_from_extra_proxies(self):
        async with self.db_lock:
            await self.cursor.execute("DELETE FROM ProxyList")
            await self.connection.commit()

    async def close_connection(self):
        if self.clear_task:
            self.clear_task.cancel()
        await self.connection.close()

    async def get_new_from_extra_proxies(self, table="ProxyList"):
        proxy = await self._fetch_and_validate_proxy(table)
        return proxy

    async def _fetch_and_validate_proxy(self, table):
        while True:
            proxy = await self._get_candidate_proxy(table)
            if not proxy:
                return None

            # Проверка, находится ли прокси в таблице BadProxies
            async with self.db_lock:
                await self.cursor.execute("SELECT proxy FROM BadProxies WHERE proxy = ?", (proxy,))
                bad_proxy = await self.cursor.fetchone()

            if bad_proxy:
                logger.info(f"Proxy {proxy} is in bad proxies list. Skipping...")
                continue

            logger.info(f"Checking proxy: {proxy}")
            is_valid = await self._is_proxy_valid(proxy)

            if is_valid:
                return proxy
            else:
                # Добавление прокси в таблицу BadProxies
                async with self.db_lock:
                    await self.cursor.execute("INSERT OR IGNORE INTO BadProxies(proxy) VALUES(?)", (proxy,))
                    await self.connection.commit()
                logger.warning(f"Proxy {proxy} failed validation. Adding to bad proxies list.")

    async def _get_candidate_proxy(self, table):
        async with self.db_lock:
            await self.cursor.execute(f"SELECT proxy FROM {table} ORDER BY RANDOM() LIMIT 1")
            result = await self.cursor.fetchone()
        return result[0] if result else None

    async def _is_proxy_valid(self, proxy):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                        "https://api.ipify.org?format=json",
                        proxy=proxy,
                        timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status != 200:
                        return False

                    content_type = response.headers.get("Content-Type", "")
                    if "application/json" not in content_type:
                        logger.warning(f"Unexpected content type from api.ipify.org: {content_type}")
                        return False

                    try:
                        data = await response.json()
                    except aiohttp.ContentTypeError:
                        logger.warning("Received non-JSON response from api.ipify.org")
                        return False

                    if "ip" not in data:
                        return False

                    return True
        except (
                aiohttp.ClientProxyConnectionError, aiohttp.ClientConnectorError, asyncio.TimeoutError, ValueError
        ) as e:
            logger.error(f"Proxy validation failed: {str(e)}")
            return False

    async def _remove_valid_proxy(self, table, proxy):
        async with self.db_lock:
            await self.cursor.execute(f"DELETE FROM {table} WHERE proxy = ?", (proxy,))
            await self.connection.commit()

    async def _remove_invalid_proxy(self, table, proxy):
        async with self.db_lock:
            await self.cursor.execute(f"DELETE FROM {table} WHERE proxy = ?", (proxy,))
            await self.connection.commit()

    async def clear_bad_proxies_on_first_run(self):
        if AccountsDB.is_first_run:
            async with self.db_lock:
                await self.cursor.execute("DELETE FROM BadProxies")
                await self.connection.commit()
            AccountsDB.is_first_run = False
            logger.info("BadProxies table cleared on first run")

    async def periodic_clear_bad_proxies(self):
        while True:
            await asyncio.sleep(self.clear_bad_proxies_interval * 60)
            async with self.db_lock:
                await self.cursor.execute("DELETE FROM BadProxies")
                await self.connection.commit()
            logger.info("BadProxies table cleared periodically")