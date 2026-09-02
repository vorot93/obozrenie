import unittest

from obozrenie.diagnostics import (
    SafeDiagnostic,
    SafeDiagnosticError,
    diagnostic_for_exception,
    safe_url,
)


class SafeDiagnosticTests(unittest.TestCase):
    def test_safe_url_redacts_userinfo_path_query_and_fragment(self):
        self.assertEqual(
            str(
                safe_url(
                    "https://user:pass@example.com:8443/private/token?a=secret#x"
                )
            ),
            "https://example.com:8443/<redacted>",
        )

    def test_safe_url_preserves_bracketed_ipv6_host_and_port(self):
        self.assertEqual(
            str(safe_url("https://[2001:db8::1]:443/list")),
            "https://[2001:db8::1]:443/<redacted>",
        )

    def test_safe_url_preserves_ipv4_host_without_port(self):
        self.assertEqual(
            str(safe_url("http://192.0.2.1/list")),
            "http://192.0.2.1/<redacted>",
        )

    def test_safe_url_fails_closed_for_malformed_values(self):
        for value in (
            None,
            1,
            "",
            "not a URL",
            "https:///missing-host",
            "https://exa mple.com/private",
            "https://exam\x00ple.com/private",
            "https://exam\nple.com/private",
            r"https://example.com\private\secret",
            "https://example..com/private",
            "https://-example.com/private",
            "https://example-.com/private",
            "https://exa_mple.com/private",
            "https://999.999.999.999/private",
        ):
            with self.subTest(value=value):
                self.assertEqual(str(safe_url(value)), "<invalid URL>")

    def test_unexpected_exception_diagnostic_never_calls_str_or_repr(self):
        class HostileError(Exception):
            @property
            def args(self):
                raise AssertionError("args inspected")

            def __str__(self):
                raise AssertionError("str called")

            def __repr__(self):
                raise AssertionError("repr called")

        self.assertEqual(
            str(diagnostic_for_exception(HostileError("secret"))),
            "Unexpected HostileError; exception details omitted.",
        )

    def test_safe_diagnostic_error_returns_declared_diagnostic(self):
        class HostileSafeDiagnosticError(SafeDiagnosticError):
            @property
            def args(self):
                raise AssertionError("args inspected")

        diagnostic = SafeDiagnostic("Declared safe diagnostic.")

        self.assertIs(
            diagnostic_for_exception(HostileSafeDiagnosticError(diagnostic)),
            diagnostic,
        )
