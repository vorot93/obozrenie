import copy
import pickle
import unittest
from dataclasses import (
    FrozenInstanceError,
    field,
    is_dataclass,
    make_dataclass,
    replace,
)
from types import MappingProxyType
from unittest import mock

from obozrenie.models import Endpoint, Player, Server


class _MutableString(str):
    pass


class _MutableInteger(int):
    pass


class _MutableFloat(float):
    pass


def _make_bypassed_subclass(record_type):
    with mock.patch.object(record_type, "_record_final", False):
        return type(f"Legacy{record_type.__name__}", (record_type,), {})


class ClosedValueRecordTests(unittest.TestCase):
    def test_all_subclass_creation_paths_are_rejected(self):
        cases = (
            (
                Endpoint,
                "Endpoint is a closed value-record type and cannot be subclassed.",
            ),
            (
                Player,
                "Player is a closed value-record type and cannot be subclassed.",
            ),
            (
                Server,
                "Server is a closed value-record type and cannot be subclassed.",
            ),
        )

        class NonCooperativeMixin:
            def __init_subclass__(cls, **kwargs):
                pass

        class CooperativeMixin:
            def __init_subclass__(cls, **kwargs):
                super().__init_subclass__(**kwargs)

        class RightMixin:
            pass

        def make_direct(record_type):
            class DirectSubclass(record_type):
                pass

            return DirectSubclass

        def make_noncooperative_left(record_type):
            class NonCooperativeSubclass(NonCooperativeMixin, record_type):
                pass

            return NonCooperativeSubclass

        def make_cooperative_left(record_type):
            class CooperativeSubclass(CooperativeMixin, record_type):
                pass

            return CooperativeSubclass

        def make_right_mixin(record_type):
            class RightMixinSubclass(record_type, RightMixin):
                pass

            return RightMixinSubclass

        def make_plain_dataclass(record_type):
            return make_dataclass(
                f"MutableDataclass{record_type.__name__}",
                [],
                bases=(record_type,),
            )

        def make_frozen_dataclass(record_type):
            return make_dataclass(
                f"MutableFrozen{record_type.__name__}",
                [("payload", list, field(default_factory=list))],
                bases=(record_type,),
                frozen=True,
            )

        def make_slots_dataclass(record_type):
            return make_dataclass(
                f"MutableSlots{record_type.__name__}",
                [("payload", list, field(default_factory=list))],
                bases=(record_type,),
                frozen=True,
                slots=True,
            )

        for record_type, message in cases:
            for style, make_subclass in (
                ("direct", make_direct),
                ("non-cooperative left mixin", make_noncooperative_left),
                ("cooperative left mixin", make_cooperative_left),
                ("right mixin", make_right_mixin),
                ("plain dataclass", make_plain_dataclass),
                ("frozen dataclass", make_frozen_dataclass),
                ("slots dataclass", make_slots_dataclass),
            ):
                with self.subTest(record=record_type.__name__, style=style):
                    try:
                        make_subclass(record_type)
                    except Exception as error:
                        raised = error
                    else:
                        raised = None
                    self.assertIs(type(raised), TypeError)
                    self.assertEqual(str(raised), message)

    def test_inherited_final_marker_closes_bypassed_legacy_subclasses(self):
        cases = (
            (
                Endpoint,
                "LegacyEndpoint is a closed value-record type "
                "and cannot be subclassed.",
            ),
            (
                Player,
                "LegacyPlayer is a closed value-record type and cannot be subclassed.",
            ),
            (
                Server,
                "LegacyServer is a closed value-record type and cannot be subclassed.",
            ),
        )

        for record_type, message in cases:
            legacy_type = _make_bypassed_subclass(record_type)
            with self.subTest(record=record_type.__name__):
                try:
                    type(
                        f"SecondGeneration{record_type.__name__}",
                        (legacy_type,),
                        {},
                    )
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), TypeError)
                self.assertEqual(str(raised), message)

    def test_concrete_records_remain_slotted_dataclasses(self):
        values = (
            Endpoint("example.org", 1),
            Player("Ada", score=-2, ping=-4),
            Server(Endpoint("server.example", 2), "g"),
        )

        self.assertEqual(
            [
                (is_dataclass(value), hasattr(value, "__dict__"))
                for value in values
            ],
            [(True, False), (True, False), (True, False)],
        )

    def test_existing_endpoint_and_player_pickle_round_trips_remain_supported(self):
        endpoint = pickle.loads(pickle.dumps(Endpoint("example.org", 27015)))
        player = pickle.loads(pickle.dumps(Player("Ada", score=-3, ping=-7)))

        self.assertIs(type(endpoint), Endpoint)
        self.assertEqual((endpoint.host, endpoint.port), ("example.org", 27015))
        self.assertIs(type(player), Player)
        self.assertEqual(
            (player.name, player.score, player.ping), ("Ada", -3, -7)
        )


class EndpointTests(unittest.TestCase):
    def test_parses_hostname_and_ipv4(self):
        self.assertEqual(
            Endpoint.parse("example.org:27015"), Endpoint("example.org", 27015)
        )
        self.assertEqual(
            Endpoint.parse("127.0.0.1:30000"), Endpoint("127.0.0.1", 30000)
        )

    def test_parses_and_renders_bracketed_ipv6(self):
        endpoint = Endpoint.parse("[2001:db8::1]:30000")
        self.assertEqual(endpoint.host, "2001:db8::1")
        self.assertEqual(str(endpoint), "[2001:db8::1]:30000")

    def test_rejects_host_only_unbracketed_ipv6_and_bad_ports(self):
        for value in (
            "127.0.0.1",
            "2001:db8::1:30000",
            "host:0",
            "host:65536",
            "host:nope",
            "",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Endpoint.parse(value)

    def test_direct_constructor_requires_an_exact_string_host(self):
        mutable_host = _MutableString("example.org")
        mutable_host.payload = []
        for host in (mutable_host, None, b"example.org", 127001):
            with self.subTest(host=host):
                try:
                    Endpoint(host, 27015)
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), TypeError)
                self.assertEqual(
                    str(raised), "Endpoint host must be an exact str."
                )

    def test_direct_constructor_rejects_noncanonical_hosts(self):
        for host in (
            "",
            " example.org",
            "example.org ",
            "example .org",
            "example\t.org",
            "[2001:db8::1]",
            "hostname:part",
            "2001:db8::invalid",
        ):
            with self.subTest(host=host), self.assertRaises(ValueError):
                Endpoint(host, 27015)

    def test_direct_constructor_requires_an_exact_integer_port(self):
        mutable_port = _MutableInteger(27015)
        mutable_port.payload = []
        for port in (mutable_port, True, False, "27015", 27015.0, None):
            with self.subTest(port=port):
                try:
                    Endpoint("example.org", port)
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), TypeError)
                self.assertEqual(
                    str(raised), "Endpoint port must be an exact int."
                )

    def test_direct_constructor_rejects_out_of_range_ports(self):
        for port in (-1, 0, 65536):
            with self.subTest(port=port), self.assertRaises(ValueError):
                Endpoint("example.org", port)

    def test_direct_constructor_round_trips_through_rendered_syntax(self):
        for endpoint in (
            Endpoint("example.org", 27015),
            Endpoint("192.0.2.1", 30000),
            Endpoint("2001:db8::1", 65535),
        ):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(Endpoint.parse(str(endpoint)), endpoint)


class PlayerTests(unittest.TestCase):
    def test_accepts_empty_name_and_existing_score_and_ping_semantics(self):
        empty_name = Player("", score=None, ping=-30)
        scored = Player("Ada", score=-12, ping=0)

        self.assertEqual(
            (empty_name.name, empty_name.score, empty_name.ping),
            ("", None, -30),
        )
        self.assertEqual(
            (scored.name, scored.score, scored.ping),
            ("Ada", -12, 0),
        )

    def test_rejects_non_exact_field_types_with_field_errors(self):
        mutable_string = _MutableString("Ada")
        mutable_string.payload = []
        mutable_integer = _MutableInteger(7)
        mutable_integer.payload = []
        cases = (
            (
                "name string subclass",
                {"name": mutable_string},
                "Player name must be an exact str.",
            ),
            (
                "name container",
                {"name": ["Ada"]},
                "Player name must be an exact str.",
            ),
            (
                "score bool",
                {"name": "Ada", "score": True},
                "Player score must be an exact int or None.",
            ),
            (
                "score integer subclass",
                {"name": "Ada", "score": mutable_integer},
                "Player score must be an exact int or None.",
            ),
            (
                "score container",
                {"name": "Ada", "score": [7]},
                "Player score must be an exact int or None.",
            ),
            (
                "ping bool",
                {"name": "Ada", "ping": False},
                "Player ping must be an exact int.",
            ),
            (
                "ping integer subclass",
                {"name": "Ada", "ping": mutable_integer},
                "Player ping must be an exact int.",
            ),
            (
                "ping container",
                {"name": "Ada", "ping": {"milliseconds": 7}},
                "Player ping must be an exact int.",
            ),
        )

        for label, arguments, message in cases:
            with self.subTest(label=label):
                try:
                    Player(**arguments)
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), TypeError)
                self.assertEqual(str(raised), message)

    def test_gint_fields_accept_exact_signed_32_bit_boundaries(self):
        floor = Player("Ada", score=-2147483648, ping=-2147483648)
        ceiling = Player("Lin", score=2147483647, ping=2147483647)
        unscored = Player("Ola", score=None, ping=2147483647)

        self.assertEqual(
            (floor.score, floor.ping), (-2147483648, -2147483648)
        )
        self.assertEqual(
            (ceiling.score, ceiling.ping), (2147483647, 2147483647)
        )
        self.assertEqual((unscored.score, unscored.ping), (None, 2147483647))

    def test_gint_fields_reject_values_outside_signed_32_bit_range(self):
        cases = (
            (
                "score below the floor",
                {"name": "Ada", "score": -2147483649},
                "Player score must be from -2147483648 to 2147483647.",
            ),
            (
                "score above the ceiling",
                {"name": "Ada", "score": 2147483648},
                "Player score must be from -2147483648 to 2147483647.",
            ),
            (
                "ping below the floor",
                {"name": "Ada", "ping": -2147483649},
                "Player ping must be from -2147483648 to 2147483647.",
            ),
            (
                "ping above the ceiling",
                {"name": "Ada", "ping": 2147483648},
                "Player ping must be from -2147483648 to 2147483647.",
            ),
        )

        for label, arguments, message in cases:
            with self.subTest(label=label):
                try:
                    Player(**arguments)
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), ValueError)
                self.assertEqual(str(raised), message)


class ServerTests(unittest.TestCase):
    def test_accepts_empty_strings_exact_scalars_and_existing_numeric_semantics(self):
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="",
            name="",
            password=True,
            player_count=3,
            player_limit=1,
            ping=-25,
            secure=False,
            country="",
            game_mod="",
            game_type="",
            terrain="",
            players=(Player("", score=-4, ping=-8),),
            rules={
                "text": "",
                "integer": -3,
                "float": -2.5,
                "boolean": False,
                "missing": None,
            },
        )

        self.assertEqual(
            server.to_mapping(),
            {
                "host": "example.org:1",
                "game_id": "",
                "name": "",
                "password": True,
                "player_count": 3,
                "player_limit": 1,
                "ping": -25,
                "secure": False,
                "country": "",
                "game_mod": "",
                "game_type": "",
                "terrain": "",
                "players": [{"name": "", "score": -4, "ping": -8}],
                "rules": {
                    "text": "",
                    "integer": -3,
                    "float": -2.5,
                    "boolean": False,
                    "missing": None,
                },
            },
        )

    def test_rejects_non_exact_scalar_fields_with_field_errors(self):
        mutable_string = _MutableString("value")
        mutable_string.payload = []
        mutable_integer = _MutableInteger(7)
        mutable_integer.payload = []
        mutable_endpoint_type = _make_bypassed_subclass(Endpoint)
        mutable_endpoint = mutable_endpoint_type("example.org", 1)
        object.__setattr__(mutable_endpoint, "payload", [])
        cases = (
            (
                "endpoint subclass",
                {"endpoint": mutable_endpoint},
                "Server endpoint must be an exact Endpoint.",
            ),
            (
                "endpoint container",
                {"endpoint": ["example.org", 1]},
                "Server endpoint must be an exact Endpoint.",
            ),
            (
                "game_id subclass",
                {"game_id": mutable_string},
                "Server game_id must be an exact str.",
            ),
            (
                "name subclass",
                {"name": mutable_string},
                "Server name must be an exact str.",
            ),
            (
                "country subclass",
                {"country": mutable_string},
                "Server country must be an exact str.",
            ),
            (
                "game_mod subclass",
                {"game_mod": mutable_string},
                "Server game_mod must be an exact str.",
            ),
            (
                "game_type subclass",
                {"game_type": mutable_string},
                "Server game_type must be an exact str.",
            ),
            (
                "terrain subclass",
                {"terrain": mutable_string},
                "Server terrain must be an exact str.",
            ),
            (
                "password integer",
                {"password": 1},
                "Server password must be an exact bool.",
            ),
            (
                "secure integer subclass",
                {"secure": mutable_integer},
                "Server secure must be an exact bool.",
            ),
            (
                "player_count bool",
                {"player_count": True},
                "Server player_count must be an exact int.",
            ),
            (
                "player_count integer subclass",
                {"player_count": mutable_integer},
                "Server player_count must be an exact int.",
            ),
            (
                "player_limit bool",
                {"player_limit": False},
                "Server player_limit must be an exact int.",
            ),
            (
                "player_limit container",
                {"player_limit": [16]},
                "Server player_limit must be an exact int.",
            ),
            (
                "ping bool",
                {"ping": True},
                "Server ping must be an exact int.",
            ),
            (
                "ping integer subclass",
                {"ping": mutable_integer},
                "Server ping must be an exact int.",
            ),
        )

        for label, overrides, message in cases:
            arguments = {
                "endpoint": Endpoint("example.org", 1),
                "game_id": "g",
            }
            arguments.update(overrides)
            with self.subTest(label=label):
                try:
                    Server(**arguments)
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), TypeError)
                self.assertEqual(str(raised), message)

    def test_rejects_only_negative_player_counts(self):
        cases = (
            ("player_count", {"player_count": -1, "player_limit": 0}),
            ("player_limit", {"player_count": 0, "player_limit": -1}),
        )

        for label, counts in cases:
            with self.subTest(label=label), self.assertRaises(
                ValueError
            ) as raised:
                Server(
                    endpoint=Endpoint("example.org", 1),
                    game_id="g",
                    **counts,
                )
            self.assertEqual(
                str(raised.exception), "Player counts cannot be negative."
            )

    def test_gint_fields_accept_signed_32_bit_boundaries_and_nonnegative_counts(
        self,
    ):
        ceiling = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            player_count=2147483647,
            player_limit=2147483647,
            ping=2147483647,
            players=(Player("Ada", score=2147483647, ping=2147483647),),
        )
        floor = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            player_count=0,
            player_limit=0,
            ping=-2147483648,
            players=(Player("Lin", score=-2147483648, ping=-2147483648),),
        )

        self.assertEqual(
            (ceiling.player_count, ceiling.player_limit, ceiling.ping),
            (2147483647, 2147483647, 2147483647),
        )
        self.assertEqual(
            (floor.player_count, floor.player_limit, floor.ping),
            (0, 0, -2147483648),
        )

    def test_gint_fields_reject_values_outside_signed_32_bit_range(self):
        message = "Server %s must be from -2147483648 to 2147483647."
        cases = (
            (
                "player_count above the ceiling",
                {"player_count": 2147483648},
                message % "player_count",
            ),
            (
                "player_count below the floor",
                {"player_count": -2147483649},
                message % "player_count",
            ),
            (
                "player_limit above the ceiling",
                {"player_limit": 2147483648},
                message % "player_limit",
            ),
            (
                "player_limit below the floor",
                {"player_limit": -2147483649},
                message % "player_limit",
            ),
            (
                "ping above the ceiling",
                {"ping": 2147483648},
                message % "ping",
            ),
            (
                "ping below the floor",
                {"ping": -2147483649},
                message % "ping",
            ),
        )

        for label, overrides, expected in cases:
            arguments = {
                "endpoint": Endpoint("example.org", 1),
                "game_id": "g",
            }
            arguments.update(overrides)
            with self.subTest(label=label):
                try:
                    Server(**arguments)
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), ValueError)
                self.assertEqual(str(raised), expected)

    def test_requires_an_iterable_of_exact_player_records(self):
        mutable_player_type = _make_bypassed_subclass(Player)
        mutable_player = mutable_player_type("Ada")
        object.__setattr__(mutable_player, "payload", [])
        cases = (
            (
                "non-iterable",
                7,
                "Server players must be an iterable of exact Player records.",
            ),
            (
                "mapping element",
                [Player("Ada"), {"name": "Lin"}],
                "Server players[1] must be an exact Player.",
            ),
            (
                "player subclass",
                [mutable_player],
                "Server players[0] must be an exact Player.",
            ),
        )

        for label, players, message in cases:
            with self.subTest(label=label):
                try:
                    Server(
                        endpoint=Endpoint("example.org", 1),
                        game_id="g",
                        players=players,
                    )
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), TypeError)
                self.assertEqual(str(raised), message)

    def test_rejects_non_exact_rule_keys_and_values(self):
        mutable_string = _MutableString("mutable")
        mutable_string.payload = []
        mutable_integer = _MutableInteger(3)
        mutable_integer.payload = []
        mutable_float = _MutableFloat(2.5)
        mutable_float.payload = []
        cases = (
            (
                "rules container",
                [],
                "Server rules must be a mapping.",
            ),
            (
                "string key subclass",
                {mutable_string: "value"},
                "Server rule keys must be exact str values.",
            ),
            (
                "tuple key",
                {("mode",): "value"},
                "Server rule keys must be exact str values.",
            ),
            (
                "string value subclass",
                {"value": mutable_string},
                "Server rule values must be exact immutable scalars.",
            ),
            (
                "integer value subclass",
                {"value": mutable_integer},
                "Server rule values must be exact immutable scalars.",
            ),
            (
                "float value subclass",
                {"value": mutable_float},
                "Server rule values must be exact immutable scalars.",
            ),
            (
                "list value",
                {"value": []},
                "Server rule values must be exact immutable scalars.",
            ),
            (
                "dict value",
                {"value": {}},
                "Server rule values must be exact immutable scalars.",
            ),
            (
                "tuple value",
                {"value": ()},
                "Server rule values must be exact immutable scalars.",
            ),
        )

        for label, rules, message in cases:
            with self.subTest(label=label):
                try:
                    Server(
                        endpoint=Endpoint("example.org", 1),
                        game_id="g",
                        rules=rules,
                    )
                except Exception as error:
                    raised = error
                else:
                    raised = None
                self.assertIs(type(raised), TypeError)
                self.assertEqual(str(raised), message)

    def test_copies_caller_containers_and_is_transitively_immutable(self):
        first_player = Player("Ada", score=-2, ping=-5)
        players = [first_player]
        rules = {
            "text": "survival",
            "integer": 3,
            "float": 1.5,
            "boolean": True,
            "missing": None,
        }
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            players=players,
            rules=rules,
        )

        players[0] = Player("Grace")
        players.append(Player("Lin"))
        rules["text"] = "creative"
        rules["mutable"] = []

        self.assertEqual(
            tuple(
                (player.name, player.score, player.ping)
                for player in server.players
            ),
            (("Ada", -2, -5),),
        )
        self.assertIsInstance(server.rules, MappingProxyType)
        self.assertEqual(
            dict(server.rules),
            {
                "text": "survival",
                "integer": 3,
                "float": 1.5,
                "boolean": True,
                "missing": None,
            },
        )
        self.assertEqual(
            [(type(key), type(value)) for key, value in server.rules.items()],
            [
                (str, str),
                (str, int),
                (str, float),
                (str, bool),
                (str, type(None)),
            ],
        )
        self.assertIs(type(server.endpoint), Endpoint)
        self.assertIs(type(server.endpoint.host), str)
        self.assertIs(type(server.endpoint.port), int)
        self.assertIs(type(server.players), tuple)
        self.assertIs(type(server.players[0]), Player)
        self.assertIs(type(server.players[0].name), str)
        self.assertIs(type(server.players[0].score), int)
        self.assertIs(type(server.players[0].ping), int)
        self.assertIs(copy.deepcopy(server), server)
        with self.assertRaises(TypeError):
            server.rules["text"] = "mutated"
        with self.assertRaises(FrozenInstanceError):
            server.players[0].name = "mutated"
        with self.assertRaises(FrozenInstanceError):
            server.endpoint.host = "mutated.example"
        with self.assertRaises(FrozenInstanceError):
            server.name = "mutated"

    def test_deepcopy_rejects_a_bypassed_server_subclass(self):
        mutable_server_type = _make_bypassed_subclass(Server)
        mutable_server = mutable_server_type(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
        )
        object.__setattr__(mutable_server, "payload", [])

        try:
            copy.deepcopy(mutable_server)
        except Exception as error:
            raised = error
        else:
            raised = None

        self.assertIs(type(raised), TypeError)
        self.assertEqual(
            str(raised), "Server deepcopy requires an exact Server record."
        )

    def test_replace_and_deepcopy_preserve_an_exact_server(self):
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            ping=25,
            country="CA",
        )

        replaced = replace(server, ping=-7, country="")

        self.assertIs(type(replaced), Server)
        self.assertEqual((replaced.ping, replaced.country), (-7, ""))
        self.assertIs(copy.deepcopy(replaced), replaced)

    def test_materializes_supplied_player_iterables(self):
        server = Server(
            endpoint=Endpoint("example.org", 1),
            game_id="g",
            players=(player for player in (Player("Ada"), Player("Lin"))),
        )

        self.assertEqual(
            tuple(
                (player.name, player.score, player.ping)
                for player in server.players
            ),
            (("Ada", None, 9999), ("Lin", None, 9999)),
        )
