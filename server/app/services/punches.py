"""Bélyegzés-feldolgozás: UID-feloldás, irányszámítás, munkamenetek.

Ez a modul az egész rendszer szíve. A legfontosabb tulajdonsága, hogy a
`punches` tábla az **egyetlen igazságforrás**, a `work_sessions` sorok pedig
belőle bármikor újraépíthető, származtatott adatok.

Ezért tud helyesen viselkedni az offline puffer visszatöltése: ha a gateway
három óra kiesés után egyszerre küld fel egy köteget, azok az események
`ts_utc` szerint korábbiak, mint a közben már beérkezett friss bélyegzések.
A `recalculate_directions` ilyenkor újrajátssza az érintett szakaszt
`ts_utc` sorrendben, és mindent a helyére tesz.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Iterable

from pydantic import ValidationError
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Card, Employee, Punch, WorkSession
from ..schemas import EventError, EventIn, EventIngestResponse
from ..timeutil import local_clock_utc, local_date_of, parse_iso_to_utc, utcnow

log = logging.getLogger("iroda.punches")

DEBOUNCED_NOTE = "debounced"
MANUAL_NOTE = "manual"
MANUAL_FIX_NOTE = "manual_fix"

# Épeszűségi korlátok a gateway órájára. Ezeken kívül az időbélyeg hibás.
MIN_VALID_TS = datetime(2020, 1, 1)
MAX_FUTURE_SKEW = timedelta(days=1)


# --------------------------------------------------------------------------
# Jegyzet-címkék (a `punches.note` mező vesszővel elválasztott címkéket tárol)
# --------------------------------------------------------------------------
def note_tags(note: str | None) -> list[str]:
    return [tag.strip() for tag in (note or "").split(",") if tag.strip()]


def has_tag(note: str | None, tag: str) -> bool:
    return tag in note_tags(note)


def add_tag(note: str | None, tag: str) -> str:
    tags = note_tags(note)
    if tag not in tags:
        tags.append(tag)
    return ",".join(tags)


def remove_tag(note: str | None, tag: str) -> str | None:
    tags = [t for t in note_tags(note) if t != tag]
    return ",".join(tags) or None


def normalize_uid(uid: str) -> str:
    """A kártya UID-ját mindenhol azonos alakra hozzuk, hogy a kézzel felvitt
    és az olvasóból érkező azonosító biztosan találkozzon."""
    return uid.strip().upper()


# --------------------------------------------------------------------------
# Automatikus napzárás
# --------------------------------------------------------------------------
def auto_close_deadline(started_at: datetime) -> datetime:
    """Meddig maradhat nyitva egy adott pillanatban indult munkamenet.

    A határidő a munkamenet **saját lokális napjának** `AUTO_CLOSE_HOUR`
    időpontja. Ha valaki már a napzárás után lépett be (pl. 23:59:30-kor),
    rá a következő nap zárása vonatkozik.
    """
    day = local_date_of(started_at)
    deadline = local_clock_utc(day, settings.auto_close_hour, settings.auto_close_minute)
    if deadline <= started_at:
        deadline = local_clock_utc(
            day + timedelta(days=1), settings.auto_close_hour, settings.auto_close_minute
        )
    return deadline


def apply_auto_close(session: WorkSession) -> None:
    """Lezár egy nyitva felejtett munkamenetet, kézi ellenőrzésre megjelölve."""
    end = max(auto_close_deadline(session.started_at), session.started_at)
    session.ended_at = end
    session.duration_seconds = int((end - session.started_at).total_seconds())
    session.auto_closed = True
    session.out_punch_id = None


def close_stale_sessions(db: Session, now: datetime | None = None) -> int:
    """A háttérfeladat belépési pontja: minden lejárt, nyitott munkamenet zárása."""
    now = now or utcnow()
    open_sessions = db.scalars(
        select(WorkSession).where(WorkSession.ended_at.is_(None))
    ).all()
    closed = 0
    for session in open_sessions:
        if auto_close_deadline(session.started_at) <= now:
            apply_auto_close(session)
            closed += 1
    if closed:
        db.commit()
        log.info("Automatikus zárás: %d nyitva maradt munkamenet lezárva", closed)
    return closed


# --------------------------------------------------------------------------
# Irányszámítás és munkamenet-újraépítés
# --------------------------------------------------------------------------
def _last_effective_ts_before(db: Session, card_uid: str, before: datetime) -> datetime | None:
    """Az adott kártya utolsó, NEM debounce-olt bélyegzése a megadott idő előtt.

    Ez a debounce viszonyítási pontja: a gyors ismétlődéseket mindig az utolsó
    érvényes bélyegzéshez mérjük, nem az előző (esetleg szintén eldobott)
    ismétléshez.
    """
    return db.scalar(
        select(Punch.ts_utc)
        .where(
            Punch.card_uid == card_uid,
            Punch.ts_utc < before,
            or_(Punch.note.is_(None), ~Punch.note.contains(DEBOUNCED_NOTE)),
        )
        .order_by(Punch.ts_utc.desc(), Punch.id.desc())
        .limit(1)
    )


def _rebuild_anchor(db: Session, employee_id: int, from_ts: datetime) -> datetime:
    """Az a legkorábbi időpont, ahonnan biztonságosan újrajátszható a történet.

    A `from_ts`-től kezdve minden érintett munkamenetet az elejétől kell
    újraépíteni, különben egy félbevágott munkamenet maradna vissza.
    """
    anchor = from_ts
    for _ in range(16):  # a gyakorlatban 1-2 kör alatt lefut
        earliest = db.scalar(
            select(WorkSession.started_at)
            .where(
                WorkSession.employee_id == employee_id,
                or_(
                    WorkSession.started_at >= anchor,
                    WorkSession.ended_at.is_(None),
                    WorkSession.ended_at >= anchor,
                ),
            )
            .order_by(WorkSession.started_at.asc())
            .limit(1)
        )
        if earliest is None or earliest >= anchor:
            return anchor
        anchor = earliest
    return anchor


def recalculate_directions(db: Session, employee_id: int, from_ts: datetime) -> None:
    """Újraszámolja egy dolgozó irányait és munkameneteit `from_ts`-től.

    A feldolgozás **mindig `ts_utc` szerint rendezve** történik, soha nem
    `received_at` szerint – ez teszi lehetővé, hogy egy órákkal később
    felérkező, de korábban keletkezett esemény a helyére kerüljön.
    """
    anchor = _rebuild_anchor(db, employee_id, from_ts)

    # Az érintett munkameneteket eldobjuk; mindjárt újraépítjük őket.
    db.execute(
        delete(WorkSession).where(
            WorkSession.employee_id == employee_id,
            or_(
                WorkSession.started_at >= anchor,
                WorkSession.ended_at.is_(None),
                WorkSession.ended_at >= anchor,
            ),
        )
    )
    db.flush()

    punches = list(
        db.scalars(
            select(Punch)
            .where(Punch.employee_id == employee_id, Punch.ts_utc >= anchor)
            .order_by(Punch.ts_utc.asc(), Punch.id.asc())
        )
    )
    if not punches:
        db.flush()
        return

    # Debounce-viszonyítás: kártyánként az anchor előtti utolsó érvényes bélyegzés.
    last_effective: dict[str, datetime] = {}
    for uid in {p.card_uid for p in punches}:
        previous = _last_effective_ts_before(db, uid, anchor)
        if previous is not None:
            last_effective[uid] = previous

    debounce = timedelta(seconds=max(settings.debounce_seconds, 0))
    open_session: WorkSession | None = None

    for punch in punches:
        previous = last_effective.get(punch.card_uid)
        if (
            debounce
            and previous is not None
            and timedelta(0) <= (punch.ts_utc - previous) < debounce
        ):
            # Túl gyors ismétlés: eltároljuk, de nem nyit és nem zár munkamenetet.
            punch.direction = None
            punch.note = add_tag(punch.note, DEBOUNCED_NOTE)
            continue

        punch.note = remove_tag(punch.note, DEBOUNCED_NOTE)
        last_effective[punch.card_uid] = punch.ts_utc

        # A nyitott munkamenet napzárási határideje lejárt-e időközben?
        if open_session is not None and auto_close_deadline(open_session.started_at) <= punch.ts_utc:
            apply_auto_close(open_session)
            open_session = None

        # Kézzel rögzített irányt nem írunk felül – az admin döntése horgony.
        forced = has_tag(punch.note, MANUAL_FIX_NOTE) and punch.direction in ("IN", "OUT")
        desired = punch.direction if forced else ("OUT" if open_session is not None else "IN")

        if desired == "IN":
            if open_session is not None:
                # Kézi IN egy nyitott munkamenet közben: a korábbit napzárással
                # zárjuk, hogy látszódjon: ellenőrzést igényel.
                apply_auto_close(open_session)
            punch.direction = "IN"
            open_session = WorkSession(
                employee_id=employee_id,
                in_punch_id=punch.id,
                started_at=punch.ts_utc,
                auto_closed=False,
            )
            db.add(open_session)
            db.flush()
        else:
            punch.direction = "OUT"
            if open_session is None:
                # Lógó kilépés (pl. kézzel felvitt OUT nyitott munkamenet nélkül):
                # az irány megmarad, de nincs mit lezárni.
                continue
            open_session.out_punch_id = punch.id
            open_session.ended_at = punch.ts_utc
            open_session.duration_seconds = int(
                (punch.ts_utc - open_session.started_at).total_seconds()
            )
            open_session.auto_closed = False
            open_session = None

    # A sor végén maradt nyitott munkamenet: ha a határideje már elmúlt, zárjuk.
    if open_session is not None and auto_close_deadline(open_session.started_at) <= utcnow():
        apply_auto_close(open_session)

    db.flush()


# --------------------------------------------------------------------------
# Beérkező események feldolgozása
# --------------------------------------------------------------------------
def _resolve_employee_id(db: Session, uid: str) -> int | None:
    card = db.scalar(select(Card).where(Card.uid == uid))
    if card is None or not card.active:
        return None
    return card.employee_id


def _validate_timestamp(raw: str) -> datetime:
    ts_utc = parse_iso_to_utc(raw)
    if ts_utc < MIN_VALID_TS or ts_utc > utcnow() + MAX_FUTURE_SKEW:
        raise ValueError("az időbélyeg hihetetlen tartományban van")
    return ts_utc


def ingest_events(
    db: Session, gateway_id: str, raw_events: Iterable[Any]
) -> EventIngestResponse:
    """Egy batch bélyegzés feldolgozása.

    Egyetlen hibás elem sem buktathatja el a köteget: a jó eseményeket
    elmentjük, a rosszat az `errors` listába tesszük.
    """
    accepted: list[str] = []
    duplicates: list[str] = []
    errors: list[EventError] = []
    # Dolgozónként a legkorábbi most beszúrt időpont – innen kell újraszámolni.
    affected: dict[int, datetime] = {}

    for raw in raw_events:
        uuid_hint = raw.get("event_uuid") if isinstance(raw, dict) else None
        uuid_hint = uuid_hint if isinstance(uuid_hint, str) else None

        try:
            event = EventIn.model_validate(raw)
        except ValidationError:
            errors.append(EventError(event_uuid=uuid_hint, reason="invalid_payload"))
            continue

        try:
            ts_utc = _validate_timestamp(event.ts_local)
        except ValueError:
            errors.append(EventError(event_uuid=event.event_uuid, reason="invalid_timestamp"))
            continue

        if db.scalar(select(Punch.id).where(Punch.event_uuid == event.event_uuid)) is not None:
            # A gateway újraküldte, mert nem kapta meg a választ. Csendben elfogadjuk.
            duplicates.append(event.event_uuid)
            continue

        uid = normalize_uid(event.uid)
        employee_id = _resolve_employee_id(db, uid)
        punch = Punch(
            event_uuid=event.event_uuid,
            card_uid=uid,
            employee_id=employee_id,
            direction=None,
            ts_utc=ts_utc,
            ts_local=event.ts_local,
            gateway_id=gateway_id,
            received_at=utcnow(),
        )
        try:
            with db.begin_nested():
                db.add(punch)
                db.flush()
        except IntegrityError:
            # Verseny két párhuzamos feltöltés között – ez is duplikátum.
            duplicates.append(event.event_uuid)
            continue

        accepted.append(event.event_uuid)
        if employee_id is not None:
            current = affected.get(employee_id)
            affected[employee_id] = ts_utc if current is None else min(current, ts_utc)

    # Az irányszámítás mindig a köteg beszúrása UTÁN fut, dolgozónként egyszer,
    # a legkorábbi most érkezett eseménytől kezdve.
    for employee_id, from_ts in affected.items():
        recalculate_directions(db, employee_id, from_ts)

    db.commit()
    return EventIngestResponse(accepted=accepted, duplicates=duplicates, errors=errors)


# --------------------------------------------------------------------------
# Kártyakezelés
# --------------------------------------------------------------------------
def assign_card(
    db: Session, uid: str, employee_id: int | None, note: str | None = None
) -> tuple[Card, int]:
    """Kártya dolgozóhoz rendelése, a korábbi bélyegzések visszamenőleges feldolgozásával.

    A még senkihez nem tartozó (`employee_id IS NULL`) régi bélyegzések
    megkapják az új dolgozót, majd lefut rájuk az irányszámítás. A korábban
    már MÁS dolgozóhoz rendelt bélyegzéseket nem írjuk át – az a múlt.
    """
    uid = normalize_uid(uid)
    card = db.scalar(select(Card).where(Card.uid == uid))
    if card is None:
        card = Card(uid=uid, active=True)
        db.add(card)

    card.employee_id = employee_id
    card.assigned_at = utcnow() if employee_id is not None else None
    if note is not None:
        card.note = note
    db.flush()

    backfilled = 0
    if employee_id is not None:
        orphans = list(
            db.scalars(
                select(Punch)
                .where(Punch.card_uid == uid, Punch.employee_id.is_(None))
                .order_by(Punch.ts_utc.asc())
            )
        )
        if orphans:
            from_ts = orphans[0].ts_utc
            for punch in orphans:
                punch.employee_id = employee_id
            backfilled = len(orphans)
            db.flush()
            recalculate_directions(db, employee_id, from_ts)

    db.commit()
    return card, backfilled


def unknown_card_uids(db: Session) -> list[dict[str, Any]]:
    """Olyan UID-ok, amelyekhez nem tartozik dolgozó – ezek várnak hozzárendelésre."""
    rows = db.execute(
        select(
            Punch.card_uid,
            Punch.gateway_id,
        )
        .where(Punch.employee_id.is_(None))
        .order_by(Punch.ts_utc.desc())
    ).all()

    seen: dict[str, dict[str, Any]] = {}
    for card_uid, gateway_id in rows:
        entry = seen.setdefault(
            card_uid, {"uid": card_uid, "count": 0, "last_seen": None, "gateway_id": gateway_id}
        )
        entry["count"] += 1

    for uid, entry in seen.items():
        entry["last_seen"] = db.scalar(
            select(Punch.ts_utc)
            .where(Punch.card_uid == uid, Punch.employee_id.is_(None))
            .order_by(Punch.ts_utc.desc())
            .limit(1)
        )
    return sorted(seen.values(), key=lambda e: e["last_seen"] or MIN_VALID_TS, reverse=True)


# --------------------------------------------------------------------------
# Kézi javítás
# --------------------------------------------------------------------------
def create_manual_punch(
    db: Session,
    employee_id: int,
    ts_utc: datetime,
    direction: str | None,
    ts_local_raw: str,
    event_uuid: str,
) -> Punch:
    """Kézzel felvitt bélyegzés (pl. valaki elfelejtett kijelentkezni)."""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise ValueError("Ismeretlen dolgozó")

    note = MANUAL_NOTE
    if direction in ("IN", "OUT"):
        # Explicit irány esetén horgonyként kezeljük: az újraszámolás sem írja felül.
        note = add_tag(note, MANUAL_FIX_NOTE)
    else:
        direction = None

    card_uid = db.scalar(
        select(Card.uid).where(Card.employee_id == employee_id, Card.active.is_(True)).limit(1)
    )
    punch = Punch(
        event_uuid=event_uuid,
        card_uid=card_uid or f"MANUAL-{employee.employee_code}",
        employee_id=employee_id,
        direction=direction,
        ts_utc=ts_utc,
        ts_local=ts_local_raw,
        gateway_id=None,
        received_at=utcnow(),
        note=note,
    )
    db.add(punch)
    db.flush()
    recalculate_directions(db, employee_id, ts_utc)
    db.commit()
    return punch


def override_punch_direction(db: Session, punch: Punch, direction: str | None) -> None:
    """Egy meglévő bélyegzés irányának felülírása."""
    if direction in ("IN", "OUT"):
        punch.direction = direction
        punch.note = add_tag(add_tag(punch.note, MANUAL_FIX_NOTE), MANUAL_NOTE)
    else:
        # "automatikus": az újraszámolás dönt.
        punch.direction = None
        punch.note = remove_tag(punch.note, MANUAL_FIX_NOTE)
    db.flush()
    if punch.employee_id is not None:
        recalculate_directions(db, punch.employee_id, punch.ts_utc)
    db.commit()


def delete_punch(db: Session, punch: Punch) -> None:
    """Bélyegzés törlése. Csak kézzel, adminként történhet – automatikusan soha."""
    employee_id = punch.employee_id
    ts_utc = punch.ts_utc

    # A munkamenetek hivatkozásait előbb oldjuk, hogy a törlés ne bukjon FK-n.
    for session in db.scalars(
        select(WorkSession).where(
            or_(WorkSession.in_punch_id == punch.id, WorkSession.out_punch_id == punch.id)
        )
    ).all():
        if session.in_punch_id == punch.id:
            session.in_punch_id = None
        if session.out_punch_id == punch.id:
            session.out_punch_id = None
    db.flush()

    db.delete(punch)
    db.flush()
    if employee_id is not None:
        recalculate_directions(db, employee_id, ts_utc)
    db.commit()
