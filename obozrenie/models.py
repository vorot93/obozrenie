from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from ipaddress import ip_address
from types import MappingProxyType


# Four of the record integers below — `Player.ping`, `Server.player_count`,
# `Server.player_limit` and `Server.ping` — reach the UI through a
# `GObject.Property(type=int)` row property in `list_rows`, which holds a
# signed 32-bit `gint` and raises TypeError on anything wider. `Player.score`
# travels as an object property instead, so that it can stay None, and an
# object property enforces nothing. The records own the domain for all five,
# so a hostile master response is rejected at this boundary rather than
# reaching a row that would raise mid-render or silently take the value.
_GINT_MIN = -(1 << 31)
_GINT_MAX = (1 << 31) - 1


def _validate_gint(
    record_name: str,
    field_name: str,
    value: int | None,
) -> None:
    """Reject an integer wider than a `gint` row property can hold.

    `Player.score` is checked against the same bound even though its own row
    property would accept more, so every rendered integer has one domain.
    `None` passes through: the exact-type checks admit it for `Player.score`
    alone, and an absent score has nothing to overflow.
    """
    if value is None:
        return
    if not _GINT_MIN <= value <= _GINT_MAX:
        raise ValueError(
            f"{record_name} {field_name} must be "
            f"from {_GINT_MIN} to {_GINT_MAX}."
        )


class _RecordMeta(type):
    def __new__(metaclass, name, bases, namespace, **kwargs):
        for base in bases:
            if getattr(base, "_record_final", False):
                raise TypeError(
                    f"{base.__name__} is a closed value-record type "
                    "and cannot be subclassed."
                )
        return super().__new__(metaclass, name, bases, namespace, **kwargs)


# slots=True recreates each concrete dataclass from this non-final base.
class _Record(metaclass=_RecordMeta):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Endpoint(_Record):
    _record_final = True
    host: str
    port: int

    def __post_init__(self) -> None:
        if type(self.host) is not str:
            raise TypeError("Endpoint host must be an exact str.")
        if type(self.port) is not int:
            raise TypeError("Endpoint port must be an exact int.")
        if (
            not self.host
            or self.host != self.host.strip()
            or any(character.isspace() for character in self.host)
            or "[" in self.host
            or "]" in self.host
        ):
            raise ValueError("Endpoint host must be nonempty and unadorned.")
        if ":" in self.host:
            try:
                is_ipv6 = ip_address(self.host).version == 6
            except ValueError:
                is_ipv6 = False
            if not is_ipv6:
                raise ValueError(
                    "A colon-containing host must be a valid IPv6 address."
                )
        if not 1 <= self.port <= 65535:
            raise ValueError("Endpoint port must be from 1 to 65535.")

    @classmethod
    def parse(cls, value: str) -> "Endpoint":
        value = value.strip()
        if value.startswith("["):
            close = value.find("]")
            if close < 0 or value[close + 1:close + 2] != ":":
                raise ValueError("Invalid bracketed IPv6 endpoint.")
            host, port_text = value[1:close], value[close + 2:]
            if ip_address(host).version != 6:
                raise ValueError("Brackets are only valid for IPv6 endpoints.")
        else:
            host, separator, port_text = value.rpartition(":")
            if not separator or ":" in host:
                raise ValueError(
                    "Endpoint must include an explicit port; IPv6 must be bracketed."
                )
        try:
            port = int(port_text)
        except ValueError as error:
            raise ValueError("Endpoint port must be an integer.") from error
        return cls(host, port)

    def __str__(self) -> str:
        try:
            is_ipv6 = ip_address(self.host).version == 6
        except ValueError:
            is_ipv6 = False
        host = f"[{self.host}]" if is_ipv6 else self.host
        return f"{host}:{self.port}"


@dataclass(frozen=True, slots=True)
class Player(_Record):
    _record_final = True
    name: str
    score: int | None = None
    ping: int = 9999

    def __post_init__(self) -> None:
        if type(self.name) is not str:
            raise TypeError("Player name must be an exact str.")
        if self.score is not None and type(self.score) is not int:
            raise TypeError("Player score must be an exact int or None.")
        if type(self.ping) is not int:
            raise TypeError("Player ping must be an exact int.")
        _validate_gint("Player", "score", self.score)
        _validate_gint("Player", "ping", self.ping)


@dataclass(frozen=True, slots=True)
class Server(_Record):
    _record_final = True
    endpoint: Endpoint
    game_id: str
    name: str = ""
    password: bool = False
    player_count: int = 0
    player_limit: int = 0
    ping: int = 9999
    secure: bool = False
    country: str = "unknown"
    game_mod: str = ""
    game_type: str = ""
    terrain: str = ""
    players: tuple[Player, ...] = ()
    rules: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.endpoint) is not Endpoint:
            raise TypeError("Server endpoint must be an exact Endpoint.")
        for field_name in (
            "game_id",
            "name",
            "country",
            "game_mod",
            "game_type",
            "terrain",
        ):
            if type(getattr(self, field_name)) is not str:
                raise TypeError(f"Server {field_name} must be an exact str.")
        for field_name in ("password", "secure"):
            if type(getattr(self, field_name)) is not bool:
                raise TypeError(f"Server {field_name} must be an exact bool.")
        for field_name in ("player_count", "player_limit", "ping"):
            field_value = getattr(self, field_name)
            if type(field_value) is not int:
                raise TypeError(f"Server {field_name} must be an exact int.")
            _validate_gint("Server", field_name, field_value)
        if self.player_count < 0 or self.player_limit < 0:
            raise ValueError("Player counts cannot be negative.")

        try:
            players = tuple(self.players)
        except TypeError:
            raise TypeError(
                "Server players must be an iterable of exact Player records."
            ) from None
        for index, player in enumerate(players):
            if type(player) is not Player:
                raise TypeError(
                    f"Server players[{index}] must be an exact Player."
                )

        if not isinstance(self.rules, Mapping):
            raise TypeError("Server rules must be a mapping.")
        rules = dict(self.rules)
        if any(type(key) is not str for key in rules):
            raise TypeError("Server rule keys must be exact str values.")
        if any(
            type(value) not in (str, int, float, bool, type(None))
            for value in rules.values()
        ):
            raise TypeError(
                "Server rule values must be exact immutable scalars."
            )
        object.__setattr__(self, "players", players)
        object.__setattr__(self, "rules", MappingProxyType(rules))

    def __deepcopy__(self, memo) -> "Server":
        if type(self) is not Server:
            raise TypeError("Server deepcopy requires an exact Server record.")
        return self

    def with_network(self, ping: int, country: str) -> "Server":
        return replace(self, ping=ping, country=country)
