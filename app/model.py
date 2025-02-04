import json
import uuid
from datetime import datetime, timedelta
from enum import Enum, IntEnum
from typing import Optional, Union

from fastapi import HTTPException
from pydantic import BaseModel, Json
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import NoResultFound

from .config import TTL_LIVE_INTERVAL, TTL_POLLING_INTERVAL
from .db import engine


class InvalidToken(Exception):
    """指定されたtokenが不正だったときに投げる"""


class SafeUser(BaseModel):
    """token を含まないUser"""

    id: int
    name: str
    leader_card_id: int

    class Config:
        orm_mode = True


def create_user(name: str, leader_card_id: int) -> str:
    """Create new user and returns their token"""
    token = str(uuid.uuid4())
    # NOTE: tokenが衝突したらリトライする必要がある.
    with engine.begin() as conn:
        conn: Connection
        while _get_user_by_token(conn, token) is not None:
            token = str(uuid.uuid4())
        result = conn.execute(
            text(
                "INSERT INTO `user` (`name`, `token`, `leader_card_id`) VALUES (:name, :token, :leader_card_id)"
            ),
            {"name": name, "token": token, "leader_card_id": leader_card_id},
        )
        # print(f"create_user(): id={result.lastrowid} {token=}")
    return token


def _get_user_by_token(conn: Connection, token: str) -> Optional[SafeUser]:
    result = conn.execute(
        text("SELECT `id`, `name`, `leader_card_id` FROM `user` WHERE `token`=:token"),
        {"token": token},
    )
    row = result.one_or_none()
    return row and SafeUser.from_orm(row)


def _get_user_by_token_strict(conn: Connection, token: str) -> SafeUser:
    user = _get_user_by_token(conn, token)
    if user is None:
        raise InvalidToken
    return user


def get_user_by_token(token: str) -> Optional[SafeUser]:
    with engine.begin() as conn:
        return _get_user_by_token(conn, token)


def update_user(token: str, name: str, leader_card_id: int) -> None:
    with engine.begin() as conn:
        conn: Connection
        result = conn.execute(
            text(
                "UPDATE `user` SET `name`=:name, `leader_card_id`=:leader_card_id WHERE `token`=:token"
            ),
            {"name": name, "token": token, "leader_card_id": leader_card_id},
        )
        if result.rowcount != 1:
            raise InvalidToken


# Room API
class LiveDifficulty(Enum):
    NORMAL = 1
    HARD = 2


class JoinRoomResult(Enum):
    OK = 1  # 入場OK
    ROOM_FULL = 2  # 満員
    DISBANDED = 3  # 解散済み
    OTHER_ERROR = 4  # その他エラー


class WaitRoomStatus(Enum):
    WATING = 1
    LIVE_START = 2
    DISSOLUTION = 3


class RoomInfo(BaseModel):
    room_id: str
    live_id: str
    joined_user_count: int
    max_user_count: int

    class Config:
        orm_mode = True


class RoomUser(BaseModel):
    user_id: int
    name: str
    leader_card_id: int
    select_difficulty: LiveDifficulty
    is_me: bool
    is_host: bool

    class Config:
        orm_mode = True


class ResultUser(BaseModel):
    user_id: int
    judge_count_list: Union[Json[list[int]], list[int]]
    score: int

    class Config:
        orm_mode = True


class RoomMemberRecord(BaseModel):
    id: int
    user_id: int
    room_id: int
    select_difficulty: Optional[LiveDifficulty]
    is_host: bool
    judge_count_list: Optional[Union[Json[list[int]], list[int]]]
    score: Optional[int]
    ttl: Optional[datetime]

    class Config:
        orm_mode = True


def _valitate_duplicate_member(conn: Connection, user_id: int, room_id: int = 0):
    """
    すでに他の部屋に入っていたら退出する
    room_idは無視したい部屋(これから入る部屋)を指定する
    """
    for (room_id,) in conn.execute(
        text(
            "SELECT `room_id` FROM `room_member` WHERE `user_id`=:user_id and `room_id`!=:room_id"
        ),
        {"user_id": user_id, "room_id": room_id},
    ):
        _leave_room(conn, user_id, room_id)


def _leave_expired_member(conn: Connection):
    """
    TTLの切れたroom_memberレコードを削除する
    """
    execute_text = text(
        "SELECT `user_id`, `room_id` FROM `room_member` WHERE `ttl` < NOW()"
    )
    for (user_id, room_id) in conn.execute(execute_text):
        _leave_room(conn, user_id, room_id)


def _update_member_ttl(
    conn: Connection,
    room_id: int,
    user_id: Optional[int] = None,
    time: timedelta = timedelta(seconds=TTL_POLLING_INTERVAL),
):
    """
    TTLを更新する
    """
    conn.execute(
        text(
            "UPDATE `room_member` SET `ttl`=ADDTIME(NOW(), :time) "
            f"WHERE {'' if user_id is None else '`user_id`=:user_id and '}`room_id`=:room_id"
        ),
        {"user_id": user_id, "room_id": room_id, "time": time},
    )


def _join_room(
    conn: Connection,
    user_id: int,
    room_id: int,
    select_difficulty: LiveDifficulty,
    is_host: bool = False,
) -> JoinRoomResult:
    """
    部屋に参加する
    """

    # room_memberバリテーション
    _valitate_duplicate_member(conn, user_id, room_id)

    # 参加済みチェック
    execute_text = text(
        "SELECT `id` FROM `room_member` WHERE `user_id`=:user_id and `room_id`=:room_id"
    )
    result = conn.execute(execute_text, {"user_id": user_id, "room_id": room_id})
    if result.one_or_none() is not None:
        return JoinRoomResult.OK

    # room検索
    room_row = conn.execute(
        text(
            "SELECT `id` as `room_id`, `live_id`, `joined_user_count`, `max_user_count`, `wait_room_status` "
            "FROM `room` WHERE `id`=:room_id FOR UPDATE"
        ),
        {"room_id": room_id},
    ).one()
    status = WaitRoomStatus(room_row["wait_room_status"])
    if status is not WaitRoomStatus.WATING:
        return JoinRoomResult.DISBANDED
    room_info = RoomInfo.from_orm(room_row)
    if room_info.joined_user_count >= room_info.max_user_count:
        return JoinRoomResult.ROOM_FULL

    # joined_user_countをインクリメント
    update_result = conn.execute(
        text(
            "UPDATE `room` SET `joined_user_count`=`joined_user_count`+1 WHERE `id`=:room_id"
        ),
        {"room_id": room_id},
    )
    # room_memberをinsert
    result = conn.execute(
        text(
            "INSERT INTO `room_member` (`user_id`, `room_id`, `select_difficulty`, `is_host`, `ttl`) "
            "VALUES (:user_id, :room_id, :select_difficulty, :is_host, ADDTIME(NOW(), :time))"
        ),
        {
            "user_id": user_id,
            "room_id": room_id,
            "select_difficulty": select_difficulty.value,
            "is_host": is_host,
            "time": timedelta(seconds=TTL_POLLING_INTERVAL),
        },
    )
    return JoinRoomResult.OK


def create_room(token: str, live_id: int, select_difficulty: LiveDifficulty) -> int:
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)
        result = conn.execute(
            text(
                "INSERT INTO `room` "
                "(`live_id`, `joined_user_count`, `max_user_count`, `wait_room_status`) "
                "VALUES (:live_id, :joined_user_count, :max_user_count, :wait_room_status)"
            ),
            {
                "live_id": live_id,
                "joined_user_count": 0,
                "max_user_count": 4,
                "wait_room_status": WaitRoomStatus.WATING.value,
            },
        )
        room_id: int = result.lastrowid
        _join_room(conn, user.id, room_id, select_difficulty, True)
    return room_id


def get_room_info_list(token: str, live_id: int) -> list[RoomInfo]:
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)
        _valitate_duplicate_member(conn, user.id)
        result = conn.execute(
            text(
                "SELECT `id` as `room_id`, `live_id`, `joined_user_count`, `max_user_count` FROM `room` "
                "WHERE `joined_user_count`<`max_user_count` and wait_room_status=1"
                f"{' and `live_id`=:live_id' if live_id else ''}"
            ),
            {"live_id": live_id},
        )

        return list(map(RoomInfo.from_orm, result))


def join_room(
    token: str, room_id: int, select_difficulty: LiveDifficulty
) -> JoinRoomResult:
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)
        join_result = _join_room(conn, user.id, room_id, select_difficulty, False)
    return join_result


def get_room_wait_status(
    token: str, room_id: int
) -> tuple[WaitRoomStatus, list[RoomUser]]:
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)

        # memberバリテーション
        conn.execute(
            text(
                "SELECT `id` FROM `room_member` WHERE `user_id`=:user_id and `room_id`=:room_id LIMIT 1"
            ),
            {"user_id": user.id, "room_id": room_id},
        ).one()

        status_result = conn.execute(
            text("SELECT `wait_room_status` FROM `room` WHERE `id`=:room_id"),
            {"room_id": room_id},
        )
        status = WaitRoomStatus(status_result.one()["wait_room_status"])

        if status is WaitRoomStatus.WATING:
            _update_member_ttl(
                conn, room_id, user.id, timedelta(seconds=TTL_POLLING_INTERVAL)
            )

        member_result = conn.execute(
            text(
                "SELECT `user_id`, `name`, `leader_card_id`, `select_difficulty`, "
                "`user_id`=:user_id as `is_me`, `is_host` "
                "FROM `room_member` INNER JOIN `user` "
                "ON `room_id`=:room_id and `room_member`.`user_id` = `user`.`id`"
            ),
            {"room_id": room_id, "user_id": user.id},
        )
        room_user_list = list(map(RoomUser.from_orm, member_result))
        return status, room_user_list


def _get_room_member(conn: Connection, user_id: int, room_id: int):
    return RoomMemberRecord.from_orm(
        conn.execute(
            text(
                "SELECT * FROM `room_member` WHERE `user_id`=:user_id and `room_id`=:room_id"
            ),
            {"user_id": user_id, "room_id": room_id},
        ).one()
    )


def start_room(token: str, room_id: int):
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)
        member = _get_room_member(conn, user.id, room_id)
        _update_member_ttl(conn, room_id, time=timedelta(seconds=TTL_LIVE_INTERVAL))
        result = conn.execute(
            text(
                "UPDATE `room` SET `wait_room_status`=:start WHERE `id`=:room_id and `wait_room_status`=:wait"
            ),
            {
                "room_id": room_id,
                "start": WaitRoomStatus.LIVE_START.value,
                "wait": WaitRoomStatus.WATING.value,
            },
        )
        if result.rowcount != 1:
            raise Exception


def end_room(token: str, room_id: int, judge_count_list: list[int], score: int):
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)
        _update_member_ttl(conn, room_id)
        result = conn.execute(
            text(
                "UPDATE `room_member` SET `judge_count_list`=:judge_count_list, `score`=:score "
                "WHERE `user_id`=:user_id and `room_id`=:room_id"
            ),
            {
                "judge_count_list": json.dumps(judge_count_list),
                "score": score,
                "user_id": user.id,
                "room_id": room_id,
            },
        )
        if result.rowcount != 1:
            raise Exception


def get_room_result(token: str, room_id: int) -> list[ResultUser]:
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)
        _update_member_ttl(conn, room_id, user_id=user.id)
        joined_user_count = conn.execute(
            text(
                "SELECT `joined_user_count` from `room` "
                "WHERE `id`=:room_id and `wait_room_status`=:start"
            ),
            {"room_id": room_id, "start": WaitRoomStatus.LIVE_START.value},
        ).one()["joined_user_count"]
        result = conn.execute(
            text(
                "SELECT `user_id`, `judge_count_list`, `score` FROM `room_member` "
                "WHERE `room_id`=:room_id and `judge_count_list` IS NOT NULL and `score` IS NOT NULL"
            ),
            {"room_id": room_id},
        )
        result_user_list = list(map(ResultUser.from_orm, result))
        if len(result_user_list) < joined_user_count:
            return []
        if not any(ru.user_id == user.id for ru in result_user_list):
            return []
        _dissolution_room(conn, room_id)
    return result_user_list


def _leave_room(conn: Connection, user_id: int, room_id: int):
    member = RoomMemberRecord.from_orm(
        conn.execute(
            text(
                "SELECT * FROM `room_member` WHERE `room_id`=:room_id and `user_id`=:user_id"
            ),
            {"room_id": room_id, "user_id": user_id},
        ).one()
    )

    # room_member削除
    conn.execute(text("DELETE FROM `room_member` WHERE `id`=:id"), {"id": member.id})

    room_row = conn.execute(
        text(
            "SELECT `joined_user_count`, `wait_room_status` from `room` WHERE `id`=:room_id FOR UPDATE"
        ),
        {"room_id": room_id},
    ).one()
    joined_user_count = room_row["joined_user_count"]
    status = WaitRoomStatus(room_row["wait_room_status"])

    # memberのいないroomをDISSOLUTIONにする
    joined_user_count -= 1
    if joined_user_count < 1:
        status = WaitRoomStatus.DISSOLUTION
    conn.execute(
        text(
            "UPDATE `room` SET `joined_user_count`=:joined_user_count, `wait_room_status`=:status "
            "WHERE `id`=:room_id"
        ),
        {
            "room_id": room_id,
            "joined_user_count": joined_user_count,
            "status": status.value,
        },
    )

    # host移譲
    if status != WaitRoomStatus.DISSOLUTION and member.is_host:
        conn.execute(
            text(
                "UPDATE `room_member` SET `is_host`=true WHERE `room_id`=:room_id LIMIT 1"
            ),
            {"room_id": room_id},
        )


def leave_room(token: str, room_id: int):
    with engine.begin() as conn:
        conn: Connection
        user = _get_user_by_token_strict(conn, token)
        _leave_room(conn, user.id, room_id)


def _dissolution_room(conn: Connection, room_id: int):
    conn.execute(
        text("DELETE FROM `room_member` WHERE `room_id`=:room_id"), {"room_id": room_id}
    )
    conn.execute(
        text(
            "UPDATE `room` SET `joined_user_count`=:joined_user_count, `wait_room_status`=:status "
            "WHERE `id`=:room_id"
        ),
        {
            "room_id": room_id,
            "joined_user_count": 0,
            "status": WaitRoomStatus.DISSOLUTION.value,
        },
    )


def leave_expired_member():
    with engine.begin() as conn:
        _leave_expired_member(conn)
