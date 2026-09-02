import ipaddress
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SafeDiagnostic:
    text: str

    def __str__(self) -> str:
        return self.text


class SafeDiagnosticError(Exception):
    def __init__(self, diagnostic: SafeDiagnostic) -> None:
        self.diagnostic = diagnostic
        super().__init__(diagnostic.text)


def _is_valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # Digit-and-dot hosts that failed above (such as "123" or
        # "999.999.999.999") are deliberately rejected: they are neither
        # resolvable DNS names nor valid IPv4 literals.
        if ":" in host or all(
            character.isdigit() or character == "." for character in host
        ):
            return False
    else:
        return True

    try:
        dns_host = host.encode("idna").decode("ascii").removesuffix(".")
    except UnicodeError:
        return False
    if not dns_host or len(dns_host) > 253:
        return False
    return all(
        0 < len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in dns_host.split(".")
    )


def is_unambiguous_url_text(value: object) -> bool:
    """Report whether every URL parser sees the same characters.

    `urlsplit` strips leading C0 control characters and spaces and drops tab,
    carriage return and newline anywhere in the value, while urllib3 keeps
    them; a backslash is an authority or path separator to some parsers and a
    literal to others. Text carrying any of those would be validated in one
    form and requested in another, so only exact `str` free of them qualifies.
    """
    return (
        type(value) is str
        and "\\" not in value
        and all(
            character.isprintable() and not character.isspace()
            for character in value
        )
    )


def safe_url(value: object) -> SafeDiagnostic:
    if not is_unambiguous_url_text(value):
        return SafeDiagnostic("<invalid URL>")
    try:
        parts = urllib.parse.urlsplit(value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return SafeDiagnostic("<invalid URL>")
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or not _is_valid_host(host)
        or parts.netloc.count("@") > 1
        or parts.netloc.rsplit("@", 1)[-1].endswith(":")
    ):
        return SafeDiagnostic("<invalid URL>")
    rendered_host = f"[{host}]" if ":" in host else host
    authority = rendered_host if port is None else f"{rendered_host}:{port}"
    suffix = "/<redacted>" if parts.path not in {"", "/"} else ""
    return SafeDiagnostic(f"{parts.scheme}://{authority}{suffix}")


def diagnostic_for_exception(error: BaseException) -> SafeDiagnostic:
    if isinstance(error, SafeDiagnosticError):
        return error.diagnostic
    return SafeDiagnostic(
        f"Unexpected {type(error).__name__}; exception details omitted."
    )
