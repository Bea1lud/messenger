import json
import os
import time
from aiohttp import web, WSMsgType

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHAT_HTML = os.path.join(BASE_DIR, "chat.html")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
STEGCLOAK_FILE = os.path.join(BASE_DIR, "stegcloak.min.js")

MESSAGE_TTL_SECONDS = 16 * 60 * 60
rooms = {}


def now_ts():
    return int(time.time())


def ensure_history_file():
    if not os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump({"rooms": {}}, f, ensure_ascii=False, indent=2)


def load_history_data():
    ensure_history_file()
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            data = {"rooms": {}}

    if "rooms" not in data or not isinstance(data["rooms"], dict):
        data = {"rooms": {}}

    return data


def save_history_data(data):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def prune_expired_messages():
    data = load_history_data()
    cutoff = now_ts() - MESSAGE_TTL_SECONDS
    changed = False
    new_rooms = {}

    for room_id, messages in data.get("rooms", {}).items():
        if not isinstance(messages, list):
            changed = True
            continue

        kept = []
        for item in messages:
            created_at = int(item.get("created_at", 0))
            if created_at >= cutoff:
                kept.append(item)

        if len(kept) != len(messages):
            changed = True

        if kept:
            new_rooms[room_id] = kept
        else:
            changed = True

    if changed:
        data["rooms"] = new_rooms
        save_history_data(data)


def append_history(room_id_hash: str, encrypted_payload: str, message_id: str):
    prune_expired_messages()
    data = load_history_data()

    if room_id_hash not in data["rooms"]:
        data["rooms"][room_id_hash] = []

    data["rooms"][room_id_hash].append({
        "id": message_id,
        "data": encrypted_payload,
        "created_at": now_ts()
    })

    save_history_data(data)


def load_room_history(room_id_hash: str):
    prune_expired_messages()
    data = load_history_data()
    return data["rooms"].get(room_id_hash, [])


def delete_room_history(room_id_hash: str):
    prune_expired_messages()
    data = load_history_data()

    if room_id_hash in data["rooms"]:
        del data["rooms"][room_id_hash]

    save_history_data(data)


def delete_message(room_id_hash: str, message_id: str):
    prune_expired_messages()
    data = load_history_data()

    if room_id_hash not in data["rooms"]:
        return False

    old_len = len(data["rooms"][room_id_hash])
    data["rooms"][room_id_hash] = [
        item for item in data["rooms"][room_id_hash]
        if str(item.get("id")) != message_id
    ]

    deleted = len(data["rooms"][room_id_hash]) != old_len

    if room_id_hash in data["rooms"] and not data["rooms"][room_id_hash]:
        del data["rooms"][room_id_hash]

    save_history_data(data)
    return deleted


def wipe_all_history():
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"rooms": {}}, f, ensure_ascii=False, indent=2)


def remove_ws_from_room(ws):
    empty_rooms = []

    for room_id, peers in list(rooms.items()):
        if ws in peers:
            peers.remove(ws)
        if not peers:
            empty_rooms.append(room_id)

    for room_id in empty_rooms:
        del rooms[room_id]


async def broadcast_to_room(room_id: str, payload: dict):
    if room_id not in rooms:
        return

    dead_peers = []

    for peer in rooms[room_id]:
        try:
            await peer.send_json(payload)
        except Exception:
            dead_peers.append(peer)

    for dead in dead_peers:
        if dead in rooms.get(room_id, []):
            rooms[room_id].remove(dead)

    if room_id in rooms and not rooms[room_id]:
        del rooms[room_id]


async def broadcast_to_room_except(room_id: str, sender_ws, payload: dict):
    if room_id not in rooms:
        return

    dead_peers = []

    for peer in rooms[room_id]:
        if peer is sender_ws:
            continue
        try:
            await peer.send_json(payload)
        except Exception:
            dead_peers.append(peer)

    for dead in dead_peers:
        if dead in rooms.get(room_id, []):
            rooms[room_id].remove(dead)

    if room_id in rooms and not rooms[room_id]:
        del rooms[room_id]


async def broadcast_to_all(payload: dict):
    dead = []

    for room_id, peers in list(rooms.items()):
        for peer in peers:
            try:
                await peer.send_json(payload)
            except Exception:
                dead.append(peer)

    for ws in dead:
        remove_ws_from_room(ws)


async def index(request):
    response = web.FileResponse(CHAT_HTML)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


async def stegcloak_js(request):
    if not os.path.exists(STEGCLOAK_FILE):
        return web.Response(text="stegcloak.min.js not found", status=404)

    response = web.FileResponse(STEGCLOAK_FILE)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


async def get_history(request):
    room_id = request.query.get("room", "").strip()

    if not room_id:
        return web.json_response({
            "ok": False,
            "error": "room is required"
        }, status=400)

    history = load_room_history(room_id)

    return web.json_response({
        "ok": True,
        "history": history,
        "ttl_seconds": MESSAGE_TTL_SECONDS
    })


async def clear_history(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({
            "ok": False,
            "error": "invalid json"
        }, status=400)

    room_id = str(data.get("room", "")).strip()

    if not room_id:
        return web.json_response({
            "ok": False,
            "error": "room is required"
        }, status=400)

    delete_room_history(room_id)

    await broadcast_to_room(room_id, {
        "type": "dialog-deleted"
    })

    return web.json_response({"ok": True})


async def delete_message_handler(request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({
            "ok": False,
            "error": "invalid json"
        }, status=400)

    room_id = str(data.get("room", "")).strip()
    message_id = str(data.get("id", "")).strip()

    if not room_id or not message_id:
        return web.json_response({
            "ok": False,
            "error": "room and id are required"
        }, status=400)

    deleted = delete_message(room_id, message_id)

    if deleted:
        await broadcast_to_room(room_id, {
            "type": "message-deleted",
            "id": message_id
        })

    return web.json_response({"ok": deleted})


async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    current_room = None

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                event_type = data.get("type")

                if event_type == "join":
                    prune_expired_messages()

                    room_id = str(data.get("room", "")).strip()
                    if not room_id:
                        continue

                    current_room = room_id

                    if room_id not in rooms:
                        rooms[room_id] = []

                    if ws not in rooms[room_id]:
                        rooms[room_id].append(ws)

                    if len(rooms[room_id]) > 2:
                        if ws in rooms[room_id]:
                            rooms[room_id].remove(ws)

                        await ws.send_json({"type": "room-full"})
                        current_room = None
                        continue

                    user_index = rooms[room_id].index(ws)
                    role = "offerer" if user_index == 0 else "answerer"

                    await ws.send_json({
                        "type": "joined",
                        "role": role,
                        "color": "#2ecc71" if user_index == 0 else "#3498db",
                        "peerCount": len(rooms[room_id])
                    })

                    if len(rooms[room_id]) == 2:
                        await broadcast_to_room(room_id, {"type": "ready"})

                elif event_type in ("offer", "answer", "ice-candidate"):
                    if not current_room or current_room not in rooms:
                        continue

                    for peer in rooms[current_room]:
                        if peer is not ws:
                            try:
                                await peer.send_json(data)
                            except Exception:
                                pass

                elif event_type == "store-message":
                    room_id = str(data.get("room", "")).strip()
                    encrypted_payload = data.get("payload")
                    message_id = str(data.get("id", "")).strip()

                    if room_id and encrypted_payload and message_id:
                        append_history(room_id, encrypted_payload, message_id)

                elif event_type == "relay-message":
                    room_id = str(data.get("room", "")).strip()
                    encrypted_payload = data.get("payload")
                    message_id = str(data.get("id", "")).strip()

                    if room_id and encrypted_payload and message_id:
                        await broadcast_to_room_except(room_id, ws, {
                            "type": "relay-message",
                            "id": message_id,
                            "payload": encrypted_payload
                        })

                elif event_type == "wipe-all":
                    wipe_all_history()
                    await broadcast_to_all({"type": "server-wiped"})

            elif msg.type == WSMsgType.ERROR:
                pass

    finally:
        remove_ws_from_room(ws)

    return ws


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/stegcloak.min.js", stegcloak_js)
app.router.add_get("/history", get_history)
app.router.add_post("/clear-history", clear_history)
app.router.add_post("/delete-message", delete_message_handler)
app.router.add_get("/ws", websocket_handler)

if __name__ == "__main__":
    ensure_history_file()
    port = int(os.environ.get("PORT", 8000))
    web.run_app(app, host="0.0.0.0", port=port, access_log=None)