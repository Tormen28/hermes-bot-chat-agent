"""Fase 0 — reproducción de E4: leer con error = estado vacío que se sobrescribe.

Falla (v1.2 §1.2 E4): ``team_flow._load_state`` devuelve estado vacío ante
``OSError`` o JSON inválido, y tanto ``record_delivery`` como
``take_announcements`` guardan ese vacío encima con ``_save_state``. Una
lectura que falla una sola vez BORRA todos los pendientes (pérdida) y la
lista de anunciados (duplicados después). Amplifica E1/E2: basta un fallo
transitorio de lectura para vaciar el buzón.

El fixture siembra pendientes, inyecta un error de lectura y luego hace una
operación normal; afirma que los pendientes originales siguen ahí. Hoy se
borran, así que los tests FALLAN.

Dos variantes del error de lectura:
1. JSON corrupto (``JSONDecodeError``) — determinista, sin mocks.
2. ``OSError`` al leer (disco/permiso) — con ``unittest.mock`` sobre
   ``Path.read_text`` (un ``chmod 000`` no sirve: los tests pueden correr
   como root y root ignora los permisos).

Estado esperado: estos tests FALLAN con el código actual (89f8b25) y deben
PASAR cuando la Fase 1a aborte la operación ante un estado ilegible (nunca
"vacío y sobrescribir").

Fase 1a: el almacén vivo es SQLite (``deliveries.db`` junto al JSON). Los
tests ya no afirman sobre el contenido del JSON (que el backend sqlite no
escribe) sino sobre el estado del store: un JSON corrupto o ilegible no
toca el store, y el JSON queda intacto (la importación solo lee). El tercer
test cubre además el caso simétrico: una base SQLite corrupta aborta la
operación sin borrar nada.

Notas:
- Solo Python 3.12.3 en este entorno (el vivo usa 3.14.9); no se usa ningún
  rasgo posterior a 3.12.
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if "team_flow" in sys.modules:
    tf = sys.modules["team_flow"]
else:
    spec = importlib.util.spec_from_file_location("team_flow", ROOT / "team_flow.py")
    tf = importlib.util.module_from_spec(spec)
    sys.modules["team_flow"] = tf
    spec.loader.exec_module(tf)

import delivery_store  # noqa: E402  (team_flow ya lo puso en sys.path)

GODS = ["researcher"]


class TestReproE4(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.notify = self.tmp / "team-notify.json"
        self.store = delivery_store.DeliveryStore(
            delivery_store.store_path_for(self.notify))

    def _seed(self):
        """Siembra 3 pendientes válidos con el propio record_delivery."""
        for i in range(3):
            tf.record_delivery(
                self.notify,
                task_id=f"t-old-{i}",
                assignee="researcher",
                summary=f"old result {i}",
                gods=list(GODS),
            )
        self.assertEqual(
            len(self.store.get("t-old-0")) + len(self.store.get("t-old-1"))
            + len(self.store.get("t-old-2")), 3,
            "la siembra no dejó 3 pendientes en el store")

    def _pending_ids(self):
        stats = self.store.stats()
        rows = []
        for i in range(4):
            rows += self.store.get(f"t-old-{i}")
        rows += self.store.get("t-new")
        return [r["task_id"] for r in rows if r["status"] == "pending"]

    def test_corrupt_json_wipes_pending(self):
        """JSON corrupto + operación normal: los pendientes deben sobrevivir."""
        self._seed()
        self.notify.write_text("{not valid json", encoding="utf-8")
        before = self.notify.read_bytes()
        tf.record_delivery(
            self.notify,
            task_id="t-new",
            assignee="researcher",
            summary="new result",
            gods=list(GODS),
        )
        pending = self._pending_ids()
        self.assertIn(
            "t-old-0", pending,
            "E4 REPRODUCIDA (JSON corrupto): tras una lectura fallida, "
            "record_delivery guardó el estado vacío encima y los 3 pendientes "
            f"originales desaparecieron; pendientes ahora: {sorted(pending)}",
        )
        self.assertEqual(
            len(pending), 4,
            "E4 REPRODUCIDA (JSON corrupto): debían quedar 3 pendientes "
            f"originales + t-new; hay {sorted(pending)}",
        )
        self.assertEqual(
            self.notify.read_bytes(), before,
            "el JSON corrupto no debe modificarse: la importación solo lee",
        )

    def test_unreadable_file_wipes_pending(self):
        """OSError al leer + operación normal: los pendientes deben sobrevivir."""
        self._seed()
        with mock.patch.object(
            Path, "read_text", side_effect=OSError("simulated disk read error")
        ):
            tf.record_delivery(
                self.notify,
                task_id="t-new",
                assignee="researcher",
                summary="new result",
                gods=list(GODS),
            )
        pending = self._pending_ids()
        self.assertIn(
            "t-old-0", pending,
            "E4 REPRODUCIDA (OSError al leer): tras una lectura fallida, "
            "record_delivery guardó el estado vacío encima y los 3 pendientes "
            f"originales desaparecieron; pendientes ahora: {sorted(pending)}",
        )

    def test_corrupt_json_wipes_announced_on_take(self):
        """JSON corrupto + take: los ofrecidos no deben borrarse ni duplicarse."""
        self._seed()
        taken = tf.take_announcements(self.notify)
        self.assertEqual(len(taken), 3, "la primera toma debía devolver 3 avisos")
        self.notify.write_text("{not valid json", encoding="utf-8")
        before = self.notify.read_bytes()
        tf.record_delivery(
            self.notify,
            task_id="t-new",
            assignee="researcher",
            summary="new result",
            gods=list(GODS),
        )
        # Los 3 viejos siguen 'offered' (una sola vez cada uno) y t-new
        # queda 'pending'; la lectura fallida no tocó el store.
        for i in range(3):
            rows = self.store.get(f"t-old-{i}")
            self.assertEqual(len(rows), 1, f"t-old-{i} debe existir una sola vez")
            self.assertEqual(
                rows[0]["status"], "offered",
                f"E4 REPRODUCIDA (anunciados): la lectura fallida cambió el "
                f"estado de t-old-{i} a {rows[0]['status']}",
            )
        rows = self.store.get("t-new")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertEqual(
            self.notify.read_bytes(), before,
            "el JSON corrupto no debe modificarse: la importación solo lee",
        )
        # Tras confirmar el turno, una segunda toma solo trae t-new.
        tf.confirm_turn(self.notify, "", turn_id=None)
        second = tf.take_announcements(self.notify)
        self.assertEqual(
            [item["id"] for item in second], ["t-new"],
            "E4 REPRODUCIDA (duplicados): tras la lectura fallida, los 3 "
            "avisos viejos se volvieron a anunciar como duplicados",
        )

    def test_corrupt_sqlite_aborts_without_wiping(self):
        """Base SQLite corrupta: la operación aborta sin borrar nada."""
        self._seed()
        db_path = delivery_store.store_path_for(self.notify)
        before = db_path.read_bytes()
        db_path.write_bytes(b"this is not a sqlite database")
        with self.assertRaises(
            sqlite3.DatabaseError,
            msg="una base corrupta debe abortar la operación, no devolver vacío",
        ):
            tf.record_delivery(
                self.notify,
                task_id="t-new",
                assignee="researcher",
                summary="new result",
                gods=list(GODS),
            )
        self.assertEqual(
            db_path.read_bytes(), b"this is not a sqlite database",
            "la operación fallida no debe escribir nada sobre la base corrupta",
        )
        self.assertNotEqual(
            before, db_path.read_bytes(),
            "la siembra debía haber creado una base válida antes de corromperla",
        )


if __name__ == "__main__":
    unittest.main()
