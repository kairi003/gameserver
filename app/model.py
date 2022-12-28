import json
import uuid
from enum import Enum, IntEnum
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import NoResultFound

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
    judge_count_list: list[int]
    score: int


class RoomJoinException(Exception):
    """入室時のエラー"""


class RoomFullException(RoomJoinException):
    """満室時のエラー"""


class RoomDisbandedException(RoomJoinException):
    """ルーム解散時のエラー"""


def _join_room(
    conn: Connection,
    user_id: int,
    room_id: int,
    select_difficulty: LiveDifficulty,
    is_host: bool = False,
):
    room_row = conn.execute(
        text(
            "SELECT `id` as `room_id`, `live_id`, `joined_user_count`, `max_user_count`, `wait_room_status` "
            "FROM `room` WHERE `id`=:room_id FOR UPDATE"
        ),
        {"room_id": room_id},
    ).one()
    status = WaitRoomStatus(room_row["wait_room_status"])
    if status is not WaitRoomStatus.WATING:
        raise RoomDisbandedException
    room_info = RoomInfo.from_orm(room_row)
    if room_info.joined_user_count >= room_info.max_user_count:
        raise RoomFullException

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
            "INSERT INTO `room_member` (`user_id`, `room_id`, `select_difficulty`, `is_host`) "
            "VALUES (:user_id, :room_id, :select_difficulty, :is_host)"
        ),
        {
            "user_id": user_id,
            "room_id": room_id,
            "select_difficulty": select_difficulty.value,
            "is_host": is_host,
        },
    )


def create_room(token: str, live_id: int, select_difficulty: LiveDifficulty) -> int:

    # TODO: すでに部屋に入ってるかバリテーションする
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


def get_room_info_list(live_id: int) -> list[RoomInfo]:
    with engine.begin() as conn:
        conn: Connection
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
    try:
        with engine.begin() as conn:
            conn: Connection
            user = _get_user_by_token_strict(conn, token)
            _join_room(conn, user.id, room_id, select_difficulty, False)
    except RoomFullException:
        return JoinRoomResult.ROOM_FULL
    except RoomDisbandedException:
        return JoinRoomResult.DISBANDED
    except RoomJoinException:
        return JoinRoomResult.OTHER_ERROR
    return JoinRoomResult.OK


def get_room_wait_status(
    token: str, room_id: int
) -> tuple[WaitRoomStatus, list[RoomUser]]:
    with engine.begin() as conn:
        conn: Connection
        status_result = conn.execute(
            text("SELECT `wait_room_status` FROM `room` WHERE `id`=:room_id"),
            {"room_id": room_id},
        )
        user = _get_user_by_token_strict(conn, token)
        status = WaitRoomStatus(status_result.one()["wait_room_status"])
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
