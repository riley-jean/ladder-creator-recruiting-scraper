"""Client registry. Add a new client by dropping in a Client subclass and
registering it here; the core needs no changes."""
from .ladder import LadderClient
from .speak import SpeakClient

CLIENTS = {
    "ladder": LadderClient,
    "speak": SpeakClient,
}


def get_client(name):
    try:
        return CLIENTS[name]()
    except KeyError:
        raise SystemExit(f"unknown client '{name}' — choose from: {', '.join(sorted(CLIENTS))}")
