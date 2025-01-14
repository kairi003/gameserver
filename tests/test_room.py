from fastapi.testclient import TestClient

from app import model
from app.api import app

client = TestClient(app)
user_tokens = []


def _create_users():
    for i in range(10):
        response = client.post(
            "/user/create",
            json={"user_name": f"room_user_{i}", "leader_card_id": 1000},
        )
        user_tokens.append(response.json()["user_token"])


_create_users()


def _auth_header(i=0):
    token = user_tokens[i]
    return {"Authorization": f"bearer {token}"}


def _test_room_create(i, live_id=1001, difficulty=1):
    response = client.post(
        "/room/create",
        headers=_auth_header(i),
        json={"live_id": live_id, "select_difficulty": difficulty},
    )
    assert response.status_code == 200
    room_id = response.json()["room_id"]
    return room_id


def _test_room_join(i, room_id, difficulty=1):
    response = client.post(
        "/room/join",
        headers=_auth_header(i),
        json={"room_id": room_id, "select_difficulty": difficulty},
    )
    assert response.status_code == 200
    join_room_result = model.JoinRoomResult(response.json()["join_room_result"])
    return join_room_result


def test_room_2():
    room_id = _test_room_create(0)
    room_id2 = _test_room_create(1)
    assert room_id != room_id2
    for i in range(2, 2 + 3):
        join_result = _test_room_join(i, room_id)
        assert join_result is model.JoinRoomResult.OK
    join_result = _test_room_join(5, room_id)
    assert join_result is model.JoinRoomResult.ROOM_FULL


def test_room_1():
    response = client.post(
        "/room/create",
        headers=_auth_header(1),
        json={"live_id": 1001, "select_difficulty": 1},
    )
    assert response.status_code == 200

    room_id = response.json()["room_id"]
    print(f"room/create {room_id=}")

    response = client.post(
        "/room/join",
        headers=_auth_header(0),
        json={"room_id": room_id, "select_difficulty": 1},
    )
    assert response.status_code == 200
    print("room/join response:", response.json())

    response = client.post(
        "/room/leave", headers=_auth_header(1), json={"room_id": room_id}
    )
    assert response.status_code == 200
    print("room/leave response:", response.json())

    response = client.post(
        "/room/list", headers=_auth_header(1), json={"live_id": 1001}
    )
    assert response.status_code == 200
    print("room/list response:", response.json())

    response = client.post(
        "/room/wait", headers=_auth_header(), json={"room_id": room_id}
    )
    assert response.status_code == 200
    print("room/wait response:", response.json())

    response = client.post(
        "/room/start", headers=_auth_header(), json={"room_id": room_id}
    )
    assert response.status_code == 200
    print("room/wait response:", response.json())

    response = client.post(
        "/room/end",
        headers=_auth_header(),
        json={
            "room_id": room_id,
            "score": 1234,
            "judge_count_list": [1111, 222, 33, 44, 5],
        },
    )
    assert response.status_code == 200
    print("room/end response:", response.json())

    response = client.post(
        "/room/result",
        headers=_auth_header(),
        json={"room_id": room_id},
    )
    assert response.status_code == 200
    print("room/end response:", response.json())
