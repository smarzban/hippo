"""Folder-tree persistence: the adjacency table CRUD, subtree walks, and the
cascade delete (which reuses `_delete_chunks` from the documents mixin via the
Storage facade). Child folders inherit the parent's tier; roots can't move/delete."""

import sqlite3

from ..roles import readable_min_roles
from ._common import Folder


class _FoldersMixin:
    # -- folders -----------------------------------------------------------

    def get_folder(self, folder_id: int) -> Folder | None:
        """Fetch one folder (no role filter — callers gate on the returned
        min_role/origin). doc_count is the folder's own documents."""
        with self._lock:
            row = self.con.execute(
                """SELECT f.id, f.parent_id, f.name, f.min_role, f.origin, f.location,
                          (SELECT count(*) FROM documents d WHERE d.folder_id = f.id)
                   FROM folders f WHERE f.id=?""",
                (folder_id,),
            ).fetchone()
        return Folder(*row) if row else None

    def list_folders(self, *, role: str) -> list[Folder]:
        """Every folder the caller may read, ordered for tree rendering (roots
        first, then by name). Filtered by rank on the folder's own tier."""
        allowed = readable_min_roles(role)
        ph = ",".join("?" * len(allowed))
        with self._lock:
            rows = self.con.execute(
                f"""SELECT f.id, f.parent_id, f.name, f.min_role, f.origin, f.location,
                           (SELECT count(*) FROM documents d WHERE d.folder_id = f.id)
                    FROM folders f WHERE f.min_role IN ({ph})
                    ORDER BY (f.parent_id IS NOT NULL), f.parent_id, f.name""",
                allowed,
            ).fetchall()
        return [Folder(*r) for r in rows]

    def create_folder(self, *, parent_id: int, name: str,
                      origin: str = "manual", location: str | None = None) -> int:
        """Create a child folder inheriting the parent's tier. parent_id is
        required (the three roots are seeded, never created here). Raises
        ValueError on a missing parent or a duplicate sibling name."""
        name = name.strip()
        if not name:
            raise ValueError("folder name cannot be empty")
        with self._lock, self.con:
            prow = self.con.execute(
                "SELECT min_role FROM folders WHERE id=?", (parent_id,)).fetchone()
            if prow is None:
                raise ValueError(f"no folder with id {parent_id}")
            try:
                cur = self.con.execute(
                    "INSERT INTO folders(parent_id, name, min_role, origin, location) "
                    "VALUES (?,?,?,?,?)",
                    (parent_id, name, prow[0], origin, location),
                )
            except sqlite3.IntegrityError as e:
                # Either the (parent_id, name) sibling-uniqueness or the non-null
                # location-uniqueness index fired.
                if location is not None and self.con.execute(
                    "SELECT 1 FROM folders WHERE location=?", (location,)).fetchone():
                    raise ValueError(f"location {location!r} is already mounted") from e
                raise ValueError(f"a folder named {name!r} already exists here") from e
            return cur.lastrowid

    def folder_by_location(self, location: str) -> int | None:
        with self._lock:
            row = self.con.execute(
                "SELECT id FROM folders WHERE location=?", (location,)).fetchone()
        return row[0] if row else None

    def folder_path(self, folder_id: int) -> str:
        """The slash-joined ancestor path, e.g. 'Default/Retail'. Used to
        folder-qualify upload document paths so the same filename in two folders
        stays unique and the citation reads meaningfully."""
        with self._lock:
            parts: list[str] = []
            cur_id: int | None = folder_id
            while cur_id is not None:
                row = self.con.execute(
                    "SELECT parent_id, name FROM folders WHERE id=?", (cur_id,)).fetchone()
                if row is None:
                    break
                parts.append(row[1])
                cur_id = row[0]
        return "/".join(reversed(parts))

    def rename_folder(self, folder_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("folder name cannot be empty")
        with self._lock, self.con:
            try:
                cur = self.con.execute(
                    "UPDATE folders SET name=? WHERE id=?", (new_name, folder_id))
            except sqlite3.IntegrityError as e:
                raise ValueError(f"a folder named {new_name!r} already exists here") from e
            if cur.rowcount == 0:
                raise ValueError(f"no folder with id {folder_id}")

    def _subtree_ids(self, folder_id: int) -> list[int]:
        """folder_id plus all descendants (recursive). Caller holds the lock."""
        rows = self.con.execute(
            """WITH RECURSIVE sub(id) AS (
                   SELECT ? UNION ALL
                   SELECT f.id FROM folders f JOIN sub ON f.parent_id = sub.id)
               SELECT id FROM sub""",
            (folder_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def move_folder(self, folder_id: int, new_parent_id: int) -> None:
        """Reparent a folder; rewrites the whole moved subtree's tier to the new
        parent's tier (no per-subfolder overrides in SP1). Refuses moving a root,
        moving under itself/a descendant (cycle), or a duplicate sibling name."""
        with self._lock, self.con:
            row = self.con.execute(
                "SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if row is None:
                raise ValueError(f"no folder with id {folder_id}")
            if row[0] is None:
                raise ValueError("cannot move a root folder")
            prow = self.con.execute(
                "SELECT min_role FROM folders WHERE id=?", (new_parent_id,)).fetchone()
            if prow is None:
                raise ValueError(f"no folder with id {new_parent_id}")
            subtree = self._subtree_ids(folder_id)
            if new_parent_id in subtree:
                raise ValueError("cannot move a folder under itself")
            try:
                self.con.execute(
                    "UPDATE folders SET parent_id=? WHERE id=?", (new_parent_id, folder_id))
            except sqlite3.IntegrityError as e:
                raise ValueError("a folder with that name already exists in the target") from e
            ph = ",".join("?" * len(subtree))
            self.con.execute(
                f"UPDATE folders SET min_role=? WHERE id IN ({ph})", (prow[0], *subtree))

    def delete_folder(self, folder_id: int) -> bool:
        """Delete a folder, its descendants, and all their documents/chunks/vectors.
        Roots cannot be deleted. Returns False if the folder does not exist."""
        with self._lock:
            row = self.con.execute(
                "SELECT parent_id FROM folders WHERE id=?", (folder_id,)).fetchone()
            if row is None:
                return False
            if row[0] is None:
                raise ValueError("cannot delete a root folder")
            subtree = self._subtree_ids(folder_id)
            ph = ",".join("?" * len(subtree))
            doc_ids = [r[0] for r in self.con.execute(
                f"SELECT id FROM documents WHERE folder_id IN ({ph})", subtree)]
            with self.con:
                for did in doc_ids:
                    self._delete_chunks(did)
                # ON DELETE CASCADE removes descendant folders + their documents,
                # but chunk_vec (vec0) is not FK-managed, so chunks were cleared above.
                self.con.execute("DELETE FROM folders WHERE id=?", (folder_id,))
            return True
