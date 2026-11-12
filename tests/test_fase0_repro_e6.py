"""Fase 0 — reproducción de E6: duplicados por recorte de ``announced``.

Falla (v1.2 §1.2 E6): ``take_announcements`` recorta ``announced`` a los
últimos 500 (``data["announced"] = data["announced"][-500:]``), pero
``pending`` NUNCA se poda. Cuando un ID sale de ``announced`` sigue en
``pending`` y se vuelve a anunciar en la siguiente toma. Además el archivo
crece sin límite: los pendientes entregados no se eliminan jamás.

El fixture registra 510 entregas, las toma todas y vuelve a tomar; afirma
que la segunda toma está vacía (ningún aviso se anuncia dos veces). Hoy la
segunda toma devuelve los 10 IDs que salieron del recorte, así que el test
FALLA.

Estado esperado: este test FALLA con el código actual (89f8b25) y debe
PASAR cuando la Fase 1a pode ``pending`` al confirmar la entrega (o use
retención explícita, v1.2 §5.3).

Fase 1a: el protocolo es ``pending -> offered -> delivered_in_turn``. Una
segunda toma SIN confirmar re-ofrece por diseño (eso es E5: el turno
anterior pudo interrumpirse), así que el test confirma el turno entre las
dos tomas (como haría ``post_llm_call``) y afirma que la segunda toma está
vacía y que ``pending`` queda en 0. La retención es explícita: las
entregas confirmadas quedan como ``delivered_in_turn`` hasta ``prune``,
nunca en una lista recortada que reactive IDs viejos.

Notas:
- 510 entregas: 500 es el límite del recorte; con 510 quedan 10 IDs
  expulsados y el duplicado es inequívoco. Con SQLite cada entrega es una
  transacción corta; el test tarda del orden de 1 s.
- Solo Python 3.12.3 en este entorno (el vivo usa 3.14.9); no se usa ningún
  rasgo posterior a 3.12.
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

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
N = 510  # > 500 (límite del recorte de `announced`)


class TestReproE6(unittest.TestCase):
    def test_no_announcement_is_repeated_after_trim(self):
        """Ningún aviso se anuncia dos veces, aunque supere los 500."""
        tmp = Path(tempfile.mkdtemp())
        notify = tmp / "team-notify.json"
        for i in range(N):
            tf.record_delivery(
                notify,
                task_id=f"t-{i:04d}",
                assignee="researcher",
                summary=f"result {i}",
                gods=list(GODS),
            )
        first = tf.take_announcements(notify)
        self.assertEqual(
            len(first), N,
            f"la primera toma debía devolver las {N} entregas; devolvió {len(first)}",
        )
        first_ids = [item["id"] for item in first]
        self.assertEqual(len(set(first_ids)), N, "la primera toma ya trae duplicados")

        # El turno se completó: post_llm_call confirma las ofrecidas.
        tf.confirm_turn(notify, "", turn_id=None)
        second = tf.take_announcements(notify)
        second_ids = [item["id"] for item in second]
        self.assertEqual(
            second, [],
            f"E6 REPRODUCIDA: tras recortar `announced` a los últimos 500, "
            f"{len(second)} avisos se volvieron a anunciar en la segunda toma "
            f"(primeros: {second_ids[:10]}); `pending` nunca se poda y los IDs "
            "expulsados del recorte se re-anuncian como duplicados",
        )
        store = delivery_store.DeliveryStore(delivery_store.store_path_for(notify))
        stats = store.stats()
        self.assertEqual(stats["by_status"].get("pending", 0), 0,
                         "tras confirmar, no debe quedar nada pendiente")
        self.assertEqual(stats["by_status"].get("offered", 0), 0,
                         "tras confirmar, no debe quedar nada ofrecido")
        self.assertEqual(stats["by_status"].get("delivered_in_turn", 0), N,
                         "las 510 entregas deben quedar como entregadas, no borradas")

    def test_pending_does_not_grow_forever(self):
        """Tras tomar y confirmar todas las entregas, `pending` queda en 0."""
        tmp = Path(tempfile.mkdtemp())
        notify = tmp / "team-notify.json"
        for i in range(N):
            tf.record_delivery(
                notify,
                task_id=f"t-{i:04d}",
                assignee="researcher",
                summary=f"result {i}",
                gods=list(GODS),
            )
        tf.take_announcements(notify)
        tf.confirm_turn(notify, "", turn_id=None)
        store = delivery_store.DeliveryStore(delivery_store.store_path_for(notify))
        stats = store.stats()
        self.assertEqual(
            stats["by_status"].get("pending", 0), 0,
            f"E6 REPRODUCIDA (crecimiento sin límite): tras tomar las {N} "
            f"entregas, `pending` sigue guardando "
            f"{stats['by_status'].get('pending', 0)} entradas que nunca se "
            "podan; el archivo crece para siempre",
        )
        self.assertEqual(
            stats["by_status"].get("offered", 0), 0,
            "tras confirmar, no debe quedar nada en estado offered",
        )


if __name__ == "__main__":
    unittest.main()
