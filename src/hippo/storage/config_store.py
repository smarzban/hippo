"""Config-table persistence (SP3): the key/value overlay the `Config` resolver
reads from, the atomic first-run setup claim, and the cheap count helpers used by
the setup wizard and /settings/status. `SETUP_COMPLETE_KEY` is a class attribute
so it stays reachable as `Storage.SETUP_COMPLETE_KEY`."""


class _ConfigMixin:
    # -- config store (SP3) --------------------------------------------------

    SETUP_COMPLETE_KEY = "setup_complete"

    def get_config(self, key: str) -> str | None:
        with self._lock:
            row = self.con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_config(self, key: str, value: str) -> None:
        with self._lock, self.con:
            self.con.execute(
                "INSERT INTO config(key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def is_setup_complete(self) -> bool:
        return self.get_config(self.SETUP_COMPLETE_KEY) == "1"

    def mark_setup_complete(self) -> None:
        self.set_config(self.SETUP_COMPLETE_KEY, "1")

    def claim_setup(self) -> bool:
        """Atomically claim the first-run setup. Sets setup_complete='1' ONLY if
        it is not already set, returning True iff THIS call did the claiming.
        Concurrent /setup requests that race past is_setup_complete() converge
        here so exactly one creates the owner; the loser gets a 409."""
        with self._lock, self.con:
            cur = self.con.execute(
                "INSERT INTO config(key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO NOTHING",
                (self.SETUP_COMPLETE_KEY,),
            )
            return cur.rowcount > 0

    def document_count(self) -> int:
        with self._lock:
            return self.con.execute("SELECT count(*) FROM documents").fetchone()[0]

    def folder_count(self) -> int:
        with self._lock:
            return self.con.execute("SELECT count(*) FROM folders").fetchone()[0]
