# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The RadioFoundry Authors
"""Private, disk-backed JSON history with bounded SQLite and Python caches.

List semantics keep existing checkpoint readers and streaming JSON encoders usable.
A history owns its temporary file until its last reader releases it.
"""

import json
import sqlite3
import tempfile
import weakref


class History(list):
    def __init__(self, values=(), *, encode=None, decode=None):
        self.file = tempfile.NamedTemporaryFile(prefix="rf-history-")
        self.db = sqlite3.connect(self.file.name, check_same_thread=False)
        # Scratch history is rebuilt from the run; durable output is written separately at shutdown.
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("PRAGMA cache_size=-256")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, data TEXT, k0, k1, k2, k3, k4)")
        self.db.execute("CREATE INDEX history_data ON history(data)")
        self.encode = encode or (lambda value: value)
        self.decode = decode or (lambda value: value)
        self.count = 0
        weakref.finalize(self, self._close, self.db, self.file)
        self.extend(values)

    @staticmethod
    def _close(db, file):
        db.close()
        file.close()

    def append(self, value):
        self.db.execute("INSERT INTO history(id,data) VALUES (?,?)",
                        (self.count, json.dumps(self.encode(value), separators=(",", ":"), allow_nan=False)))
        self.count += 1

    add = append

    def extend(self, values):
        for value in values:
            self.append(value)

    def __len__(self):
        return self.count

    def __iter__(self):
        for data, in self.db.execute("SELECT data FROM history ORDER BY id"):
            yield self.decode(json.loads(data))

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self.count))]
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        data, = self.db.execute("SELECT data FROM history WHERE id=?", (index,)).fetchone()
        return self.decode(json.loads(data))

    def __setitem__(self, index, value):
        if not 0 <= index < self.count:
            raise IndexError(index)
        self.db.execute("UPDATE history SET data=? WHERE id=?",
                        (json.dumps(self.encode(value), separators=(",", ":"), allow_nan=False), index))

    def __contains__(self, value):
        data = json.dumps(self.encode(value), separators=(",", ":"), allow_nan=False)
        return self.db.execute("SELECT 1 FROM history WHERE data=? LIMIT 1", (data,)).fetchone() is not None

    def index(self, value):
        for index, item in enumerate(self):
            if item == value:
                return index
        raise ValueError(value)

    def __eq__(self, other):
        return len(self) == len(other) and all(a == b for a, b in zip(self, other))

    def __ne__(self, other):
        return not self == other

    def __deepcopy__(self, memo):
        return History(self, encode=self.encode, decode=self.decode)

    def sort(self, *, key):
        for identity, data in self.db.execute("SELECT id,data FROM history"):
            parts = tuple(key(self.decode(json.loads(data))))
            self.db.execute("UPDATE history SET k0=?,k1=?,k2=?,k3=?,k4=? WHERE id=?",
                            (*parts, *((None,) * (5 - len(parts))), identity))
        self.db.execute("CREATE INDEX IF NOT EXISTS history_order ON history(k0,k1,k2,k3,k4,id)")
        self.db.execute("CREATE TABLE sorted AS SELECT row_number() OVER (ORDER BY k0,k1,k2,k3,k4,id)-1 AS id, "
                        "data FROM history")
        self.db.execute("DELETE FROM history")
        self.db.execute("INSERT INTO history(id,data) SELECT id,data FROM sorted")
        self.db.execute("DROP TABLE sorted")
